"""对应前端 DownloadPage(下载):种子检索、改名预览、提交下载/建立RSS订阅。"""
from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.orm import Session

import config_store
import rename_engine
import resource_client
import qbittorrent_client
from database import get_db
from models import DownloadTask, SubscriptionRule
from schemas import DownloadRequest, RenamePreviewRequest
from services.common import (
    get_setting,
    resolve_series_identity,
    staging_folder,
    upsert_anime_folder,
)
from services.subscription import activate_subscription_task

router = APIRouter(tags=["下载"])


@router.get("/resources/search")
async def search_resources(keyword: str, source: str = "dmhy", bgm_id: int | None = None):
    results = await resource_client.search_by_source(keyword, source, bgm_id)
    return results


@router.post("/resources/preview-rename")
async def preview_rename_batch(payload: RenamePreviewRequest, db: Session = Depends(get_db)):
    """给选中的种子标题,批量算出改名预览结果。优先使用Bangumi官方中文名命名。"""
    anime_title, _, _ = await resolve_series_identity(payload.bgm_id, payload.anime_title)
    library_root = get_setting(db, "library_root", config_store.DEFAULTS["library_root"])
    previews = [
        rename_engine.preview_rename(anime_title, title, library_root)
        for title in payload.titles
    ]
    return previews


@router.post("/download/execute")
async def execute_download(
    payload: DownloadRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    执行下载:
    - 如果 subscribe=True,先建立/更新一条订阅规则(关联这部番+关键词+字幕组+画质)
    - 对每个选中的种子,计算改名预览(如果开启auto_rename,使用Bangumi官方名),
      推送给qBittorrent,并在 download_task 表里留档。
    """
    anime_title, main_bgm_id, season_title = await resolve_series_identity(
        payload.bgm_id, payload.anime_title
    )

    subscription_id = None
    rss_managed = payload.subscribe  # RSS开关打开时,不走一次性直接下载,交给qBittorrent的RSS轮询处理

    if payload.subscribe:
        download_root = get_setting(db, "download_root", config_store.DEFAULTS["download_root"])
        existing = (
            db.query(SubscriptionRule)
            .filter(SubscriptionRule.keyword == payload.keyword)
            .first()
        )
        if existing:
            existing.fansub_name = payload.fansub_name
            existing.bgm_id = payload.bgm_id  # 继续存"这次提交的季度专属ID",给RSS的subject=过滤用,不是根ID
            existing.source = payload.source
            existing.quality = payload.quality
            existing.subtitle = payload.subtitle
            existing.format = payload.format
            existing.release_type = payload.release_type
            existing.auto_rename = payload.auto_rename
            existing.enabled = True
            db.commit()
            db.refresh(existing)
            rule = existing
        else:
            rule = SubscriptionRule(
                anime_title=anime_title,
                bgm_id=payload.bgm_id,
                keyword=payload.keyword,
                source=payload.source,
                fansub_name=payload.fansub_name,
                quality=payload.quality,
                subtitle=payload.subtitle,
                format=payload.format,
                release_type=payload.release_type,
                auto_rename=payload.auto_rename,
            )
            db.add(rule)
            db.commit()
            db.refresh(rule)

        # 打开RSS开关那一刻,就把这条规则丢进后台去推送给qBittorrent(注册RSS源+自动下载规则),
        # 不阻塞这次HTTP响应——之前"画面显示提交失败,但qBittorrent里其实已经建好"的现象,
        # 大概率就是这几次对qBittorrent的网络请求耗时较长导致前端fetch先中断了。
        subscription_id = rule.id
        background_tasks.add_task(activate_subscription_task, rule.id, download_root)

    created_tasks = []
    if not rss_managed:
        download_root = get_setting(db, "download_root", config_store.DEFAULTS["download_root"])
        staging_folder_path = staging_folder(download_root, anime_title, main_bgm_id)
        upsert_anime_folder(
            db, staging_folder_path, anime_title, main_bgm_id, payload.bgm_id, payload.auto_rename
        )
        for item in payload.items:
            try:
                await qbittorrent_client.add_torrent(
                    magnet=item.magnet, save_path=staging_folder_path
                )
                status = "已推送,等待后台整理任务搬进媒体库"
            except Exception as e:
                status = f"推送失败: {e}"

            task = DownloadTask(
                subscription_id=subscription_id,
                anime_title=anime_title,
                original_title=item.title,
                magnet=item.magnet,
                fansub_name=item.fansub_name,
                target_full_path=None,  # 实际路径由后台整理任务处理完之后才能确定
                status=status,
            )
            db.add(task)
            db.commit()
            db.refresh(task)
            created_tasks.append(
                {
                    "id": task.id,
                    "original_title": task.original_title,
                    "target_full_path": task.target_full_path,
                    "status": task.status,
                }
            )

    return {
        "subscribed": payload.subscribe,
        "subscription_id": subscription_id,
        "tasks": created_tasks,
        "rss_managed": rss_managed,
    }
