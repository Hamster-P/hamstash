"""
Bangumi系列/季度解析结果的持久化缓存 + 后台预热。

resolve_family_season_map()是一次不便宜的图遍历(见bangumi_family.py)，这里把结果
缓存进AnimeFamilyCache表，命中就不用再发网络请求；下载页一打开就会触发后台预热
(prefetch_rename_cache_task)，等真正要用的时候大概率已经是热缓存。
"""
from sqlalchemy.orm import Session

import rename_engine
from database import SessionLocal
from models import AnimeFamilyCache


async def resolve_series_identity(bgm_id: int | None, fallback_title: str):
    """
    返回 (folder_title, main_bgm_id, season_hint_text):
    - folder_title/main_bgm_id: 系列最早一部的名字/ID,决定文件夹归属,
      不管提交时是第几季,同系列都落进同一个文件夹。
    - season_hint_text: 这一季自己在Bangumi的官方名字,专门给季度文字判断用
      (根条目的名字通常不带"第几季"这种信息)。
    """
    import bangumi_client  # 延迟导入,避免模块加载顺序问题
    import bangumi_family

    if not bgm_id:
        return fallback_title, None, fallback_title

    try:
        season_detail = await bangumi_client.get_subject_detail(bgm_id)
        season_title = season_detail.get("name_cn") or season_detail.get("name") or fallback_title
    except Exception:
        season_title = fallback_title

    try:
        main_id = await bangumi_family.resolve_root_subject_id(bgm_id)
        main_detail = await bangumi_client.get_subject_detail(main_id)
        folder_title = main_detail.get("name_cn") or main_detail.get("name") or season_title
    except Exception:
        main_id, folder_title = bgm_id, season_title

    return folder_title, main_id, season_title


async def resolve_tv_season_ordinal_cached(
    db: Session, season_bgm_id: int | None, main_bgm_id: int | None
) -> str | None:
    """
    bangumi_family.resolve_family_season_map()的持久化缓存包装:按bgm_id查
    AnimeFamilyCache表,命中直接返回season_ordinal,不发任何网络请求;没命中
    (这个bgm_id从没被算过——可能是全新番,也可能是老系列新出的剧场版/新一季)
    才触发一次完整的家族重新计算,并把整个家族的全部成员一次性写回缓存表
    (不止写被问到的这一个,顺带把家族里其他成员的结果也刷新一遍,自然覆盖
    "系列新增了一部作品"这种场景,不需要额外的过期/失效机制)。

    缓存没有TTL——这类关联家族的变动率很低(一部番一年也就新增1~2个成员),
    也没有证据表明Bangumi已收录条目的platform/集数/关联关系会事后变化,
    命中缓存直接用是安全的;真遇到需要强制刷新的场景,手动删掉对应行即可。
    """
    if not season_bgm_id or not main_bgm_id:
        return None

    cached = db.query(AnimeFamilyCache).filter(AnimeFamilyCache.bgm_id == season_bgm_id).first()
    if cached:
        return cached.season_ordinal

    import bangumi_family  # 延迟导入,避免跟bangumi_family反过来import本模块形成循环import

    family_map = await bangumi_family.resolve_family_season_map(main_bgm_id)
    for bid, info in family_map.items():
        # folder_bucket只是给人查表用的展示字段,用跟preview_rename_file()同一个
        # resolve_folder_bucket()算,保证不会出现"缓存表说进A桶、实际文件却进了B桶"的错位。
        media_type = rename_engine.classify_media_type("", info.get("platform"))
        folder_bucket = rename_engine.resolve_folder_bucket(
            media_type, main_bgm_id, info.get("season_ordinal")
        )

        row = db.query(AnimeFamilyCache).filter(AnimeFamilyCache.bgm_id == bid).first()
        if row is None:
            row = AnimeFamilyCache(bgm_id=bid)
            db.add(row)
        row.source_bgm_id = main_bgm_id
        row.name = info.get("name") or ""
        row.date = info.get("date")
        row.platform = info.get("platform")
        row.eps = info.get("eps")
        row.total_episodes = info.get("total_episodes")
        row.season_ordinal = info.get("season_ordinal")
        row.folder_bucket = folder_bucket
    db.commit()

    result = family_map.get(season_bgm_id)
    return result["season_ordinal"] if result else None


# 进程内存,防止同一个bgm_id短时间内被并发触发多次重复的家族预热计算——
# 只是节流,不是正确性保证:即使真的撞了并发,两次算出来的结果应该一致,
# 顶多白打一遍Bangumi API,不会产生错误数据。进程重启后自然清空,不需要持久化。
_prefetching_bgm_ids: set[int] = set()


async def prefetch_rename_cache_task(bgm_id: int, anime_title: str) -> None:
    """
    后台任务版本:下载页一打开(带着bgm_id进来的场景)就调用,提前把这部番的
    家族解析结果算出来、写进AnimeFamilyCache——开一个新的独立DB session,
    不复用请求处理函数里的db(请求结束后那个session可能已经被关闭),
    跟services/rss_poller.py的poll_subscription_task是同一种模式。

    用户在下载页搜索/选种子/切换设置的这段时间里,这个后台任务在悄悄把结果
    算好;等用户真正点了预览、或者种子下载完触发organize_loop整理时,
    大概率已经是热缓存,不用再等——这是"两层"设计里负责后台预热的那一层,
    另一层(命中判断+pending状态展示)在/resources/preview-rename里。
    """
    if bgm_id in _prefetching_bgm_ids:
        return
    _prefetching_bgm_ids.add(bgm_id)
    db = SessionLocal()
    try:
        _, main_bgm_id, _ = await resolve_series_identity(bgm_id, anime_title)
        if main_bgm_id:
            await resolve_tv_season_ordinal_cached(db, bgm_id, main_bgm_id)
    except Exception as e:
        print(f"[PREFETCH] 预热改名缓存失败 bgm_id={bgm_id}: {e}")
    finally:
        db.close()
        _prefetching_bgm_ids.discard(bgm_id)
