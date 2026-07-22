import re
import xml.etree.ElementTree as ET
from urllib.parse import urlencode

import httpx

ANIMEGARDEN_URL = "https://api.animes.garden/resources"
ANIMEGARDEN_FEED_URL = "https://api.animes.garden/feed.xml"
DMHY_RSS_URL = "https://dmhy.org/topics/rss/rss.xml"

HEADERS = {"User-Agent": "hamstash/0.1 (personal project)"}


async def _search_animegarden(keyword: str, page_size: int = 50):
    """主力源:AnimeGarden,用search参数做服务端关键词过滤,数据结构好
    (自带字幕组名和Bangumi编号)。"""
    async with httpx.AsyncClient(headers=HEADERS, timeout=15.0) as client:
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
                "size": item.get("size"),
                "created_at": item.get("createdAt"),
                "bgm_id": item.get("subjectId"),
            }
        )
    return results


async def _search_dmhy_fallback(keyword: str):
    """备用源:AnimeGarden连不上时才用dmhy直连,字幕组名靠标题粗略提取,
    准确度不如AnimeGarden,也没有Bangumi编号。"""
    async with httpx.AsyncClient(headers=HEADERS, timeout=15.0) as client:
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
            }
        )
    return results

async def _search_animegarden_by_subject(bgm_id: int, page_size: int = 50):
    """
    直接按Bangumi ID查AnimeGarden,比关键词文本匹配更精确——
    不同字幕组标题写法差异(繁简、有无虚词、罗马音)在这里天然不是问题,
    因为AnimeGarden服务器端已经把这些种子都关联到了同一个bgm_id。
    """
    async with httpx.AsyncClient(headers=HEADERS, timeout=15.0) as client:
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
                "size": item.get("size"),
                "created_at": item.get("createdAt"),
                "bgm_id": item.get("subjectId"),
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



SOURCE_SEARCH_FUNCS = {
    "dmhy": None,  # 占位,下面赋值,避免函数定义顺序问题
    "animegarden": None,
}