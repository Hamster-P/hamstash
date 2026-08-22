"""下载源抽象层的公共设施:统一结果模型、共享纯函数、源配置读取、唯一权威过滤谓词、
SourceAdapter 接口。

这里集中了原本分散在 resource_client.py 和 services/rss_poller.py 两个模块里的:
- 搜索结果模型(裸 dict)与 RSS FeedItem —— 现在统一成一个 ResourceItem;
- 关键词/引号/画质格式等拼词逻辑、大小字符串解析、字幕组名提取;
- nyaa 自己拼磁力链接用的 tracker 列表 + info_hash 拼装;
- 字幕语言/合集识别的词表 + 匹配谓词 —— 现在是全仓库唯一副本 matches_criteria,
  搜索接口(服务端权威过滤)和 RSS 轮询共用同一份,保证"看到的 = 下到的"。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx

import config_store
from services.proxy import get_proxy_url

# 所有源共用的 UA(dmhy 曾经因短时间密集请求被限流,UA 保持低调);代理由
# services.proxy.get_proxy_url() 统一提供,搜索与 RSS 轮询走同一套代理配置,
# 保证两条路径在代理环境下行为一致。
HEADERS = {"User-Agent": "hamstash/0.1 (personal project)"}


# ---------------------------------------------------------------------------
# 共享 HTTP 入口:搜索抓取与 RSS Feed 抓取都走这里,统一 UA + 代理配置,
# 保证两条路径在代理环境下完全一致(用户关心的"代理下是否一致"由此保证)。
# ---------------------------------------------------------------------------
async def http_get(url: str, params: dict | None = None, *, timeout: float = 15.0) -> httpx.Response:
    async with httpx.AsyncClient(
        headers=HEADERS, timeout=timeout, proxy=get_proxy_url(), follow_redirects=True
    ) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        return resp


async def http_get_bytes(url: str) -> bytes:
    resp = await http_get(url)
    return resp.content


async def probe_source_reachable(adapter: "SourceAdapter", *, timeout: float = 8.0) -> bool:
    """轮询前的源级可达性预检:经当前代理探一下该源主 URL(adapter.proxy_probe()['url'],
    取自 URL_FIELDS[0])能否连上。只关心传输层能否到达——拿到任何 HTTP 响应(含 4xx/5xx)
    即算连通(链路是通的,跟 settings.py::_probe 同款判定);只有连接/代理/超时类错误才算
    不可达。给 RSS/整理轮询"不可达就整源/整轮跳过"用,避免逐条订阅各撞一次超时。"""
    url = (adapter.proxy_probe() or {}).get("url")
    if not url:
        return True  # 没有可探 URL 就不拦,交给原有逐条逻辑
    try:
        async with httpx.AsyncClient(
            headers=HEADERS, timeout=timeout, proxy=get_proxy_url(), follow_redirects=True
        ) as client:
            await client.head(url)  # 不 raise_for_status:拿到任何响应即视为连通
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 统一结果模型
# ---------------------------------------------------------------------------
@dataclass
class ResourceItem:
    """搜索结果与 RSS Feed 条目共用的统一模型。

    搜索侧(HTML/JSON 抓取)填 size/created_at/bgm_id/detail_url/fansub 等;
    RSS 侧(XML Feed)填 guid/info_hash,填不了的字段留默认值。两条路径从此
    流动同一个形状,过同一个 matches_criteria 谓词。
    """
    source: str
    title: str
    magnet: str | None = None
    provider: str | None = None
    fansub_name: str = "未知字幕组"
    size: int | None = None
    created_at: str | None = None
    bgm_id: int | None = None
    detail_url: str | None = None
    # RSS 侧专用:去重键 / nyaa 自己拼磁力链接用的 info hash
    guid: str | None = None
    info_hash: str | None = None

    def to_api_dict(self) -> dict:
        """给 /resources/search 返回前端用的字段——沿用重构前裸 dict 的那 9 个 key,
        前端 ResourceItem TS interface 不用改。guid/info_hash 是 RSS 内部用的,不外泄。"""
        return {
            "source": self.source,
            "provider": self.provider,
            "title": self.title,
            "fansub_name": self.fansub_name,
            "magnet": self.magnet,
            "size": self.size,
            "created_at": self.created_at,
            "bgm_id": self.bgm_id,
            "detail_url": self.detail_url,
        }


@dataclass
class SearchCriteria:
    """一次搜索/一条订阅规则共用的过滤条件。search 接口和 RSS 规则都归一到这个形状,
    喂给唯一的 matches_criteria。"""
    keyword: str | None = None
    fansub_name: str | None = None
    quality: str | None = None
    subtitle: str | None = None
    format: str | None = None
    release_type: str | None = None


@dataclass
class SearchResult:
    results: list[ResourceItem] = field(default_factory=list)
    has_more: bool = False


# ---------------------------------------------------------------------------
# 源配置读取(设置页可改 base URL / 启用开关,应对换域名/换镜像)
# ---------------------------------------------------------------------------
def _all_overrides() -> dict:
    """读设置里的 download_sources(JSON 字符串,存在 INI / app_setting 里)。
    读 INI(read_ini 已 {**DEFAULTS, **section} 合并),不需要 DB session,搜索与
    轮询在任何上下文都能取到。解析失败/为空一律当作"无覆盖",回落各 adapter 内置默认。"""
    raw = (config_store.read_ini().get("download_sources") or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def source_overrides(source_id: str) -> dict:
    entry = _all_overrides().get(source_id)
    return entry if isinstance(entry, dict) else {}


def source_enabled(source_id: str, default: bool = True) -> bool:
    val = source_overrides(source_id).get("enabled", default)
    return bool(val)


def configured_url(source_id: str, key: str, default: str) -> str:
    """取某源某个 URL 配置项,留空/未配则用 adapter 内置默认(这是"改 API 地址"的落点)。"""
    val = source_overrides(source_id).get(key)
    return val.strip() if isinstance(val, str) and val.strip() else default


# ---------------------------------------------------------------------------
# 共享纯函数(原 resource_client.py 里的私有工具,移到这里给各 adapter 复用)
# ---------------------------------------------------------------------------
_FANSUB_NAME_RE = re.compile(r"^[\[【]([^\]】]+)[\]】]")


def extract_fansub_name(title: str) -> str:
    match = _FANSUB_NAME_RE.match(title)
    return match.group(1) if match else "未知字幕组"


def quote_term(term: str) -> str:
    """词里带空格时用双引号包起来当短语整体匹配,不被站点分词逻辑拆散。"""
    return f'"{term}"' if " " in term else term


def extra_query_terms(quality: str | None, format_: str | None) -> list[str]:
    """画质/格式两个筛选翻译成能拼进站点搜索关键词的附加词。字幕语言不在这里
    (单个 CJK 字符查询词站点分词查不到),完全交给本地 matches_criteria 过滤。"""
    terms = []
    if quality and quality != "不限":
        terms.append(quality)
    if format_ and format_ != "不限":
        terms.append(format_)
    return terms


def split_keyword(keyword: str) -> list[str]:
    """按空格切词,双引号包起来的短语当一个词(引号去掉)——"本地精确短语匹配"的唯一权威实现,
    搜索/RSS 过滤都用它。"""
    import shlex
    try:
        return shlex.split(keyword)
    except ValueError:
        return keyword.split()


def strip_query_quotes(keyword: str) -> str:
    """去掉用户为精确短语匹配加的引号语法,只用在构造发给站点搜索/RSS 的查询串这一步
    (nyaa 会把字面引号当必须原样出现的内容,带引号查不到东西)。"""
    if not keyword:
        return keyword
    return " ".join(split_keyword(keyword))


def build_effective_keyword(keyword: str, extra_terms: list[str], fansub_name: str | None = None) -> str:
    """把关键词、字幕组名(dmhy/nyaa 没有结构化字幕组参数,只能拼进关键词)、
    画质/格式附加词拼成最终发给站点搜索接口的关键词串,带空格的词各自加引号。"""
    parts = [strip_query_quotes(keyword)] if keyword else []
    if fansub_name:
        parts.append(quote_term(fansub_name))
    parts.extend(quote_term(t) for t in extra_terms)
    return " ".join(parts)


def animegarden_size_to_bytes(size_bytes) -> int | None:
    """AnimeGarden 的 size **本来就是字节数**,这里只做空值/类型归一,不做单位换算
    (跟 parse_size_text / 前端 formatSize "size 永远是字节数"的约定一致)。

    这个函数原本写的是 `int(size_kb) * 1024`,注释也声称上游单位是 KB —— 那是错的,
    而且从 0.7.0 落地第一天就错(不是上游后来改了口径),导致所有 AnimeGarden 结果的
    大小统一虚报 1024 倍:界面上一集 1080p 番显示成"1433.60 GB"。

    三重实测确认过是字节:
    1. 300 条样本、3 个 provider(dmhy/moe/mikan)全是 int,当字节解释中位数
       567MB~1.4GB、最大 133GB(整季 BD 合集),当 KB 则中位数 567GB~1.4TB,不可能;
    2. 跟 dmhy 页面抓取那条**完全独立**的链路(parse_size_text 解析"622.2MB"这类
       人类可读字符串)按标题精确配对 18 条,比值全部严格等于 1.0000;
    3. 各 provider 之间没有第二个数量级分布,不存在"不同源/不同上传者口径不一样"。

    所以不要再给它补上 *1024。
    """
    if not size_bytes:
        return None
    return int(size_bytes)


def parse_size_text(size_text: str | None) -> int | None:
    """nyaa/dmhy 页面上的"38.0 GiB"/"222.3MB"人类可读字符串转成字节数。"""
    if not size_text:
        return None
    match = re.match(r"([\d.]+)\s*([KMGT]?i?B)", size_text.strip(), re.IGNORECASE)
    if not match:
        return None
    value = float(match.group(1))
    multipliers = {
        "B": 1,
        "KIB": 1024, "KB": 1024,
        "MIB": 1024 ** 2, "MB": 1024 ** 2,
        "GIB": 1024 ** 3, "GB": 1024 ** 3,
        "TIB": 1024 ** 4, "TB": 1024 ** 4,
    }
    return int(value * multipliers.get(match.group(2).upper(), 1))


# nyaa 的 RSS 只给 .torrent 链接 + nyaa:infoHash,自己拼磁力链接用的公共 tracker
_PUBLIC_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "http://nyaa.tracker.wf:7777/announce",
]


def build_magnet_from_hash(info_hash: str, title: str) -> str:
    trackers = "&".join(f"tr={quote(t, safe='')}" for t in _PUBLIC_TRACKERS)
    return f"magnet:?xt=urn:btih:{info_hash}&dn={quote(title)}&{trackers}"


# ---------------------------------------------------------------------------
# 唯一权威过滤谓词(全仓库唯一副本;搜索服务端过滤 + RSS 轮询共用)
# ---------------------------------------------------------------------------
_SIMPLIFIED_TERMS = ["简", "chs", "gb"]
_TRADITIONAL_TERMS = ["繁", "cht", "big5"]
_JAPANESE_TERMS = ["日", "jp", "japanese"]
_RAW_TERMS = ["raw", "bilibili", "baha", "cr", "crunchyroll", "web-dl"]
_BATCH_TERMS = ["合集", "全集", "batch", "pack", "fin"]
_BATCH_RANGE_RE = re.compile(r"\d+-\d+")


def _subtitle_matches(title_lower: str, subtitle: str) -> bool:
    """与前端 SUBTITLE_OPTIONS(DownloadPage.tsx)逐条对齐的唯一权威实现。
    取值是前端下拉/订阅规则实际存的那套:简体/繁体/简繁/简日/繁日/RAW/日文/无字。
    (旧 rss_poller.matches_rule 里用的是 "纯简体" 这类对不上的字符串,导致 RSS 对
    subtitle=简体/繁体 实际没生效——这次统一按前端取值修掉这个"看到≠下到"的缺陷。)"""
    has_simplified = any(k in title_lower for k in _SIMPLIFIED_TERMS)
    has_traditional = any(k in title_lower for k in _TRADITIONAL_TERMS)
    has_japanese = any(k in title_lower for k in _JAPANESE_TERMS)
    is_raw = any(k in title_lower for k in _RAW_TERMS)

    if subtitle == "简体":
        return has_simplified and not has_traditional and "简日" not in title_lower and not is_raw
    if subtitle == "繁体":
        return has_traditional and not has_simplified and "繁日" not in title_lower and not is_raw
    if subtitle == "简繁":
        return "简繁" in title_lower or (has_simplified and has_traditional)
    if subtitle == "简日":
        return any(k in title_lower for k in ["简日", "chs_jp", "chs&jpg", "jp_ch"]) or (
            has_simplified and has_japanese and not has_traditional
        )
    if subtitle == "繁日":
        return any(k in title_lower for k in ["繁日", "cht_jp"]) or (
            has_traditional and has_japanese and not has_simplified
        )
    if subtitle == "RAW":
        return is_raw
    if subtitle == "日文/无字":
        return has_japanese and not has_simplified and not has_traditional and not is_raw
    return True


def matches_criteria(item: ResourceItem, criteria: SearchCriteria) -> bool:
    """唯一权威过滤谓词:搜索结果(服务端权威过滤)和 RSS 轮询命中判断都过这个函数,
    保证"搜索页看到的"和"订阅实际下载的"在同一数据窗口内逐项一致。

    全部大小写不敏感(修掉旧 matches_rule 里字幕组大小写敏感这个已知缺陷,并跟
    前端 filteredResults 大小写不敏感的处理对齐)。谓词只依赖 title / fansub_name,
    两条路径的 ResourceItem 都有。
    """
    title_lower = item.title.lower()

    if criteria.keyword:
        tokens = split_keyword(criteria.keyword.strip().lower())
        if tokens and not all(tok in title_lower for tok in tokens if tok):
            return False

    if criteria.fansub_name and criteria.fansub_name != "全部":
        fansub_lower = criteria.fansub_name.lower()
        # 搜索侧(尤其 animegarden)有结构化 fansub_name,按字段精确匹配;
        # RSS/爬取侧字幕组是从标题正则提取的,退一步允许字幕组名出现在标题里。
        # 两条都覆盖,避免任一路径漏判。
        field_hit = (item.fansub_name or "").lower() == fansub_lower
        title_hit = fansub_lower in title_lower
        if not (field_hit or title_hit):
            return False

    if criteria.quality and criteria.quality != "不限" and criteria.quality.lower() not in title_lower:
        return False

    if criteria.format and criteria.format != "不限" and criteria.format.lower() not in title_lower:
        return False

    if criteria.subtitle and criteria.subtitle != "不限" and not _subtitle_matches(title_lower, criteria.subtitle):
        return False

    if criteria.release_type:
        is_batch = any(k in title_lower for k in _BATCH_TERMS) or bool(_BATCH_RANGE_RE.search(title_lower))
        if criteria.release_type == "单集(追更)" and is_batch:
            return False
        if criteria.release_type == "合集/全集(完结)" and not is_batch:
            return False

    return True


# ---------------------------------------------------------------------------
# SourceAdapter 接口
# ---------------------------------------------------------------------------
class SourceAdapter:
    """一个下载源的完整能力:搜索(HTML/JSON 抓取解析)+ RSS 订阅(Feed URL 构造 + 解析)。
    子类务必保持 id 与历史 SubscriptionRule.source 落库值一致(dmhy/nyaa/animegarden),
    否则历史订阅轮询会找不到源。
    """
    id: str = ""
    label: str = ""
    # 前端下拉/交互需要的元信息
    supports_subject: bool = False          # 是否支持按 bgm_id 精确查
    rate_limit_cooldown_ms: int | None = None  # 前端两次搜索之间的冷却(dmhy 防刷),None=不限
    rss_window_cap: int | None = None       # RSS/Feed 固定窗口上限,前端据此提示"订阅覆盖不到这么多存量"
    # 设置页可编辑的 URL 配置项:(key, 显示名, 内置默认 URL)。子类按自己的端点填。
    # key 要跟 configured_url(...) 里读的 key 一致。
    URL_FIELDS: list[tuple[str, str, str]] = []

    async def search(self, criteria: SearchCriteria, bgm_id: int | None, page: int) -> SearchResult:
        raise NotImplementedError

    def build_rss_urls(self, rule) -> list[str]:
        """给一条订阅规则构造 RSS 订阅地址(可能多个,如 animegarden 的 subject 版+关键词版)。"""
        raise NotImplementedError

    def parse_feed(self, xml_bytes: bytes) -> list[ResourceItem]:
        """解析该源的 RSS Feed 为统一 ResourceItem(带 guid/info_hash)。
        取数同源的源(animegarden)可不实现,由 poll() 覆写另行处理。"""
        raise NotImplementedError

    async def poll(self, rule) -> list[ResourceItem]:
        """一条订阅轮询时"取候选条目"的默认实现:抓 build_rss_urls 给的一个或多个 Feed、
        逐个 parse_feed、按 guid(退回 magnet)去重合并。返回的候选还要由调用方
        (rss_poller)统一过 matches_criteria 再决定下载——保证和搜索侧同谓词同判定。

        animegarden 这类"搜索接口与 Feed 同源"的源覆写本方法,直接走 search() 取数,
        让"搜索看到的"和"订阅抓到的"完全同源。"""
        items: list[ResourceItem] = []
        seen: set[str] = set()
        for url in self.build_rss_urls(rule):
            try:
                xml = await http_get_bytes(url)
            except Exception as e:  # noqa: BLE001 单个 Feed 失败不影响其他 Feed
                print(f"[RSS引擎] 拉取/解析RSS失败 source={self.id} url={url}: {e}")
                continue
            for it in self.parse_feed(xml):
                key = it.guid or it.magnet
                if key and key not in seen:
                    seen.add(key)
                    items.append(it)
        return items

    def public_meta(self) -> dict:
        """给下载页下拉用的轻量元信息。"""
        return {
            "id": self.id,
            "label": self.label,
            "supports_subject": self.supports_subject,
            "rate_limit_cooldown_ms": self.rate_limit_cooldown_ms,
            "rss_window_cap": self.rss_window_cap,
        }

    def proxy_probe(self) -> dict:
        """设置页"测试代理"要探测这个源时用的目标:探它当前配置的主 URL(第一个 URL 字段),
        换了镜像地址后探的自然也是新地址。返回 {name, url, note}。"""
        key, _label, default = self.URL_FIELDS[0]
        return {"name": self.label, "url": configured_url(self.id, key, default), "note": "下载页数据源"}

    def config_state(self) -> dict:
        """给 GET /resources/sources 的完整状态:元信息 + 启用开关 + 各 URL 字段的默认值/当前覆盖值。
        设置页据此渲染每个源一行(启用开关 + URL 输入框),下载页只取 enabled 的做下拉。"""
        overrides = source_overrides(self.id)
        urls = []
        for key, label, default in self.URL_FIELDS:
            val = overrides.get(key)
            urls.append({
                "key": key,
                "label": label,
                "default": default,
                "value": val if isinstance(val, str) else "",
            })
        return {
            **self.public_meta(),
            "enabled": source_enabled(self.id),
            "urls": urls,
        }
