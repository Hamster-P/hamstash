"""
RSS订阅规则的推送/撤回逻辑:把 SubscriptionRule 同步成
qBittorrent 里的 RSS 源 + 自动下载规则。

被 download 路由(提交下载时打开RSS开关)和 rss 路由
(订阅一览页手动开关)共用。
"""
from sqlalchemy.orm import Session

import qbittorrent_client
import resource_client
from database import SessionLocal
from models import SubscriptionRule
from services.bgm_series_cache import resolve_series_identity
from services.common import sanitize_path_segment
from services.staging import RSS_FOLDER, staging_folder, upsert_anime_folder

async def activate_subscription(db: Session, rule: SubscriptionRule, download_root: str) -> None:
    """
    把这条订阅规则真正推送给qBittorrent:
    注册RSS订阅源(没有就新建),再写入/更新对应的自动下载规则。
    任何失败都记录到rule.last_error,不阻断主流程(下载本身已经成功了)。
    """
    from services.rss_rules import build_rss_rule_def

    if not rule.rss_url:
        if rule.source == "animegarden":
            if rule.bgm_id:
                rule.rss_url = resource_client.build_animegarden_rss_url_by_subject(
                    rule.bgm_id, rule.fansub_name # 继续用季度专属ID,保证RSS只订阅这一季
                )
            else:
                rule.rss_url = resource_client.build_animegarden_rss_url(
                    rule.keyword, rule.fansub_name
                )
        elif rule.source == "nyaa":
            rule.rss_url = resource_client.build_nyaa_rss_url(rule.keyword)
        else:
            rule.rss_url = resource_client.build_dmhy_rss_url(rule.keyword)
    if not rule.rss_path:
        rule.rss_path = f"{RSS_FOLDER}\\{rule.id}-{sanitize_path_segment(rule.anime_title)}"
    if not rule.rule_name:
        rule.rule_name = f"anime-hub-{rule.id}"

    # rule.anime_title在建立订阅那一刻可能还是某一季的名字(比如"无职转生 第三季"),
    # 这里重新解析一次系列根信息,保证文件夹归属跟手动下载路径用的是同一套逻辑,
    # 不会出现"同一部番,手动下载和RSS各建一个文件夹"的情况。
    folder_title, main_bgm_id, _ = await resolve_series_identity(rule.bgm_id, rule.anime_title)
    staging_folder_path = staging_folder(download_root, folder_title, main_bgm_id)
    upsert_anime_folder(
        db, staging_folder_path, folder_title, main_bgm_id, rule.bgm_id, rule.auto_rename
    )
    
    try:
        await qbittorrent_client.ensure_category("anime-hub")
        await qbittorrent_client.add_rss_folder(RSS_FOLDER)
        await qbittorrent_client.add_rss_feed(rule.rss_url, rule.rss_path)
        await qbittorrent_client.set_rss_auto_download_rule(
            rule.rule_name, build_rss_rule_def(rule, staging_folder_path)
        )
        rule.last_error = None
    except Exception as e:
        rule.last_error = f"激活失败: {e}"

    db.commit()
    db.refresh(rule)


async def deactivate_subscription(db: Session, rule: SubscriptionRule) -> None:
    """关闭RSS开关:只撤下自动下载规则,RSS订阅源本身留着(方便随时重新打开)。"""
    if rule.rule_name:
        try:
            await qbittorrent_client.remove_rss_auto_download_rule(rule.rule_name)
            rule.last_error = None
        except Exception as e:
            rule.last_error = f"关闭失败: {e}"

    db.commit()
    db.refresh(rule)


async def activate_subscription_task(rule_id: int, download_root: str) -> None:
    """
    后台任务版本:开一个新的独立DB session去激活订阅,
    不复用请求处理函数里的db(请求结束后那个session可能已经被关闭)。
    """
    db = SessionLocal()
    try:
        rule = db.query(SubscriptionRule).filter(SubscriptionRule.id == rule_id).first()
        if rule:
            await activate_subscription(db, rule, download_root)
    finally:
        db.close()
