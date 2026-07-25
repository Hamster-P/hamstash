"""对应前端 DownloadPage(下载):种子检索、改名预览、提交下载/建立RSS订阅。"""
from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.orm import Session

import config_store
import rename_engine
import resource_client
import qbittorrent_client
from database import get_db
from models import AnimeFamilyCache, DownloadTask, SubscriptionRule
from schemas import DownloadRequest, PrefetchRenameCacheRequest, RenamePreviewRequest
from services.bgm_series_cache import prefetch_rename_cache_task, resolve_series_identity
from services.common import get_setting
from services.staging import staging_folder, upsert_anime_folder
from services.subscription import activate_subscription_task

router = APIRouter(tags=["下载"])


@router.get("/resources/search")
async def search_resources(
    keyword: str,
    source: str = "dmhy",
    bgm_id: int | None = None,
    page: int = 1,
    fansub_name: str | None = None,
    quality: str | None = None,
    subtitle: str | None = None,
    format: str | None = None,
):
    return await resource_client.search_by_source(
        keyword, source, bgm_id, page,
        fansub_name=fansub_name, quality=quality, subtitle=subtitle, format=format,
    )


@router.post("/resources/prefetch-rename-cache")
async def prefetch_rename_cache(payload: PrefetchRenameCacheRequest, background_tasks: BackgroundTasks):
    """
    下载页一打开(带着bgm_id进来的场景,比如从详情页跳转过来)就调用一次,
    后台把这部番的家族改名规则提前算好、写进AnimeFamilyCache缓存表——用户
    还在搜索/选种子的这段时间里,计算已经在悄悄进行,等真正点开预览或者
    种子下载完触发整理时大概率已经是热缓存,不用再等。

    不返回计算结果本身(前端要看结果走/resources/preview-rename,那边负责
    读缓存);没有bgm_id时什么都不用做,直接确认收到。
    """
    if payload.bgm_id:
        background_tasks.add_task(prefetch_rename_cache_task, payload.bgm_id, payload.anime_title)
    return {"status": "accepted"}


@router.post("/resources/preview-rename")
async def preview_rename_batch(
    payload: RenamePreviewRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    给选中的种子标题,批量算出改名预览结果。

    有bgm_id时优先查AnimeFamilyCache缓存表,不摸网络:命中就用缓存里的
    platform/season_ordinal生成预览,跟organize_loop真正改名时用的是
    同一套结果,不会对不上。没命中(这个bgm_id还没被/resources/prefetch-rename-cache
    或之前的整理任务解析过)不在这里同步现算、阻塞用户的交互操作,返回
    status="pending"让前端自己决定怎么显示("规则计算中"之类),同时顺手
    在后台补一次预热请求,不需要一直等前端来触发。

    没有bgm_id(纯关键词搜索、没关联具体的Bangumi条目)沿用原来的纯文本
    猜测,这条路径从来没有网络依赖,不存在pending的概念。
    """
    library_root = get_setting(db, "library_root", config_store.DEFAULTS["library_root"])

    if not payload.bgm_id:
        previews = [
            rename_engine.preview_rename(payload.anime_title, title, library_root)
            for title in payload.titles
        ]
        return {"status": "ready", "previews": previews}

    cached_self = (
        db.query(AnimeFamilyCache).filter(AnimeFamilyCache.bgm_id == payload.bgm_id).first()
    )
    if not cached_self:
        background_tasks.add_task(prefetch_rename_cache_task, payload.bgm_id, payload.anime_title)
        return {"status": "pending", "previews": []}

    # 家族根节点自己的那一行缓存了它的官方标题,拿来当anime_title(全家共用的
    # 文件夹名);根节点本身没有独立成行是不该发生的反常状态,兜底退回请求里带的标题。
    cached_root = (
        db.query(AnimeFamilyCache)
        .filter(AnimeFamilyCache.bgm_id == cached_self.source_bgm_id)
        .first()
    )
    anime_title = (cached_root.name if cached_root else None) or payload.anime_title
    folder_bgm_id = cached_self.source_bgm_id

    previews = [
        rename_engine.preview_rename_file(
            anime_title=anime_title,
            file_name=title,
            torrent_title=title,
            library_root=library_root,
            bgm_id=folder_bgm_id,
            season_hint=cached_self.name,
            season_ordinal=cached_self.season_ordinal,
            platform=cached_self.platform,
        )
        for title in payload.titles
    ]
    return {"status": "ready", "previews": previews}


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
