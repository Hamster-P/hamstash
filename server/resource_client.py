import re
import xml.etree.ElementTree as ET
from urllib.parse import urlencode, quote

import httpx

from services.common import get_proxy_url

ANIMEGARDEN_URL = "https://api.animes.garden/resources"
ANIMEGARDEN_FEED_URL = "https://api.animes.garden/feed.xml"
DMHY_RSS_URL = "https://dmhy.org/topics/rss/rss.xml"
NYAA_URL = "https://nyaa.si"
NYAA_ANIME_CATEGORY = "1_0"  # nyaa的Anime大类(含全部字幕/生肉子分类),不含漫画/音乐等其他大类
NYAA_NAMESPACES = {"nyaa": "https://nyaa.si/xmlns/nyaa"}

HEADERS = {"User-Agent": "hamstash/0.1 (personal project)"}


async def _search_animegarden(keyword: str, page_size: int = 50):
    """主力源:AnimeGarden,用search参数做服务端关键词过滤,数据结构好
    (自带字幕组名和Bangumi编号)。"""
    async with httpx.AsyncClient(headers=HEADERS, timeout=15.0, proxy=get_proxy_url(), follow_redirects=True) as client:
        resp = await client.get(
            ANIMEGARDEN_URL,
            params={"page": 1, "pageSize": page_size, "search": keyword},
        )
        resp.raise_for_status()
        data = resp.json()

    resources = data.get("resources", [])
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


async def _search_dmhy_fallback(keyword: str):
    """备用源:AnimeGarden连不上时才用dmhy直连,字幕组名靠标题粗略提取,
    准确度不如AnimeGarden,也没有Bangumi编号。"""
    async with httpx.AsyncClient(headers=HEADERS, timeout=15.0, proxy=get_proxy_url(), follow_redirects=True) as client:
        resp = await client.get(DMHY_RSS_URL, params={"keyword": keyword})
        resp.raise_for_status()
        xml_text = resp.text

    root = ET.fromstring(xml_text)
    results = []
    for item in root.iter("item"):
        title = item.findtext("title") or ""
        link = item.findtext("link") or ""
        enclosure = item.find("enclosure")
        magnet_or_torrent = enclosure.get("url") if enclosure is not None else link
        pub_date = item.findtext("pubDate")

        match = re.match(r"^[\[【]([^\]】]+)[\]】]", title)
        fansub_name = match.group(1) if match else "未知字幕组"

        results.append(
            {
                "source": "dmhy",
                "provider": "dmhy",
                "title": title,
                "fansub_name": fansub_name,
                "magnet": magnet_or_torrent,
                "size": None,
                "created_at": pub_date,
                "bgm_id": None,
                # dmhy的<link>本来就是种子详情页(share.dmhy.org/topics/view/...),
                # 有enclosure时上面只把它当磁力链兜底用,这里顺手作为详情页地址暴露出去。
                "detail_url": link or None,
            }
        )
    return results

def _animegarden_size_to_bytes(size_kb) -> int | None:
    """AnimeGarden的size字段单位是KB,不是字节——拿真实API数据核对过:
    某集芙莉莲的size=557158,当字节数解读只有0.53MB(比一张截图还小,明显不对),
    当KB解读换算出544MB才是1080p WEB-DL单集该有的体积。统一转换成字节,
    跟下面_parse_nyaa_size()以及前端formatSize()"size永远是字节数"的约定保持一致。
    """
    if not size_kb:
        return None
    return int(size_kb) * 1024


def _parse_nyaa_size(size_text: str | None) -> int | None:
    """nyaa:size是"38.0 GiB"这种人类可读字符串,不是字节数——
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


async def _search_nyaa(keyword: str):
    """补充源:nyaa.si,英语圈老牌综合BT站,限定只搜"Anime"大类(c=1_0)。
    没有bgm_id/字幕组这层服务器端归一化,字幕组名靠标题粗略提取,用法上跟dmhy一致。
    注意:nyaa上绝大多数资源用英文/罗马音标题,拿中文关键词搜大概率没有结果,
    这是站点本身的特性,不是这里要修的问题。
    """
    async with httpx.AsyncClient(headers=HEADERS, timeout=15.0, proxy=get_proxy_url(), follow_redirects=True) as client:
        resp = await client.get(
            NYAA_URL,
            params={"page": "rss", "c": NYAA_ANIME_CATEGORY, "f": 0, "q": keyword},
        )
        resp.raise_for_status()
        xml_text = resp.text

    root = ET.fromstring(xml_text)
    results = []
    for item in root.iter("item"):
        title = item.findtext("title") or ""
        link = item.findtext("link") or ""  # .torrent直链,不是磁力链,仅在没有infoHash时兜底
        # 种子详情页(如 https://nyaa.si/view/2136263)在<guid isPermaLink="true">里,
        # 不是<link>——后者是.torrent直链,两个字段职责不同,别搞混了。
        detail_url = item.findtext("guid") or None
        info_hash = item.findtext("nyaa:infoHash", namespaces=NYAA_NAMESPACES)
        pub_date = item.findtext("pubDate")
        size_text = item.findtext("nyaa:size", namespaces=NYAA_NAMESPACES)

        match = re.match(r"^[\[【]([^\]】]+)[\]】]", title)
        fansub_name = match.group(1) if match else "未知字幕组"

        magnet = (
            f"magnet:?xt=urn:btih:{info_hash}&dn={quote(title)}" if info_hash else link
        )

        results.append(
            {
                "source": "nyaa",
                "provider": "nyaa",
                "title": title,
                "fansub_name": fansub_name,
                "magnet": magnet,
                "size": _parse_nyaa_size(size_text),
                "created_at": pub_date,
                "bgm_id": None,
                "detail_url": detail_url,
            }
        )
    return results


async def _search_animegarden_by_subject(bgm_id: int, page_size: int = 50):
    """
    直接按Bangumi ID查AnimeGarden,比关键词文本匹配更精确——
    不同字幕组标题写法差异(繁简、有无虚词、罗马音)在这里天然不是问题,
    因为AnimeGarden服务器端已经把这些种子都关联到了同一个bgm_id。
    """
    async with httpx.AsyncClient(headers=HEADERS, timeout=15.0, proxy=get_proxy_url(), follow_redirects=True) as client:
        resp = await client.get(
            ANIMEGARDEN_URL,
            params={"page": 1, "pageSize": page_size, "subject": bgm_id},
        )
        resp.raise_for_status()
        data = resp.json()

    resources = data.get("resources", [])
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
                "detail_url": None,  # 同上,AnimeGarden没有自己的规范详情页
            }
        )
    return results

async def search_by_source(keyword: str, source: str, bgm_id: int | None = None):
    """
    按用户明确选择的数据源搜索,不做静默降级/自动切换。
    AnimeGarden在已知bgm_id时优先按subject查,比关键词文本匹配更精确、更能覆盖命名变体。
    """
    if source == "animegarden":
        if bgm_id:
            return await _search_animegarden_by_subject(bgm_id)
        return await _search_animegarden(keyword)
    if source == "dmhy":
        return await _search_dmhy_fallback(keyword)
    if source == "nyaa":
        return await _search_nyaa(keyword)
    raise ValueError(f"未知数据源: {source}")

def build_dmhy_rss_url(keyword: str) -> str:
    """
    构建dmhy按关键词过滤的RSS订阅地址,交给qBittorrent长期订阅用。
    dmhy的RSS本身支持keyword查询参数做服务端过滤,所以这里直接拼URL即可,
    不需要额外的搜索逻辑。
    """
    query = urlencode({"keyword": keyword})
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
    params = {"search": keyword}
    if fansub_name:
        params["fansub"] = fansub_name
    query = urlencode(params)
    return f"{ANIMEGARDEN_FEED_URL}?{query}"


def build_nyaa_rss_url(keyword: str) -> str:
    """构建nyaa.si按关键词过滤、限定Anime大类的RSS订阅地址,交给qBittorrent长期订阅用。"""
    query = urlencode({"page": "rss", "c": NYAA_ANIME_CATEGORY, "f": 0, "q": keyword})
    return f"{NYAA_URL}/?{query}"