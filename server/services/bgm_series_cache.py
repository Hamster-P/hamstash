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


async def resolve_series_identity(db: Session, bgm_id: int | None, fallback_title: str):
    """
    返回 (folder_title, main_bgm_id, season_hint_text):
    - folder_title/main_bgm_id: 系列最早一部的名字/ID,决定文件夹归属,
      不管提交时是第几季,同系列都落进同一个文件夹。
    - season_hint_text: 这一季自己在Bangumi的官方名字,专门给季度文字判断用
      (根条目的名字通常不带"第几季"这种信息)。

    优先查AnimeFamilyCache:这个bgm_id之前被任何流程(下载/预热/修复媒体库/补番)
    算过关联家族的话,直接从缓存拼结果,不发任何网络请求。没命中(全新出现的
    bgm_id,比如老番出了新剧场版/新一集)才现查一次完整家族树,顺带把这个根节点
    下的旧缓存整体清空重写——不是零散地增量insert,保证缓存跟着Bangumi最新收录
    状态刷新(柯南这类长篇新增成员/剧场版能被发现)。

    极端情况:这次现查算出来的根,如果跟这个bgm_id之前(在另一个根名下)已经
    缓存过的记录不一致——AnimeFamilyCache.bgm_id在表结构上是唯一约束
    (models.py::AnimeFamilyCache.bgm_id unique=True),不可能让两份缓存物理并存,
    所以是"后写覆盖":_persist_family_map按bgm_id匹配已有行,这次算出的新根会
    直接把旧根名下那一行的source_bgm_id改写过来,相当于把这个bgm_id从旧家族
    "认领"进新家族,不会报错也不会留下两条记录。
    """
    if not bgm_id:
        return fallback_title, None, fallback_title

    cached_self = db.query(AnimeFamilyCache).filter(AnimeFamilyCache.bgm_id == bgm_id).first()
    if cached_self:
        main_bgm_id = cached_self.source_bgm_id
        season_title = cached_self.name or fallback_title
        cached_root = (
            db.query(AnimeFamilyCache)
            .filter(AnimeFamilyCache.bgm_id == main_bgm_id)
            .first()
        )
        folder_title = (cached_root.name if cached_root else None) or season_title
        return folder_title, main_bgm_id, season_title

    import bangumi_client  # 延迟导入,避免模块加载顺序问题
    import bangumi_family

    try:
        season_detail = await bangumi_client.get_subject_detail(bgm_id)
        season_title = season_detail.get("name_cn") or season_detail.get("name") or fallback_title
    except Exception:
        season_title = fallback_title

    try:
        main_id = await bangumi_family.resolve_root_subject_id(bgm_id)
        family_map = await bangumi_family.resolve_family_season_map(main_id)
        if family_map:
            # family_map为空代表这次网络彻底失败,不删不写,保留旧缓存原样——
            # 避免因为这一次请求失败反而把之前好好的缓存清空。
            db.query(AnimeFamilyCache).filter(
                AnimeFamilyCache.source_bgm_id == main_id
            ).delete(synchronize_session=False)
            _persist_family_map(db, main_id, family_map)
        main_detail = await bangumi_client.get_subject_detail(main_id)
        folder_title = main_detail.get("name_cn") or main_detail.get("name") or season_title
    except Exception:
        main_id, folder_title = bgm_id, season_title

    return folder_title, main_id, season_title


def _persist_family_map(db: Session, main_bgm_id: int, family_map: dict) -> None:
    """把resolve_family_season_map()算出来的整个家族结果写入AnimeFamilyCache——
    从resolve_tv_season_ordinal_cached里抽出来的公共部分,resolve_related_family_ids_cached
    (补番功能用)/resolve_series_identity也要用同一段upsert逻辑,不重复写一遍。

    按bgm_id匹配已有行(models.py::AnimeFamilyCache.bgm_id在表结构上是唯一约束,
    物理上不可能给同一个bgm_id留两行)——同一个bgm_id如果之前在另一个家族(不同的
    source_bgm_id)下已经有缓存行,这次直接把那一行的source_bgm_id改写成新算出的
    根,相当于把它从旧家族"认领"进新家族,后写覆盖,不报错也不留脏行。
    """
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


def _member_eps(row: AnimeFamilyCache) -> int:
    """一个家族成员的正片集数:优先用Bangumi原始的eps字段,为0/空时才退回total_episodes。

    实测total_episodes会把特典/SP也算进去、比实际正片偏大(咒术回战第一季
    eps=24但total_episodes=25、实际正片24集;无职转生第一季两个cour的eps
    相加11+12=23正好是实际集数,total_episodes相加12+14=26就偏大了),用它
    算集数偏移量会得出E00这种错误结果。反过来eps对连载中的番经常是0,
    这时候只能退回total_episodes。
    """
    return row.eps or row.total_episodes or 0


def build_season_episode_table(db: Session, main_bgm_id: int) -> dict[str, dict]:
    """按season_ordinal汇总这部番每一季的集数信息,返回
    season_ordinal -> {eps, episode_offset, cour_count, platform, name, date}:

    - eps: 这一季自己总共多少集(同一季拆成多个cour播出时,各cour相加)
    - episode_offset: 排在这一季之前的所有季累计多少集,用来把字幕组的跨季绝对
      编号换算回季内编号(见rename_engine._normalize_absolute_episode)
    - cour_count: 这一季拆成了几个cour播出,给"按第几个cour顺序编号的老目录结构
      反推真实季度"用(见services/library_repair.py::_resolve_effective_season)
    - platform/name/date: 取这一季内首播最早的那个成员,保证season_hint拿到的是
      "第二季"这种季度本名而不是"第二季 第2クール",且结果不受数据库行顺序影响

    只统计platform=="TV"且有season_ordinal的成员——剧场版/OVA/够不上真季的旁支
    不参与季度集数计数,它们本来就不套SxxExx编号。
    """
    rows = (
        db.query(AnimeFamilyCache)
        .filter(
            AnimeFamilyCache.source_bgm_id == main_bgm_id,
            AnimeFamilyCache.platform == "TV",
            AnimeFamilyCache.season_ordinal.isnot(None),
        )
        .all()
    )

    grouped: dict[str, list[AnimeFamilyCache]] = {}
    for row in rows:
        grouped.setdefault(row.season_ordinal, []).append(row)

    table: dict[str, dict] = {}
    cumulative = 0
    for ordinal in sorted(grouped):
        members = sorted(grouped[ordinal], key=lambda r: (r.date or "", r.bgm_id))
        season_eps = sum(_member_eps(m) for m in members)
        representative = members[0]
        table[ordinal] = {
            "eps": season_eps,
            "episode_offset": cumulative,
            "cour_count": len(members),
            "platform": representative.platform,
            "name": representative.name,
            "date": representative.date,
        }
        cumulative += season_eps
    return table


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
    _persist_family_map(db, main_bgm_id, family_map)

    result = family_map.get(season_bgm_id)
    return result["season_ordinal"] if result else None


async def resolve_related_family_ids_cached(db: Session, bgm_id: int) -> list[int]:
    """
    "补番"功能专用:返回bgm_id所在系列的全部相关bgm_id列表,优先走AnimeFamilyCache,
    但会做一轮轻量的"有没有新成员"核实(bangumi_family.has_new_family_members),
    不会为了省网络请求而漏掉真正新出的续作/剧场版——纯信旧缓存就违背了"补番"这个
    功能存在的意义。

    策略:
    1. 查AnimeFamilyCache有没有这个bgm_id的记录。
       - 没有(这部番从没被任何流程算过关联家族,理论上补番按钮很少遇到——按钮只在
         本地已下载/匹配的番剧详情页出现,而下载流程本来就会预热这张缓存表,但仍要
         兜底):走全量网络路径(resolve_root_subject_id + resolve_family_season_map),
         写入缓存,返回全部成员id。
       - 有:拿到source_bgm_id(家族根)+ 这个根下缓存里已知的全部成员id集合。
    2. 对已知成员集合跑一轮has_new_family_members。
       - 没发现新成员:直接返回缓存里的成员id列表,这次请求全程零次
         resolve_root_subject_id/resolve_family_season_map调用。
       - 发现新成员:触发一次完整的resolve_family_season_map(source_bgm_id)重算并
         写回缓存,返回重算后的全部成员id;重算本身失败(网络问题)时退回已知的
         旧缓存,不让这次请求整个失败。
    """
    import bangumi_family  # 延迟导入,避免跟bangumi_family反过来import本模块形成循环import

    cached = db.query(AnimeFamilyCache).filter(AnimeFamilyCache.bgm_id == bgm_id).first()

    if not cached:
        root_id = await bangumi_family.resolve_root_subject_id(bgm_id)
        family_map = await bangumi_family.resolve_family_season_map(root_id)
        if family_map:
            _persist_family_map(db, root_id, family_map)
            return list(family_map.keys())
        return [root_id]

    main_bgm_id = cached.source_bgm_id
    known_rows = (
        db.query(AnimeFamilyCache).filter(AnimeFamilyCache.source_bgm_id == main_bgm_id).all()
    )
    known_ids = {row.bgm_id for row in known_rows}

    grown = await bangumi_family.has_new_family_members(known_ids)
    if not grown:
        return list(known_ids)

    family_map = await bangumi_family.resolve_family_season_map(main_bgm_id)
    if family_map:
        _persist_family_map(db, main_bgm_id, family_map)
        return list(family_map.keys())
    return list(known_ids)  # 重算失败(网络问题),退回已知的旧缓存,好歹能看到点东西


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
        _, main_bgm_id, _ = await resolve_series_identity(db, bgm_id, anime_title)
        if main_bgm_id:
            await resolve_tv_season_ordinal_cached(db, bgm_id, main_bgm_id)
    except Exception as e:
        print(f"[PREFETCH] 预热改名缓存失败 bgm_id={bgm_id}: {e}")
    finally:
        db.close()
        _prefetching_bgm_ids.discard(bgm_id)
