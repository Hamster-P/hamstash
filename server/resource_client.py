import asyncio
import re
import shlex
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
from bs4 import BeautifulSoup

from services.proxy import get_proxy_url

ANIMEGARDEN_URL = "https://api.animes.garden/resources"
ANIMEGARDEN_FEED_URL = "https://api.animes.garden/feed.xml"
DMHY_RSS_URL = "https://dmhy.org/topics/rss/rss.xml"
DMHY_SEARCH_URL = "https://dmhy.org/topics/list"
NYAA_URL = "https://nyaa.si"
NYAA_ANIME_CATEGORY = "1_0"  # nyaa的Anime大类(含全部字幕/生肉子分类),不含漫画/音乐等其他大类

HEADERS = {"User-Agent": "hamstash/0.1 (personal project)"}

# 三个源各自的RSS/Feed接口都有一个实测出来的固定窗口上限,超出这个数量的历史存量
# 资源,RSS订阅是永远抓不到的(qBittorrent只能拿到窗口内的这些)——前端自己累加
# 翻页翻到的条数,拿去跟这几个数字比较,提醒用户"订阅覆盖不到这么多历史存量"。
RSS_WINDOW_CAP = {"dmhy": 500, "nyaa": 75, "animegarden": 100}

# dmhy/nyaa标题开头的[字幕组名]/【字幕组名】,两个源都靠标题粗略提取,提法完全一致。
_FANSUB_NAME_RE = re.compile(r"^[\[【]([^\]】]+)[\]】]")


def _extract_fansub_name(title: str) -> str:
    match = _FANSUB_NAME_RE.match(title)
    return match.group(1) if match else "未知字幕组"


# 字幕语言这个筛选项在services/rss_rules.py::_subtitle_terms里有一套完整的
# "必须包含哪几个候选词之一 + 必须排除哪几个候选词"的正则组合,但那是给RSS
# mustContain用的,针对的是"已经命中的条目要不要留下"这种精确复核。这里不一样,
# 是要拼进站点自己的全文搜索关键词里——站点搜索只支持"必须包含哪些词"(空格
# 分词、词与词之间AND),没有OR/NOT语法,没法照搬那套组合逻辑,只能取一个
# 最有代表性的单词。查出来的结果不是100%精确,但覆盖的是站点全站历史,
# 前端拿到结果后还会照旧再跑一遍filteredResults精确过滤,这里只负责把"能搜到
# 的范围"从一页扩大到全站,精确度交给前端兜底。
_SUBTITLE_REPRESENTATIVE_TERM = {
    "纯简体": "简", "繁体": "繁", "简繁": "简繁", "简日": "简日", "繁日": "繁日", "RAW": "RAW",
    # "日文/无字"本质是"没有中文字幕"这个否定条件,全文搜索表达不了"不包含",
    # 没有可靠的正向关键词能代表它,这里不追加任何词,继续完全交给前端筛选。
    "日文/无字": None,
}


def _quote_term(term: str) -> str:
    """词里带空格时用双引号包起来,让站点自己的搜索引擎当一个短语整体匹配,
    不会被它自己的分词逻辑拆散——跟教用户手动加引号避免"3"误伤"第X集"是同一个道理。
    """
    return f'"{term}"' if " " in term else term


def _extra_query_terms(quality: str | None, subtitle: str | None, format_: str | None) -> list[str]:
    """把画质/字幕语言/格式这几个筛选,翻译成能直接拼进站点搜索关键词的附加词。"""
    terms = []
    if quality and quality != "不限":
        terms.append(quality)
    if format_ and format_ != "不限":
        terms.append(format_)
    if subtitle and subtitle != "不限":
        token = _SUBTITLE_REPRESENTATIVE_TERM.get(subtitle)
        if token:
            terms.append(token)
    return terms


def _split_keyword(keyword: str) -> list[str]:
    """按空格切词,但双引号包起来的短语当一个词(引号本身去掉)——跟前端
    DownloadPage.tsx::splitKeywordTokens、services/rss_poller.py::matches_rule
    是同一个语义,给"本地精确短语匹配"用。这里是唯一权威实现,前端/
    rss_poller.py都从这里(或对应各自语言的等价实现)复用同一套约定;
    旧的services/rss_rules.py里还有一份独立副本,那是保留不动的旧代码,
    不在这次统一范围内。
    """
    try:
        return shlex.split(keyword)
    except ValueError:
        return keyword.split()


def _strip_query_quotes(keyword: str) -> str:
    """去掉用户为"本地精确短语匹配"特意加的引号语法,只用在构造发给站点搜索/RSS
    接口的查询字符串这一步——nyaa的搜索引擎会把字面引号字符当成必须原样出现在
    标题里的内容去匹配,永远查不到东西(实测确认:带引号返回空结果,不带引号
    正常返回);dmhy/AnimeGarden的搜索能兼容/忽略引号字符,两种写法结果一致
    (同样实测确认过),所以这里不用按source分别判断要不要处理,统一清洗更简单、
    也不会影响dmhy/AnimeGarden的行为。

    本地精确短语匹配(前端filteredResults、rss_poller.py::matches_rule)继续用
    用户原始输入(带引号)做判断,不受这里影响——服务端查询变宽泛只是为了"至少
    能捞到东西",精确度交给本地过滤兜底,这个分工思路不变。
    """
    if not keyword:
        return keyword
    return " ".join(_split_keyword(keyword))


def _build_effective_keyword(keyword: str, extra_terms: list[str], fansub_name: str | None = None) -> str:
    """把用户输入的关键词、字幕组名(dmhy/nyaa没有结构化的字幕组参数,只能拼进
    关键词里)、画质/字幕语言/格式这几个附加词,拼成最终发给站点搜索接口的
    关键词字符串,统一在这里加引号保护(带空格的词各自加引号,不是拼完整体加)。
    """
    parts = [_strip_query_quotes(keyword)] if keyword else []
    if fansub_name:
        parts.append(_quote_term(fansub_name))
    parts.extend(_quote_term(t) for t in extra_terms)
    return " ".join(parts)


def _shape_animegarden_items(resources: list[dict]) -> list[dict]:
    """把AnimeGarden /resources接口返回的原始条目列表，归一化成本项目统一的资源字段。
    关键词搜索(_search_animegarden)和按bgm_id搜索(_search_animegarden_by_subject)
    拿到的resources结构完全一样，只是查询参数不同，这份组装逻辑两边共用。
    """
    results = []
    for item in resources:
        fansub = item.get("fansub") or {}
        results.append(
            {
                "source": "animegarden",
                "provider": item.get("provider"),
                "title": item.get("title", ""),
                "fansub_name": fansub.get("name", "未知字幕组"),
                "magnet": item.get("magnet"),
                "size": _animegarden_size_to_bytes(item.get("size")),
                "created_at": item.get("createdAt"),
                "bgm_id": item.get("subjectId"),
                # AnimeGarden是聚合多个上游站点的二道源,同一条资源背后可能是dmhy/nyaa/
                # 其他站点里的任意一个,没有它自己的规范详情页,详情按钮只在dmhy/nyaa实现。
                "detail_url": None,
            }
        )
    return results


async def _fetch_animegarden_page(extra_params: dict, page: int, page_size: int = 100) -> tuple[list[dict], bool]:
    """AnimeGarden的/resources接口本身支持真分页(page=2/3...拿到的确实是更早的
    不同内容),响应里的pagination.complete明确告知"这一页是不是已经是最后一页"。
    只请求调用方要的这一页,不在服务端预取多页——之前改成"一次性翻页翻到底"
    吃过亏(dmhy被防刷机制拉黑过一次),翻页统一交给前端"加载更多"按需触发。
    """
    async with httpx.AsyncClient(headers=HEADERS, timeout=15.0, proxy=get_proxy_url(), follow_redirects=True) as client:
        resp = await client.get(
            ANIMEGARDEN_URL, params={**extra_params, "page": page, "pageSize": page_size}
        )
        resp.raise_for_status()
        data = resp.json()

    resources = data.get("resources", [])
    complete = bool((data.get("pagination") or {}).get("complete", True))
    return resources, complete


async def _search_animegarden(keyword: str, page: int = 1, page_size: int = 100):
    """主力源:AnimeGarden,用search参数做服务端关键词过滤,数据结构好
    (自带字幕组名和Bangumi编号)。只取一页。"""
    resources, _ = await _fetch_animegarden_page({"search": keyword}, page, page_size)
    return _shape_animegarden_items(resources)


def _parse_dmhy_page(html: str) -> list[dict]:
    """解析dmhy网页搜索页(topics/list)的一页结果。
    表格结构是<table id="topic_list"><tbody><tr>,每行9个<td>:
    0=日期 1=分类 2=标题(内含字幕组team_id链接+详情页链接) 3=下载按钮(图标,无文字)
    4=大小 5=种子数 6=下载数 7=完成数 8=发布人。
    """
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for row in soup.select("table#topic_list tbody tr"):
        title_link = row.select_one('td.title a[href^="/topics/view/"]')
        if title_link is None:
            continue
        magnet_link = row.select_one('a[href^="magnet:"]')
        if magnet_link is None:
            continue

        title = title_link.get_text(" ", strip=True)
        detail_url = DMHY_SEARCH_URL.rsplit("/topics/list", 1)[0] + title_link["href"]

        team_link = row.select_one('a[href^="/topics/list/team_id/"]')
        fansub_name = team_link.get_text(strip=True) if team_link else _extract_fansub_name(title)

        tds = row.find_all("td")
        size_text = tds[4].get_text(strip=True) if len(tds) > 4 else None
        # 日期这一格里藏了个display:none的隐藏<span>重复了一遍同样的文字(应该是配合
        # 前端排序插件用的),get_text()不认CSS,会把两份文字拼一起,只取第一个
        # 直接文本节点,不递归进子节点(也就不会捞到那个隐藏span里的重复内容)。
        created_at = tds[0].find(string=True, recursive=False).strip() if tds else None

        results.append(
            {
                "source": "dmhy",
                "provider": "dmhy",
                "title": title,
                "fansub_name": fansub_name or "未知字幕组",
                "magnet": magnet_link["href"],
                "size": _parse_size_text(size_text),
                "created_at": created_at,
                "bgm_id": None,
                "detail_url": detail_url,
            }
        )
    return results


async def _fetch_dmhy_page(keyword: str, page: int) -> list[dict]:
    """dmhy直连,用网页搜索页(不是RSS——RSS固定上限500条且不支持翻页,实测过)。
    字幕组名优先取页面上的team_id链接文本,比从标题正则猜更准;标题里没有
    team_id链接时(极少数情况)才退回正则兜底。

    只请求这一页,不预取多页——dmhy有防刷机制,之前改成一次性顺序拉7页,
    被判定成"5秒内多次搜索"把这个IP的搜索功能直接拉黑了(不止拦单次请求,
    是持续封了一段时间,连浏览器手动搜都不行),必须避免短时间内密集请求,
    翻页交给前端"加载更多"按钮按需触发。
    """
    async with httpx.AsyncClient(headers=HEADERS, timeout=15.0, proxy=get_proxy_url(), follow_redirects=True) as client:
        resp = await client.get(
            DMHY_SEARCH_URL,
            params={"keyword": keyword, "sort_id": 0, "team_id": 0, "order": "date-desc", "page": page},
        )
        resp.raise_for_status()
    return _parse_dmhy_page(resp.text)

def _animegarden_size_to_bytes(size_kb) -> int | None:
    """AnimeGarden的size字段单位是KB,不是字节——拿真实API数据核对过:
    某集芙莉莲的size=557158,当字节数解读只有0.53MB(比一张截图还小,明显不对),
    当KB解读换算出544MB才是1080p WEB-DL单集该有的体积。统一转换成字节,
    跟下面_parse_nyaa_size()以及前端formatSize()"size永远是字节数"的约定保持一致。
    """
    if not size_kb:
        return None
    return int(size_kb) * 1024


def _parse_size_text(size_text: str | None) -> int | None:
    """nyaa/dmhy页面上的大小都是"38.0 GiB"/"222.3MB"这种人类可读字符串,不是字节数——
    前端formatSize()期望拿到的是原始字节数再自己换算显示单位,
    这里转换成一致的数值类型,不然前端拿字符串做除法会得到NaN。
    """
    if not size_text:
        return None
    match = re.match(r"([\d.]+)\s*([KMGT]?i?B)", size_text.strip(), re.IGNORECASE)
    if not match:
        return None
    value = float(match.group(1))
    multipliers = {
        "B": 1,
        "KIB": 1024, "KB": 1024,
        "MIB": 1024**2, "MB": 1024**2,
        "GIB": 1024**3, "GB": 1024**3,
        "TIB": 1024**4, "TB": 1024**4,
    }
    return int(value * multipliers.get(match.group(2).upper(), 1))


def _parse_nyaa_page(html: str) -> list[dict]:
    """解析nyaa网页搜索页(不是page=rss那个格式——RSS固定只给最新75条,不支持
    翻页,实测过page=rss+p=2跟p=1返回一模一样的内容)。表格结构是
    <table class="...torrent-list"><tbody><tr>,每行的<td>依次是:
    0=分类图标 1=标题(内含"/view/{id}"详情链接,可能还有个"#comments"评论数链接)
    2=下载按钮(torrent+磁力链接) 3=大小 4=data-timestamp+发布时间文字 5=种子数 6=下载数 7=完成数。
    """
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for row in soup.select("table.torrent-list tbody tr"):
        title_link = row.select_one('a[href^="/view/"]:not([href*="#comments"])')
        if title_link is None:
            continue
        magnet_link = row.select_one('a[href^="magnet:"]')
        if magnet_link is None:
            continue

        title = title_link.get("title") or title_link.get_text(strip=True)
        detail_url = NYAA_URL + title_link["href"]

        tds = row.find_all("td")
        size_text = tds[3].get_text(strip=True) if len(tds) > 3 else None
        timestamp = tds[4].get("data-timestamp") if len(tds) > 4 else None
        created_at = None
        if timestamp:
            created_at = (
                datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
                .strftime("%a, %d %b %Y %H:%M:%S +0000")
            )

        results.append(
            {
                "source": "nyaa",
                "provider": "nyaa",
                "title": title,
                "fansub_name": _extract_fansub_name(title),
                "magnet": magnet_link["href"],
                "size": _parse_size_text(size_text),
                "created_at": created_at,
                "bgm_id": None,
                "detail_url": detail_url,
            }
        )
    return results


async def _fetch_nyaa_page(keyword: str, page: int) -> list[dict]:
    """补充源:nyaa.si,英语圈老牌综合BT站,限定只搜"Anime"大类(c=1_0),用网页
    搜索页(不是RSS——RSS固定只给最新75条,不支持翻页,实测过)。
    没有bgm_id这层服务器端归一化,字幕组名靠标题粗略提取,用法上跟dmhy一致。
    注意:nyaa上绝大多数资源用英文/罗马音标题,拿中文关键词搜大概率没有结果,
    这是站点本身的特性,不是这里要修的问题。

    只请求这一页,不预取多页——跟dmhy同理,没必要每次搜索都无脑打好几个请求
    过去,翻页交给前端"加载更多"按钮按需触发。
    """
    async with httpx.AsyncClient(headers=HEADERS, timeout=15.0, proxy=get_proxy_url(), follow_redirects=True) as client:
        resp = await client.get(NYAA_URL, params={"c": NYAA_ANIME_CATEGORY, "f": 0, "q": keyword, "p": page})
        resp.raise_for_status()
    return _parse_nyaa_page(resp.text)


async def _search_animegarden_by_subject(bgm_id: int, page: int = 1, page_size: int = 100):
    """
    直接按Bangumi ID查AnimeGarden,比关键词文本匹配更精确——
    不同字幕组标题写法差异(繁简、有无虚词、罗马音)在这里天然不是问题,
    因为AnimeGarden服务器端已经把这些种子都关联到了同一个bgm_id。
    """
    resources, _ = await _fetch_animegarden_page({"subject": bgm_id}, page, page_size)
    return _shape_animegarden_items(resources)

async def search_by_source(
    keyword: str,
    source: str,
    bgm_id: int | None = None,
    page: int = 1,
    fansub_name: str | None = None,
    quality: str | None = None,
    subtitle: str | None = None,
    format: str | None = None,
) -> dict:
    """
    按用户明确选择的数据源搜索,不做静默降级/自动切换。
    AnimeGarden在已知bgm_id时优先按subject查,比关键词文本匹配更精确、更能覆盖命名变体。

    fansub_name/quality/subtitle/format这几个筛选条件,现在是真正发给站点服务端的
    查询条件,不是"先拿一页搜索结果、再在前端本地筛"——之前那种做法,筛出来的
    结果数量取决于筛选条件命中的条目恰好有多少落在了"这一页"里,经常筛完只剩
    寥寥几条,不代表真实存量。AnimeGarden有结构化的fansub参数,单独传更精确;
    其余情况(dmhy/nyaa的字幕组,以及三个源共通的画质/字幕语言/格式)站点搜索
    接口都没有对应的结构化参数,只能拼进关键词文本里,靠站点自己的全文检索去
    匹配(见_build_effective_keyword/_extra_query_terms)——这样查出来的范围
    覆盖站点全量历史,不再受"只看了一页"限制,前端拿到结果后还会照旧再跑一遍
    本地精确过滤兜底精度。

    每次调用只请求这一个源的这一页(page从1开始),服务端不预取多页——之前改成
    "一次性翻页翻到底"吃过亏,dmhy的防刷机制直接把这个IP的搜索功能封了一段时间。
    翻页交给前端"加载更多"按钮按需触发,天然把请求间隔开,不会短时间内密集打过去。

    返回{"results": [...], "has_more": bool, "rss_window_cap": M}——has_more只是
    "这一页看起来是不是最后一页"的判断(AnimeGarden有精确的pagination.complete
    字段;dmhy/nyaa没有总数概念,只能拿"这一页是不是空的"乐观判断,真正翻到底了
    才会返回False),rss_window_cap是这个源RSS/Feed接口的固定窗口上限,前端自己
    累加"已经翻了多少条"跟这个数比较,决定要不要提醒"订阅覆盖不到这么多存量"。
    """
    extra_terms = _extra_query_terms(quality, subtitle, format)

    if source == "animegarden":
        # fansub是AnimeGarden原生的结构化参数,单独传,比拼进关键词文本更精确;
        # subject(bgm_id)和search可以同时传(实测过,两者会一起生效做AND过滤),
        # 画质/字幕语言/格式没有独立参数,还是只能拼进search里。
        effective_keyword = _build_effective_keyword(keyword, extra_terms)
        subject_params: dict = {"subject": bgm_id} if bgm_id else {}
        if effective_keyword:
            subject_params["search"] = effective_keyword
        if fansub_name:
            subject_params["fansub"] = fansub_name

        if bgm_id:
            # AnimeGarden并不是所有资源都回填了subjectId(实测过某些代理商/正版
            # 引进的批次完全没有subjectId字段),只按subject查会静默漏掉这些——
            # 这里额外并发一次不带subject的纯文本查询兜底,两边结果按magnet去重
            # 合并,subject查到的排在前面(更精确,优先展示)。
            text_params: dict = {}
            if effective_keyword:
                text_params["search"] = effective_keyword
            if fansub_name:
                text_params["fansub"] = fansub_name
            (subject_resources, subject_complete), (text_resources, text_complete) = await asyncio.gather(
                _fetch_animegarden_page(subject_params, page),
                _fetch_animegarden_page(text_params, page),
            )
            subject_items = _shape_animegarden_items(subject_resources)
            text_items = _shape_animegarden_items(text_resources)
            seen_magnets = {item["magnet"] for item in subject_items}
            results = subject_items + [item for item in text_items if item["magnet"] not in seen_magnets]
            has_more = (not subject_complete) or (not text_complete)
        else:
            resources, complete = await _fetch_animegarden_page(subject_params, page)
            results = _shape_animegarden_items(resources)
            has_more = not complete
    elif source == "dmhy":
        # dmhy的team_id参数虽然存在,但目前只解析出字幕组的展示名、没存数字ID
        # (标题几乎都以"[字幕组名]"开头),字幕组过滤和画质/格式/字幕语言一样
        # 直接拼进keyword文本里,靠dmhy自己的全文搜索匹配,命中率足够高。
        effective_keyword = _build_effective_keyword(keyword, extra_terms, fansub_name)
        results = await _fetch_dmhy_page(effective_keyword, page)
        has_more = len(results) > 0
    elif source == "nyaa":
        # nyaa完全没有字幕组这个结构化概念,同样靠拼关键词。
        effective_keyword = _build_effective_keyword(keyword, extra_terms, fansub_name)
        results = await _fetch_nyaa_page(effective_keyword, page)
        has_more = len(results) > 0
    else:
        raise ValueError(f"未知数据源: {source}")

    return {
        "results": results,
        "has_more": has_more,
        "rss_window_cap": RSS_WINDOW_CAP.get(source),
    }

def build_dmhy_rss_url(keyword: str) -> str:
    """
    构建dmhy按关键词过滤的RSS订阅地址,交给qBittorrent长期订阅用。
    dmhy的RSS本身支持keyword查询参数做服务端过滤,所以这里直接拼URL即可,
    不需要额外的搜索逻辑。
    """
    query = urlencode({"keyword": _strip_query_quotes(keyword)})
    return f"{DMHY_RSS_URL}?{query}"

def build_animegarden_rss_url_by_subject(bgm_id: int, fansub_name: str | None = None) -> str:
    """按bgm_id构建AnimeGarden的RSS订阅地址,是首选方案。"""
    params = {"subject": bgm_id}
    if fansub_name:
        params["fansub"] = fansub_name
    query = urlencode(params)
    return f"{ANIMEGARDEN_FEED_URL}?{query}"

async def find_bgm_id_by_title(title: str):
    """
    在AnimeGarden最近的资源里找有没有匹配这个标题的条目,
    顺手拿现成的subjectId,省一次去Mikan详情页爬取的请求。
    找不到返回None,调用方需要自己再走原来的Mikan解析兜底。
    """
    try:
        results = await _search_animegarden(title, page_size=20)
    except Exception:
        return None
    for item in results:
        if item.get("bgm_id"):
            return item["bgm_id"]
    return None

def build_animegarden_rss_url(keyword: str, fansub_name: str | None = None) -> str:
    """
    构建AnimeGarden按关键词(+可选字幕组)过滤的RSS订阅地址。
    参数名沿用/resources JSON接口的约定(search/fansub),
    /feed.xml理论上是同一套过滤参数,只是输出格式是RSS而不是JSON。
    """
    params = {"search": _strip_query_quotes(keyword)}
    if fansub_name:
        params["fansub"] = fansub_name
    query = urlencode(params)
    return f"{ANIMEGARDEN_FEED_URL}?{query}"


def build_nyaa_rss_url(keyword: str) -> str:
    """构建nyaa.si按关键词过滤、限定Anime大类的RSS订阅地址,交给qBittorrent长期订阅用。
    keyword必须先去掉引号短语语法(见_strip_query_quotes)——nyaa的搜索把字面引号字符
    当成必须原样出现在标题里的内容去匹配,带引号的查询会返回空结果(实测确认过)。
    """
    query = urlencode({"page": "rss", "c": NYAA_ANIME_CATEGORY, "f": 0, "q": _strip_query_quotes(keyword)})
    return f"{NYAA_URL}/?{query}"