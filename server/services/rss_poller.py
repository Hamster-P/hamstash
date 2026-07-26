"""
RSS引擎:取代qBittorrent自身的RSS订阅+自动下载规则机制。

背景:qBittorrent自己的RSS自动下载,对nyaa这类"RSS只给.torrent文件下载链接、
不给磁力链接"的源,匹配命中后还要额外发一次HTTP请求去抓.torrent文件本体,这一步
在部分网络环境下经常连接超时,而且走不走代理完全由qBittorrent自己那套配置决定,
我们控制不了。dmhy/animegarden的RSS虽然直接在enclosure里给了磁力链接,但只要
还依赖qBittorrent自己的RSS订阅树+自动下载规则,就一直有rss_path/rule_name
命名冲突、feed节点错位这类问题(见services/subscription.py/services/rss_rules.py
这几轮踩过的坑)。

这里改成完全由我们自己的后台定时轮询:自己拉RSS、自己判断标题匹配、自己拿到
磁力链接(dmhy/animegarden从enclosure直接读,nyaa用nyaa:infoHash自己拼),
直接调用qbittorrent_client.add_torrent——这条路径等价于"手动下载"一直在用的
那条,不再依赖qBittorrent自己的RSS/规则子系统。

跟services/subscription.py、services/rss_rules.py是两套完全独立的实现,
互不调用——旧的暂时保留、不再被新的订阅创建流程触发,等新机制稳定运行一段时间
之后再统一清理。
"""
import asyncio
import re
from urllib.parse import quote
from xml.etree import ElementTree

import httpx
from sqlalchemy.orm import Session

import config_store
import qbittorrent_client
import resource_client
from database import SessionLocal
from models import RssMatchedItem, SubscriptionRule
from resource_client import _split_keyword
from services.bgm_series_cache import resolve_series_identity
from services.common import get_setting
from services.proxy import get_proxy_url
from services.staging import staging_folder, upsert_anime_folder

_NYAA_NS = {"nyaa": "https://nyaa.si/xmlns/nyaa"}

# 给"自己拼磁力链接"(nyaa)场景用的一批公共tracker——只有info hash的磁力链接
# 理论上纯靠DHT也能找到peer,带上这些能加快首次连上peer的速度,不是必需项。
_PUBLIC_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "http://nyaa.tracker.wf:7777/announce",
]

_SIMPLIFIED_TERMS = ["简", "chs", "gb"]
_TRADITIONAL_TERMS = ["繁", "cht", "big5"]
_JAPANESE_TERMS = ["日", "jp", "japanese"]
_RAW_TERMS = ["raw", "bilibili", "baha", "cr", "crunchyroll", "web-dl"]
_BATCH_TERMS = ["合集", "全集", "batch", "pack", "fin"]
_BATCH_RANGE_RE = re.compile(r"\d+-\d+")


class FeedItem:
    def __init__(self, guid: str, title: str, magnet: str, info_hash: str | None):
        self.guid = guid
        self.title = title
        self.magnet = magnet
        self.info_hash = info_hash


def _build_magnet_from_hash(info_hash: str, title: str) -> str:
    trackers = "&".join(f"tr={quote(t, safe='')}" for t in _PUBLIC_TRACKERS)
    return f"magnet:?xt=urn:btih:{info_hash}&dn={quote(title)}&{trackers}"


def _parse_dmhy_or_animegarden(xml_bytes: bytes) -> list[FeedItem]:
    """dmhy/animegarden的RSS格式一致:<enclosure url="magnet:?...">直接给完整磁力链接。"""
    root = ElementTree.fromstring(xml_bytes)
    items = []
    for item in root.iter("item"):
        title_el = item.find("title")
        guid_el = item.find("guid")
        enclosure_el = item.find("enclosure")
        if title_el is None or guid_el is None or enclosure_el is None:
            continue
        magnet = enclosure_el.get("url")
        if not magnet or not magnet.startswith("magnet:"):
            continue
        items.append(
            FeedItem(
                guid=(guid_el.text or "").strip(),
                title=(title_el.text or "").strip(),
                magnet=magnet,
                info_hash=None,
            )
        )
    return items


def _parse_nyaa(xml_bytes: bytes) -> list[FeedItem]:
    """nyaa的RSS <link> 只给.torrent文件下载链接,但item里有nyaa:infoHash,
    自己拼磁力链接,不需要经过下载.torrent文件这一步。"""
    root = ElementTree.fromstring(xml_bytes)
    items = []
    for item in root.iter("item"):
        title_el = item.find("title")
        guid_el = item.find("guid")
        hash_el = item.find("nyaa:infoHash", _NYAA_NS)
        if title_el is None or guid_el is None or hash_el is None or not hash_el.text:
            continue
        title = (title_el.text or "").strip()
        items.append(
            FeedItem(
                guid=(guid_el.text or "").strip(),
                title=title,
                magnet=_build_magnet_from_hash(hash_el.text.strip(), title),
                info_hash=hash_el.text.strip(),
            )
        )
    return items


def parse_feed_items(source: str, xml_bytes: bytes) -> list[FeedItem]:
    if source == "nyaa":
        return _parse_nyaa(xml_bytes)
    return _parse_dmhy_or_animegarden(xml_bytes)


def _subtitle_matches(title_lower: str, subtitle: str) -> bool:
    has_simplified = any(k in title_lower for k in _SIMPLIFIED_TERMS)
    has_traditional = any(k in title_lower for k in _TRADITIONAL_TERMS)
    has_japanese = any(k in title_lower for k in _JAPANESE_TERMS)
    is_raw = any(k in title_lower for k in _RAW_TERMS)

    if subtitle == "纯简体":
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


def matches_rule(title: str, rule: SubscriptionRule) -> bool:
    """判断一篇RSS文章标题是否命中这条订阅的筛选条件——跟DownloadPage.tsx::
    filteredResults(预览时本地过滤)、services/rss_rules.py::build_must_contain
    (旧实现翻译成qBittorrent正则)是同一套判断标准的第三份实现,三处必须保持一致,
    不然会出现"预览看到的"和"实际下载的"对不上。

    字幕组名称这里统一按大小写不敏感匹配(旧实现services/rss_rules.py里这块是
    大小写敏感的,跟同一文件其他条件的处理方式不一致,是个已知的既有缺陷——这里
    重新实现时直接改成不敏感,不重复这个问题)。
    """
    title_lower = title.lower()

    keywords = _split_keyword(rule.keyword.strip().lower()) if rule.keyword else []
    if keywords and not all(kw in title_lower for kw in keywords if kw):
        return False

    if rule.fansub_name and rule.fansub_name.lower() not in title_lower:
        return False

    if rule.quality and rule.quality.lower() not in title_lower:
        return False

    if rule.format and rule.format.lower() not in title_lower:
        return False

    if rule.subtitle and not _subtitle_matches(title_lower, rule.subtitle):
        return False

    is_batch = any(k in title_lower for k in _BATCH_TERMS) or bool(_BATCH_RANGE_RE.search(title_lower))
    if rule.release_type == "单集(追更)" and is_batch:
        return False
    if rule.release_type == "合集/全集(完结)" and not is_batch:
        return False

    return True


def _build_rss_urls(rule: SubscriptionRule) -> list[str]:
    """跟resource_client.py::search_by_source的animegarden union逻辑保持一致
    (见该函数注释):AnimeGarden不是所有资源都回填了subjectId,只订阅subject版
    的Feed会跟手动搜索一样漏掉这批资源,这里同时订阅subject版+关键词版两个Feed,
    轮询时把两边item合并去重(见poll_subscription),不是二选一。
    """
    if rule.source == "animegarden":
        if rule.bgm_id:
            urls = [resource_client.build_animegarden_rss_url_by_subject(rule.bgm_id, rule.fansub_name)]
            if rule.keyword:
                urls.append(resource_client.build_animegarden_rss_url(rule.keyword, rule.fansub_name))
            return urls
        return [resource_client.build_animegarden_rss_url(rule.keyword, rule.fansub_name)]
    if rule.source == "nyaa":
        return [resource_client.build_nyaa_rss_url(rule.keyword)]
    return [resource_client.build_dmhy_rss_url(rule.keyword)]


async def _fetch_rss(rss_url: str) -> bytes:
    """走跟resource_client.py搜索请求完全一样的代理配置(services/proxy.py::
    get_proxy_url())——手动搜索/下载已经验证这条路径可靠,轮询复用同一套,
    不会重新掉进qBittorrent内部那套代理配置的坑里。"""
    async with httpx.AsyncClient(
        headers=resource_client.HEADERS, timeout=15.0, proxy=get_proxy_url(), follow_redirects=True
    ) as client:
        resp = await client.get(rss_url)
        resp.raise_for_status()
        return resp.content


async def poll_subscription(db: Session, rule: SubscriptionRule, download_root: str) -> None:
    """轮询一条订阅:抓RSS、逐条判重+匹配,命中的直接发磁力链接下载。
    单条item处理失败不能影响同一轮里的其他item。
    """
    rss_urls = _build_rss_urls(rule)
    items: list[FeedItem] = []
    seen_guids: set[str] = set()
    ok_count = 0
    for rss_url in rss_urls:
        try:
            xml_bytes = await _fetch_rss(rss_url)
            # 一条订阅可能同时轮询subject版+关键词版两个Feed(见_build_rss_urls),
            # 同一篇资源可能在两边都出现,按guid去重合并——list顺序里subject版
            # 排在前面,天然让它的那份保留下来。
            for item in parse_feed_items(rule.source, xml_bytes):
                if item.guid not in seen_guids:
                    seen_guids.add(item.guid)
                    items.append(item)
            ok_count += 1
        except Exception as e:
            print(f"[RSS引擎] 拉取/解析RSS失败 subscription={rule.id} url={rss_url}: {e}")
    if ok_count == 0:
        return

    folder_title, main_bgm_id, _ = await resolve_series_identity(rule.bgm_id, rule.anime_title)
    staging_folder_path = staging_folder(download_root, folder_title, main_bgm_id)

    for item in items:
        already = (
            db.query(RssMatchedItem)
            .filter(RssMatchedItem.subscription_id == rule.id, RssMatchedItem.guid == item.guid)
            .first()
        )
        if already or not matches_rule(item.title, rule):
            continue

        try:
            upsert_anime_folder(
                db, staging_folder_path, folder_title, main_bgm_id, rule.bgm_id, rule.auto_rename
            )
            await qbittorrent_client.add_torrent(magnet=item.magnet, save_path=staging_folder_path)
            db.add(RssMatchedItem(
                subscription_id=rule.id, guid=item.guid, info_hash=item.info_hash,
                title=item.title, magnet=item.magnet, download_status="added",
            ))
        except Exception as e:
            db.add(RssMatchedItem(
                subscription_id=rule.id, guid=item.guid, info_hash=item.info_hash,
                title=item.title, magnet=item.magnet, download_status="failed", error=str(e),
            ))
        db.commit()


async def poll_subscription_task(rule_id: int, download_root: str) -> None:
    """后台任务版本:开一个新的独立DB session去轮询单条订阅——提交订阅那一刻
    的"立即触发一次"、以及前端"立即更新"按钮都走这个,不复用请求处理函数里的db
    (请求结束后那个session可能已经被关闭)。"""
    db = SessionLocal()
    try:
        rule = db.query(SubscriptionRule).filter(SubscriptionRule.id == rule_id).first()
        if rule and rule.enabled:
            download_root = download_root or get_setting(
                db, "download_root", config_store.DEFAULTS["download_root"]
            )
            await poll_subscription(db, rule, download_root)
    finally:
        db.close()


async def _poll_all_subscriptions() -> None:
    db = SessionLocal()
    try:
        download_root = get_setting(db, "download_root", config_store.DEFAULTS["download_root"])
        rule_ids = [
            r.id for r in db.query(SubscriptionRule).filter(SubscriptionRule.enabled == True).all()  # noqa: E712
        ]
    finally:
        db.close()

    for rule_id in rule_ids:
        try:
            await poll_subscription_task(rule_id, download_root)
        except Exception as e:
            print(f"[RSS引擎] 轮询订阅失败 subscription={rule_id}: {e}")


async def rss_poll_loop() -> None:
    """常驻后台循环:定期轮询所有启用中的订阅。轮询间隔从设置读取,每轮都重新读一次,
    在设置页改完点保存不用重启就能生效——照抄services/organize.py::organize_loop的模式。
    """
    while True:
        try:
            db = SessionLocal()
            try:
                interval = int(
                    get_setting(
                        db, "rss_poll_interval_seconds", config_store.DEFAULTS["rss_poll_interval_seconds"]
                    )
                )
            finally:
                db.close()
        except Exception:
            interval = int(config_store.DEFAULTS["rss_poll_interval_seconds"])

        try:
            await _poll_all_subscriptions()
        except Exception as e:
            print(f"[RSS引擎] 轮询循环出错: {e}")

        await asyncio.sleep(interval)
