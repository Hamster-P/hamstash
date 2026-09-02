"""编排 bgm_id -> tmdb_id 两跳映射 + TMDB 详情补全,写回 AnimeMetaCache。

两跳:
1. services/id_mapping_cache.py 的 BangumiExtLinker 快照,查出 anidb_id/mal_id
   (顺带也给一个快照自带的tmdb_id,当anidb/mal都查不到arm-server结果时的兜底)。
2. arm_client.py 按 anidb_id(优先)或 mal_id 现查 arm-server,拿到当下更准确的
   tmdb_id——实测快照自带的tmdb_id偶尔是错的(比如把TV剧集的id写成了对应
   电影版的id),arm-server现查结果应该优先采用。

拿到tmdb_id后紧接着调 tmdb_client 把背景图/LOGO/分级/标签/类型/工作室一并
补全写回同一行,不拆两轮请求——这样"resolved"状态就是完全能直接渲染的状态。
"""
import re
from datetime import datetime, timezone

from sqlalchemy.orm import Session

import arm_client
import bangumi_client
import tmdb_client
from models import AnimeMetaCache
from services import id_mapping_cache

# 尝试这么多次仍未解析出tmdb_id就不再自动重试(跨越几轮poll,覆盖1-2个月的
# "新番时序问题"窗口)。用户后续可以在设置页/详情页手动触发重试(暂未实现)。
MAX_RETRY_ATTEMPTS = 10

# 背景图/LOGO/分级等的"解析逻辑"版本,跟bgm_series_cache.SEASON_ALGO_VERSION同一个
# 套路:改动挑图规则(tmdb_client._pick_logo的语言序、backdrop尺寸/候选池、标题
# 搜索兜底策略等)时把这个数+1。已解析(status=resolved)的行会记下当时的版本号,
# 版本落后的行由anime_meta_poller后台按新逻辑重取一次,重取期间前端照旧显示旧图。
#   1 = 最初版
#   2 = LOGO 挑选改为 横向优先,同朝向里再按 日文>中文>英文(见 tmdb_client._pick_logo)
META_RESOLVER_VERSION = 2
_RESOLVER_VERSION_SETTING_KEY = "meta_resolver_version"


def reset_meta_resolver_if_version_changed(db: Session) -> bool:
    """挂在启动路径(db_migrate._refresh_stale_caches)上:解析逻辑版本变过时,
    把之前"彻底放弃"(unresolved_permanent)的行重置为待重试——逻辑升级正是
    "当初查不到的现在也许查得到"的时机。

    已resolved的行这里不动:它们的resolver_version已落后,poller的陈旧行扫描
    和详情页的按需触发会各自把它们按新逻辑重取。只碰anime_meta_cache一张表,
    不清任何缓存、不弹提示。幂等(版本号一致直接返回)。
    """
    from services.common import get_setting, upsert_setting

    try:
        stored = int(get_setting(db, _RESOLVER_VERSION_SETTING_KEY, "0"))
    except (TypeError, ValueError):
        stored = 0

    if stored == META_RESOLVER_VERSION:
        return False

    reset = (
        db.query(AnimeMetaCache)
        .filter(AnimeMetaCache.status == "unresolved_permanent")
        .update(
            {AnimeMetaCache.status: "unresolved_retry", AnimeMetaCache.attempt_count: 0},
            synchronize_session=False,
        )
    )
    db.commit()
    upsert_setting(db, _RESOLVER_VERSION_SETTING_KEY, str(META_RESOLVER_VERSION))
    print(
        f"[ANIME_META] 解析逻辑从v{stored}升到v{META_RESOLVER_VERSION},"
        f"已解析条目将由后台按新逻辑重取;{reset}条永久失败条目重置为待重试"
    )
    return True


def _discard_replaced_images(
    old_backdrop: str | None,
    new_backdrop: str | None,
    old_logo: str | None,
    new_logo: str | None,
) -> None:
    """刷新后挑到了跟原来不同的图,把旧URL的本地图片缓存清掉。
    延迟import避免routers<->services循环依赖。"""
    from routers import media

    for old, new in ((old_backdrop, new_backdrop), (old_logo, new_logo)):
        if old and old != new:
            media.discard_cached_image(old)

_SNAPSHOT_TMDB_ID_RE = re.compile(r"^(tv|movie)/(\d+)(?:/season/(\d+))?")


def _parse_snapshot_tmdb_id(value: str | None) -> tuple[str, int, int | None] | None:
    """BangumiExtLinker快照里的tmdb_id字段形如"tv/209867/season/1"或"movie/1323376"。
    返回(媒体类型, tmdb_id, season)。之前这里只接受tv/前缀、把movie/前缀一律丢弃
    (基于幼女战记案例——快照把应该是剧集的番错配成了电影ID),但这个判断过度纠正了:
    真正的问题是"分类错了",不是"movie前缀不可信"——铃芽之旅这种真正的剧场版,
    movie前缀恰恰是对的,只是resolve_one以前没实现调用电影接口的能力。
    现在两种类型都接受,交给resolve_one按类型调用对应的TMDB接口。"""
    if not value:
        return None
    m = _SNAPSHOT_TMDB_ID_RE.match(value)
    if not m:
        return None
    season = int(m.group(3)) if m.group(3) else None
    return m.group(1), int(m.group(2)), season


def _get_or_create(db: Session, bgm_id: int) -> AnimeMetaCache:
    row = db.query(AnimeMetaCache).filter(AnimeMetaCache.bgm_id == bgm_id).first()
    if row is None:
        row = AnimeMetaCache(bgm_id=bgm_id)
        db.add(row)
    return row


async def resolve_one(db: Session, bgm_id: int, *, is_refresh: bool = False) -> None:
    """is_refresh=True:这一行已经resolved过,只是解析逻辑升级了要按新逻辑重取。
    此时解析失败不把行降级成unresolved(旧图继续给前端显示),失败的行留着落后的
    resolver_version,靠poller的每日退避择机再试;成功则整行覆盖 + 写上新版本号,
    并清掉被换掉的旧图缓存。
    """
    row = _get_or_create(db, bgm_id)
    old_backdrop_url = row.backdrop_url
    old_logo_url = row.logo_url
    # SQLAlchemy的Column(default=0)只在flush时才生效,新建的row此刻attempt_count
    # 还是None,直接+=1会报"NoneType不支持+="——(row.attempt_count or 0)兜底。
    row.attempt_count = (row.attempt_count or 0) + 1
    row.last_attempt_at = datetime.now(timezone.utc)

    snapshot_entry = await id_mapping_cache.lookup(bgm_id)
    anidb_id = None
    mal_id = None
    if snapshot_entry:
        try:
            anidb_id = int(snapshot_entry["anidb_id"]) if snapshot_entry.get("anidb_id") else None
        except (TypeError, ValueError):
            anidb_id = None
        try:
            mal_id = int(snapshot_entry["mal_id"]) if snapshot_entry.get("mal_id") else None
        except (TypeError, ValueError):
            mal_id = None
    if anidb_id:
        row.anidb_id = anidb_id

    resolved_tmdb_id: int | None = None
    resolved_tmdb_season: int | None = None
    resolved_tvdb_id: int | None = None
    # 媒体类型判断优先级:arm-server的media字段(实时查询,更准)→ 快照tmdb_id前缀兜底。
    # 剧场版/OVA要走TMDB的/movie/{id}接口,剧集走/tv/{id}——两个接口的ID命名空间是
    # 分开的,拿电影ID去请求/tv/{id}会404(铃芽之旅就是这么踩坑的)。
    media_kind: str | None = None

    arm_result = None
    try:
        if anidb_id:
            arm_result = await arm_client.query_by_anidb(anidb_id)
        elif mal_id:
            arm_result = await arm_client.query_by_mal(mal_id)
    except Exception as e:
        print(f"[ANIME_META] arm-server查询失败 bgm_id={bgm_id}: {e}")

    if arm_result and arm_result.get("themoviedb"):
        resolved_tmdb_id = int(arm_result["themoviedb"])
        resolved_tmdb_season = arm_result.get("themoviedb-season")
        media_kind = "movie" if (arm_result.get("media") or "").upper() == "MOVIE" else "tv"
        if arm_result.get("thetvdb"):
            resolved_tvdb_id = int(arm_result["thetvdb"])
    elif snapshot_entry:
        parsed = _parse_snapshot_tmdb_id(snapshot_entry.get("tmdb_id"))
        if parsed:
            media_kind, resolved_tmdb_id, resolved_tmdb_season = parsed
        if snapshot_entry.get("tvdb_id"):
            try:
                resolved_tvdb_id = int(snapshot_entry["tvdb_id"])
            except (TypeError, ValueError):
                pass

    # 第三跳:前两跳的ID桥接都查不到时,直接拿原文标题去TMDB搜——TMDB自己的收录
    # 速度比AniDB快,新番当天社区就可能建好条目,而AniDB/Fribb往往要等一阵子
    # (实测"恶女不才，请多关照"这个案例:AniDB还没收录,但TMDB搜索直接命中)。
    # 标题搜索比ID桥接更容易撞车(重制版/同名不同作品),但这里查错顶多是背景图/LOGO
    # 展示错了,不动用户文件本身,风险可接受。
    #
    # 查询用的标题不依赖BangumiExtLinker快照——快照本身可能压根没收录这个bgm_id
    # (比snapshot_entry.get("anidb_id")为空更早一步的缺失,617123就是这种情况,
    # 那样连snapshot_entry.get("name")都拿不到)。Bangumi自己的API对任何已存在的
    # bgm_id都能查到原文标题,是更可靠、且本来就在用的数据源(bangumi_client.py
    # 全项目通用),直接现查一次即可,不用绕道快照。
    if resolved_tmdb_id is None:
        query = None
        year = None
        try:
            bgm_detail = await bangumi_client.get_subject_detail(bgm_id)
            bgm_info = bangumi_client.normalize_bgm_subject(bgm_detail)
            query = bgm_info.get("title_original") or bgm_info.get("title")
            date_str = bgm_info.get("air_date") or ""
            if len(date_str) >= 4 and date_str[:4].isdigit():
                year = int(date_str[:4])
        except Exception as e:
            print(f"[ANIME_META] 查询Bangumi原文标题失败 bgm_id={bgm_id}: {e}")

        search_hit = None
        if query:
            try:
                search_hit = await tmdb_client.search_tv(query, year)
                if search_hit:
                    media_kind = "tv"
                else:
                    search_hit = await tmdb_client.search_movie(query, year)
                    if search_hit:
                        media_kind = "movie"
            except Exception as e:
                print(f"[ANIME_META] TMDB标题搜索失败 bgm_id={bgm_id} query={query!r}: {e}")

        if search_hit:
            resolved_tmdb_id = search_hit["id"]
            resolved_tmdb_season = None

    if resolved_tmdb_id is None:
        if not is_refresh:
            row.status = (
                "unresolved_permanent" if row.attempt_count >= MAX_RETRY_ATTEMPTS else "unresolved_retry"
            )
        # is_refresh: 保持旧行原样(status/URL/resolver_version都不动),
        # last_attempt_at已在开头更新,由poller的每日退避择机重试。
        return

    row.tmdb_id = resolved_tmdb_id
    row.tmdb_season = resolved_tmdb_season
    row.tvdb_id = resolved_tvdb_id
    row.media_type = media_kind

    try:
        if media_kind == "movie":
            detail = await tmdb_client.get_movie_detail(resolved_tmdb_id)
            normalized = tmdb_client.normalize_tmdb_movie(detail)
        else:
            detail = await tmdb_client.get_tv_detail(resolved_tmdb_id, resolved_tmdb_season)
            normalized = tmdb_client.normalize_tmdb_tv(detail)
    except Exception as e:
        print(f"[ANIME_META] TMDB详情请求失败 bgm_id={bgm_id} tmdb_id={resolved_tmdb_id} kind={media_kind}: {e}")
        if not is_refresh:
            row.status = (
                "unresolved_permanent" if row.attempt_count >= MAX_RETRY_ATTEMPTS else "unresolved_retry"
            )
        # is_refresh: 旧图/旧status保持不变,择机重试。
        return

    row.backdrop_url = normalized["backdrop_url"]
    row.logo_url = normalized["logo_url"]
    row.content_rating = normalized["content_rating"]
    row.genres = ",".join(normalized["genres"])
    row.tags = ",".join(normalized["tags"])
    row.studios = ",".join(normalized["studios"])
    row.creators = ",".join(normalized["creators"])
    row.status = "resolved"
    row.resolved_at = datetime.now(timezone.utc)
    row.resolver_version = META_RESOLVER_VERSION

    # 按新逻辑挑到了跟原来不同的背景图/LOGO时,清掉旧URL的孤儿图片缓存。
    if is_refresh:
        _discard_replaced_images(
            old_backdrop_url, row.backdrop_url, old_logo_url, row.logo_url
        )
