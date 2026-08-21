"""对应前端 DownloadPage(下载):种子检索、改名预览、提交下载/建立RSS订阅。"""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

import config_store
import rename_engine
import resource_client
import qbittorrent_client
from database import get_db
from models import AnimeFamilyCache, DownloadTask, SubscriptionRule
from schemas import DownloadRequest, PrefetchRenameCacheRequest, RenamePreviewRequest
from services.bgm_series_cache import (
    build_season_episode_table,
    clear_group_override,
    prefetch_rename_cache_task,
    resolve_effective_root,
    resolve_series_identity,
    set_group_override,
)
from services.common import get_setting
from services.staging import staging_folder, upsert_anime_folder
from services.rss_poller import poll_subscription_task

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
    release_type: str | None = None,
):
    return await resource_client.search_by_source(
        keyword, source, bgm_id, page,
        fansub_name=fansub_name, quality=quality, subtitle=subtitle, format=format,
        release_type=release_type,
    )


@router.get("/resources/sources")
def list_sources():
    """下载源清单:前端下载页下拉(只取 enabled)、设置页源编辑器(全部,含默认值/覆盖值/启用开关)
    都从这里拿,不再在前端硬编码 dmhy/animegarden/nyaa。"""
    from sources.registry import all_sources
    return {"sources": [a.config_state() for a in all_sources()]}


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

    # 预览必须跟这次提交实际会用的归属一致,否则用户会看到"预览说进A文件夹、
    # 下完却进了B文件夹"。merge_to_family=False时这一部自己就是自己的家族根,
    # 跟execute_download写完覆盖之后resolve_series_identity算出来的结果对齐。
    if payload.merge_to_family:
        folder_bgm_id = resolve_effective_root(db, payload.bgm_id, cached_self.source_bgm_id)
    else:
        folder_bgm_id = payload.bgm_id

    if folder_bgm_id == payload.bgm_id:
        anime_title = cached_self.name or payload.anime_title
    else:
        # 家族根节点自己的那一行缓存了它的官方标题,拿来当anime_title(全家共用的
        # 文件夹名);根节点本身没有独立成行是不该发生的反常状态,兜底退回请求里带的标题。
        cached_root = (
            db.query(AnimeFamilyCache)
            .filter(AnimeFamilyCache.bgm_id == folder_bgm_id)
            .first()
        )
        anime_title = (cached_root.name if cached_root else None) or payload.anime_title

    # 集数偏移量/本季总集数:字幕组常用跨季连续的绝对编号(实测Re:Zero第四季发的是
    # 71~79,而这一季自己只有19集),rename_engine._normalize_absolute_episode靠这两个
    # 参数把它换算回季内编号。以前这里一个都没传,两个参数全走默认值0/None,那个函数
    # 第一行就直接原样返回——**预览显示的集数跟下载后实际落地的集数对不上**,而真正
    # 整理时走的organize._resolve_season_context是传的。
    #
    # 取值口径跟organize那边完全一致(都用"生效的家族根"+cached_self的season_ordinal),
    # 这样预览算出来的就是实际会落地的。build_season_episode_table只读AnimeFamilyCache、
    # 不发网络请求,而能走到这里就说明cached_self已命中缓存、家族数据是热的,
    # 不会给预览引入网络依赖(那正是上面status="pending"分支刻意避开的东西)。
    episode_offset = 0
    season_total_eps = None
    if cached_self.season_ordinal:
        season_info = build_season_episode_table(db, folder_bgm_id).get(
            cached_self.season_ordinal
        )
        if season_info:
            episode_offset = season_info["episode_offset"]
            season_total_eps = season_info["eps"]

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
            episode_offset=episode_offset,
            season_total_eps=season_total_eps,
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
    # "是否并进主系列"必须在解析系列归属之前落库:resolve_series_identity内部会
    # 经由resolve_effective_root读这条覆盖,才能返回"这一部自己就是自己的家族根",
    # 后面的暂存目录/媒体库文件夹名全都由它算出来。
    if payload.bgm_id:
        if payload.merge_to_family:
            # 用户这次选了合并——如果之前拆出去过,这次要把覆盖撤掉恢复自动判定,
            # 否则旧覆盖会继续把它钉在独立状态上,开关看起来"打开了却没生效"。
            clear_group_override(db, payload.bgm_id)
        else:
            set_group_override(db, payload.bgm_id, payload.bgm_id)

    anime_title, main_bgm_id, season_title = await resolve_series_identity(
        db, payload.bgm_id, payload.anime_title
    )

    subscription_id = None
    rss_managed = payload.subscribe  # RSS开关打开时,不走一次性直接下载,交给qBittorrent的RSS轮询处理

    if payload.subscribe:
        download_root = get_setting(db, "download_root", config_store.DEFAULTS["download_root"])
        # 一部番(有bgm_id时按bgm_id,没有就退回按keyword)同时只允许一条生效中的订阅——
        # 不管换源还是换字幕组,都不能让两条订阅同时对着同一部番各自往qBittorrent
        # 推送独立的RSS/自动下载规则,那样命中范围重叠会导致同一集被多次抓取,
        # 而且各自的rss_path/rule_name本来就没有关联,没法互相感知对方的存在。
        if payload.bgm_id:
            existing = (
                db.query(SubscriptionRule)
                .filter(SubscriptionRule.bgm_id == payload.bgm_id)
                .first()
            )
        else:
            existing = (
                db.query(SubscriptionRule)
                .filter(SubscriptionRule.keyword == payload.keyword)
                .first()
            )
        if existing:
            same_config = (
                existing.source == payload.source
                and existing.fansub_name == payload.fansub_name
                and existing.quality == payload.quality
                and existing.subtitle == payload.subtitle
                and existing.format == payload.format
                and existing.release_type == payload.release_type
            )
            if not same_config:
                # 内容不一致代表用户想换源/换字幕组/换筛选条件——不做静默覆盖
                # (会顶掉旧订阅的自动下载规则却没有任何确认,也会在切换的过渡期
                # 造成新旧两条规则同时命中、重复下载),要求用户先手动删除旧订阅。
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"这部番已存在一条生效中的订阅(来源: {existing.source}, "
                        f"字幕组: {existing.fansub_name or '不限'}),如需更换请先在RSS订阅一览页删除旧订阅"
                    ),
                )
            existing.auto_rename = payload.auto_rename
            existing.enabled = True
            if existing.main_bgm_id is None:
                # 迁移前创建的老订阅还没有main_bgm_id,借这次重新提交顺手回填
                existing.main_bgm_id = main_bgm_id
            db.commit()
            db.refresh(existing)
            rule = existing
        else:
            rule = SubscriptionRule(
                anime_title=anime_title,
                bgm_id=payload.bgm_id,
                main_bgm_id=main_bgm_id,
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

        # 打开RSS开关那一刻,后台立即跑一轮抓取+匹配+下载,不等下一个自动轮询周期
        # (见services/rss_poller.py),不阻塞这次HTTP响应。新架构下这里不再需要
        # 往qBittorrent注册任何RSS订阅/规则,后续的新种子由rss_poll_loop自动轮询。
        subscription_id = rule.id
        background_tasks.add_task(poll_subscription_task, rule.id, download_root)

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
