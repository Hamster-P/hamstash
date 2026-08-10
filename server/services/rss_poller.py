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
from datetime import datetime

from sqlalchemy.orm import Session

import config_store
import qbittorrent_client
from database import SessionLocal
from models import RssMatchedItem, SubscriptionRule
from sources.base import SearchCriteria, matches_criteria
from sources.registry import get_source, has_source
from services.bgm_series_cache import resolve_series_identity
from services.common import get_setting
from services.staging import staging_folder, upsert_anime_folder


def _criteria_from_rule(rule: SubscriptionRule) -> SearchCriteria:
    """把一条订阅规则翻成搜索过滤条件,喂给唯一权威谓词 matches_criteria——
    搜索接口(服务端过滤)和这里的轮询命中判断从此用同一套判断,保证"看到=下到"。"""
    return SearchCriteria(
        keyword=rule.keyword,
        fansub_name=rule.fansub_name,
        quality=rule.quality,
        subtitle=rule.subtitle,
        format=rule.format,
        release_type=rule.release_type,
    )


async def poll_subscription(db: Session, rule: SubscriptionRule, download_root: str) -> None:
    """轮询一条订阅:交给对应源 adapter 取候选(dmhy/nyaa 抓 RSS Feed、animegarden 走
    search 同源取数),逐条判重 + 过 matches_criteria,命中的直接发磁力链接下载。
    单条 item 处理失败不能影响同一轮里的其他 item。
    """
    # 不管这次轮询最终抓不抓得到/匹配不匹配得到东西,都记一下"尝试过"的时间——
    # 一览页展示的是"上次rss尝试去取得的时间",不是"上次成功命中的时间"。
    rule.last_polled_at = datetime.now()
    db.commit()

    # 禁用某源只是在设置里隐藏它、不再新建订阅,adapter 仍留在注册表里,历史订阅继续轮询。
    # 只有真正未知的 source 字面量(理论上不会出现)才在这里跳过,避免整轮轮询被 KeyError 打断。
    if not has_source(rule.source):
        print(f"[RSS引擎] 订阅 {rule.id} 的数据源 {rule.source} 未知,跳过")
        return
    adapter = get_source(rule.source)

    try:
        items = await adapter.poll(rule)
    except Exception as e:
        print(f"[RSS引擎] 轮询取数失败 subscription={rule.id} source={rule.source}: {e}")
        return
    if not items:
        return

    criteria = _criteria_from_rule(rule)
    folder_title, main_bgm_id, _ = await resolve_series_identity(rule.bgm_id, rule.anime_title)
    staging_folder_path = staging_folder(download_root, folder_title, main_bgm_id)

    for item in items:
        already = (
            db.query(RssMatchedItem)
            .filter(RssMatchedItem.subscription_id == rule.id, RssMatchedItem.guid == item.guid)
            .first()
        )
        if already or not matches_criteria(item, criteria):
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

    # 订阅之间统一加10秒间隔:轮询是顺序执行的,不加间隔的话订阅数量一多,
    # 同一个源(尤其是dmhy)会在很短时间内被连续密集请求,是之前被限流的风险点之一
    # (见config_store.py里rss_poll_interval_seconds默认30分钟的注释)。最后一条
    # 订阅后不用再等,不然只是白白拖长这一轮轮询的收尾时间。
    for idx, rule_id in enumerate(rule_ids):
        try:
            await poll_subscription_task(rule_id, download_root)
        except Exception as e:
            print(f"[RSS引擎] 轮询订阅失败 subscription={rule_id}: {e}")
        if idx < len(rule_ids) - 1:
            await asyncio.sleep(10)


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
