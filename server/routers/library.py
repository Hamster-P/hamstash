"""对应前端 LibraryPage(影视库):本地媒体库扫描、番剧匹配、播放记录、播放。"""
import os
import re
import glob
import platform
import asyncio
import shutil
from pathlib import Path
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import func
from sqlalchemy.orm import Session
from pydantic import BaseModel

import models
import rename_engine
import config_store
from database import SessionLocal, get_db
from services.common import get_setting, upsert_setting
from services import bgm_series_cache, anime_meta_resolver
from bangumi_client import get_subject_detail, normalize_bgm_subject, get_subject_details_batch
from datetime import datetime, timedelta, timezone

router = APIRouter(tags=["影视库"])

LIBRARY_SORT_MODES = {"default", "recent_watched", "recent_updated"}


class SortModeUpdate(BaseModel):
    mode: str


@router.get("/library/sort-mode")
def get_library_sort_mode(db: Session = Depends(get_db)):
    """影视库列表页排序方式记忆:重新打开页面时恢复上次选择,而不是每次都回到默认。"""
    return {"mode": get_setting(db, "library_sort_mode", config_store.DEFAULTS["library_sort_mode"])}


@router.put("/library/sort-mode")
def set_library_sort_mode(req: SortModeUpdate, db: Session = Depends(get_db)):
    if req.mode not in LIBRARY_SORT_MODES:
        raise HTTPException(status_code=400, detail=f"未知排序方式: {req.mode}")
    upsert_setting(db, "library_sort_mode", req.mode)
    config_store.update_ini_value("library_sort_mode", req.mode)
    return {"mode": req.mode}

# 跟rename_engine.VIDEO_EXTS共用同一份后缀清单(只是这边要带"."前缀跟Path.suffix比较),
# 不再各自维护一份、之后加格式两处都要改——之前这里漏了m2t等格式,导致AT-X台标注的
# .m2t录播文件在影视库详情页里"未找到符合格式的视频文件"。
VIDEO_EXTENSIONS = {f".{ext}" for ext in rename_engine.VIDEO_EXTS}

# 详情接口的结构缓存:folder_name -> (签名, structure)。签名 = (番剧根目录mtime, 各直接
# 子目录mtime的frozenset),由一次os.scandir算出;签名没变就复用缓存、跳过整棵目录树的遍历,
# 重复点同一部番时几乎瞬开。进程内缓存,重启即清空——无需持久化。
# (缓存的structure是"纯磁盘结构",不含is_watched等观看态,那些在端点里每次现查后叠加。)
_detail_structure_cache: dict[str, tuple] = {}


def _folder_structure_signature(anime_path: Path):
    """一次scandir算出目录签名,用于判断缓存是否失效。目录不存在返回None。"""
    try:
        with os.scandir(anime_path) as it:
            sub_mtimes = frozenset(
                entry.stat().st_mtime for entry in it if entry.is_dir()
            )
    except (FileNotFoundError, NotADirectoryError):
        return None
    try:
        root_mtime = anime_path.stat().st_mtime
    except OSError:
        return None
    return (root_mtime, sub_mtimes)


def _serialize_signature(signature) -> str | None:
    """把_folder_structure_signature()的(root_mtime, frozenset)元组序列化成可存进数据库/
    做字符串比较的形式。目录不存在(signature=None)时返回None。"""
    if signature is None:
        return None
    root_mtime, sub_mtimes = signature
    return f"{root_mtime}:" + ",".join(sorted(f"{m}" for m in sub_mtimes))
# ----------------- 数据模型定义 -----------------
class WatchedRequest(BaseModel):
    folder_name: str
    filename: str
    # 相对 library_root 的正斜杠路径(前端从详情页 ep / mpv 上报路径拿得到)。给"未看集数"
    # 角标的已看分子做增量用——判断这个文件在不在正片桶。缺省时不做增量,交给下次重扫。
    rel_path: str | None = None


def _rel_path_is_real_episode(db: Session, media: "models.LocalMedia | None", rel_path: str) -> bool:
    """这个 rel_path 指向的文件算不算"正片"(计入"未看集数"角标分母/分子的口径)。
    跟 delete_episode 里那段判断同一套:直接堆在番剧根目录的视频算正片;在子目录里的
    按 _bucket_name_for_subdir 归桶,不在 _NON_EPISODE_BUCKETS 才算。"""
    rel_parts = _norm_rel(rel_path).split("/")
    if len(rel_parts) < 3:  # <folder>/<file> —— 散在根目录,算正片
        return True
    family_root = None
    if media and media.bgm_id:
        family_root = bgm_series_cache.cached_auto_root(db, media.bgm_id) or media.bgm_id
    extra_buckets = bgm_series_cache.family_work_title_buckets(db, family_root)
    return _bucket_name_for_subdir(rel_parts[1], extra_buckets) not in _NON_EPISODE_BUCKETS


# ----------------- 播放记录 API -----------------
def _bump_watched_episode_count(db: Session, req: "WatchedRequest", delta: int) -> None:
    """"未看集数"角标的已看分子(LocalMedia.watched_episode_count)增量维护。
    看 / 取消看是纯 DB 事件、不改磁盘目录签名,backfill 那套"签名变了才重扫"检测不到,
    所以必须在这里就地 ±1,不然角标要等用户下次进详情页才更新。
    只有:rel_path 给了 + 是正片桶文件 + 两列都已扫过(非 None) 才动。"""
    if not req.rel_path:
        return
    media = (
        db.query(models.LocalMedia)
        .filter(models.LocalMedia.folder_name == req.folder_name)
        .first()
    )
    if not media or media.watched_episode_count is None or media.episode_file_count is None:
        return
    if not _rel_path_is_real_episode(db, media, req.rel_path):
        return
    new_val = media.watched_episode_count + delta
    media.watched_episode_count = max(0, min(new_val, media.episode_file_count))
    media.episode_count_updated_at = datetime.now()


@router.post("/library/watch")
def mark_as_watched(req: WatchedRequest, db: Session = Depends(get_db)):
    """标记为已播放，如果已存在则更新最后观看时间"""
    record = db.query(models.PlaybackRecord).filter(
        models.PlaybackRecord.folder_name == req.folder_name,
        models.PlaybackRecord.filename == req.filename
    ).first() #[cite: 15]

    if not record:
        db.add(models.PlaybackRecord(
            folder_name=req.folder_name,
            filename=req.filename,
            watched_at=datetime.now(),
        ))
        _bump_watched_episode_count(db, req, +1)  # 新增一条已看记录 → 分子 +1
    else:
        record.watched_at = datetime.now()
        if record.is_stale:
            # 之前判过"文件没了"、现在又在放 → 说明文件回来了,记录重新计入已看
            record.is_stale = False
            _bump_watched_episode_count(db, req, +1)

    db.commit() #[cite: 15]
    return {"status": "success", "message": "Marked as watched"}

@router.post("/library/unwatch")
def unmark_watched(req: WatchedRequest, db: Session = Depends(get_db)):
    """取消已播放标记（用于误触恢复）"""
    row = db.query(models.PlaybackRecord).filter(
        models.PlaybackRecord.folder_name == req.folder_name,
        models.PlaybackRecord.filename == req.filename
    ).first()
    if row:
        was_active = not row.is_stale
        db.delete(row)
        if was_active:
            _bump_watched_episode_count(db, req, -1)
    db.commit()
    return {"status": "success", "message": "Unmarked"}

def get_library_root(db: Session) -> Path:
    """
    智能获取媒体库根目录：
    1. 从设置中读取用户配置的物理路径（例如：D:\\AnimeLibrary）。
    2. 如果检测到程序运行在 Docker (Linux) 容器内，自动将其重写为容器内的映射路径 /AnimeLibrary。
    3. 如果运行在 Windows 宿主机，则保留原物理路径。
    """
    raw_path = get_setting(db, "library_root", r"D:\AnimeLibrary")
    normalized_path = raw_path.replace("\\", "/")
    is_docker = os.path.exists("/.dockerenv")
    is_linux = platform.system() == "Linux"

    if (is_docker or is_linux) and "D:" in normalized_path:
        container_path = normalized_path.replace("D:", "", 1)
        return Path(container_path)
    return Path(raw_path)


def _bucket_name_for_subdir(dir_name: str, extra_buckets: set[str] | None) -> str:
    """给番剧根目录下某个直接子目录分类,决定它进哪个"桶"。抽出来独立成一个函数
    (而不是只在scan_local_folder_structure内联),因为delete_episode那边做"未看集数"
    角标精确算术更新时,需要用同一套规则单独判断"被删的这个文件在不在正片桶里"
    (只有正片桶才计入角标分母,见_NON_EPISODE_BUCKETS),不能反手再写一份容易跟这边
    分叉的判断逻辑。

    rename_engine.py 实际会创建的子目录名只有这几种:Season NN、Season 00(OVA)、
    剧场版、Other、以及算不出季号时按作品名命名的目录(extra_buckets)——这几个要保留
    各自的桶,不能被下面的兜底正则一起归进"Specials/Others"。季度目录的正则用
    rename_engine那一份,不在这里另写一个:work_title_bucket()要靠同一个正则避开
    会被误认成季度目录的作品名。"""
    season_match = rename_engine.SEASON_DIR_PATTERN.match(dir_name)
    is_known_bucket = (
        bool(season_match)
        or dir_name in ("剧场版", "劇場版", "Other", "OVA")
        or dir_name in (extra_buckets or ())
    )
    return dir_name if is_known_bucket else "Specials/Others"


def scan_local_folder_structure(anime_path: Path, library_root: Path,
                                extra_buckets: set[str] | None = None):
    """
    扫描单部动漫文件夹下的物理结构。

    extra_buckets:这部番额外的合法桶名,来自AnimeFamilyCache.folder_bucket——
    算不出季号的成员现在会落进以作品名命名的目录(见rename_engine.work_title_bucket),
    这些目录名是动态的,没法写进下面的白名单,必须由调用方查表传进来。
    不传(或者家族缓存刚被reset_season_cache清空的窗口)时退化成原来的折叠行为:
    这些目录归进"Specials/Others",而services/library_repair.py会跳过这个桶——
    **只会少提改名建议,不会改错文件**,跟"Season 00"过去的表现一致。
    """
    structure = {}
    if not anime_path.exists():
        return structure

    library_root_str = str(library_root)

    def to_rel(full_path: str) -> str:
        return os.path.relpath(full_path, library_root_str).replace("\\", "/")

    def is_video(name: str) -> bool:
        return os.path.splitext(name)[1].lower() in VIDEO_EXTENSIONS

    # 用os.scandir/os.walk代替pathlib的iterdir/rglob:scandir/walk的条目类型直接来自
    # 目录读取结果(DirEntry),不再对每个文件单独.is_file()触发一次stat——番剧目录里
    # 混着的字幕/字体子目录(几十上百文件)/样图不会再被逐个stat,网络共享媒体库下尤其明显。
    with os.scandir(anime_path) as it:
        for entry in it:
            if entry.is_dir():
                season_name = _bucket_name_for_subdir(entry.name, extra_buckets)
                episodes = []
                for dirpath, _dirnames, filenames in os.walk(entry.path):
                    for fname in filenames:
                        if is_video(fname):
                            full = os.path.join(dirpath, fname)
                            episodes.append({
                                "filename": fname,
                                "rel_path": to_rel(full),
                            })
                if episodes:
                    # 用extend而不是直接赋值:避免不同子目录被归到同一个桶名时(比如两个都落进
                    # "Specials/Others"兜底桶)后处理的目录把前一个目录的集数覆盖掉
                    structure.setdefault(season_name, []).extend(episodes)
            elif entry.is_file() and is_video(entry.name):
                # 视频直接堆在番剧根目录(没有Season子目录)时,靠文件名里的关键词
                # 区分是剧场版/OVA合集还是正常的单季番,不再一律标成"Season 1"
                is_movie = re.search(r"剧场版|劇場版|movie|OVA", entry.name, re.IGNORECASE)
                bucket_name = "剧场版/OVA" if is_movie else "Season 1"
                structure.setdefault(bucket_name, []).append({
                    "filename": entry.name,
                    "rel_path": to_rel(entry.path),
                })
    for episodes in structure.values():
        episodes.sort(key=lambda x: x["filename"])
    return structure



# "未看集数"角标只应该反映真正的正片(季度剧集/剧场版/OVA),不该被"Other"(rename_engine
# 固定拿来放PV/预告这类附赠内容的桶)和"Specials/Others"(算不出季号/不认识的杂项兜底桶,
# 装的多是CM/菜单/花絮)撑大分母——不然角标数字会比用户直觉里的"还剩几集没看"大得多。
_NON_EPISODE_BUCKETS = {"Other", "Specials/Others"}


def _sum_real_episodes(structure: dict) -> int:
    """按上面的口径,只数正片桶里的视频文件数。"""
    return sum(
        len(episodes) for bucket, episodes in structure.items()
        if bucket not in _NON_EPISODE_BUCKETS
    )


def _real_episode_filenames(structure: dict) -> set[str]:
    """正片桶里全部文件名(给"未看集数"角标的已看分子按同一口径过滤用)。"""
    return {
        ep["filename"]
        for bucket, episodes in structure.items()
        if bucket not in _NON_EPISODE_BUCKETS
        for ep in episodes
    }


def _count_watched_real_episodes(db: Session, folder_name: str, structure: dict) -> int:
    """这个文件夹的正片桶里,有多少个文件已经看过(非 stale 的 PlaybackRecord)。
    角标的已看分子——只认正片桶,跟 _sum_real_episodes 同源,不用"不分桶的全局计数"。"""
    real_names = _real_episode_filenames(structure)
    if not real_names:
        return 0
    return (
        db.query(func.count(func.distinct(models.PlaybackRecord.filename)))
        .filter(
            models.PlaybackRecord.folder_name == folder_name,
            models.PlaybackRecord.is_stale.is_(False),
            models.PlaybackRecord.filename.in_(real_names),
        )
        .scalar()
    ) or 0


def _flag_stale_playback_records(db: Session, folder_name: str, structure: dict) -> None:
    """已播放记录里,文件已经不在磁盘上的(重新下载了别的字幕组版本、旧文件被换掉/手动删了)
    打上is_stale——这类记录不物理删除(保留"确实看过"的历史),但从此不再计入"未看集数"
    角标的已看分子,避免把早就不存在的旧文件也算成"占着一个未看名额"或"多算一次已看"。
    只在调用方已经手头有一份现成structure的地方顺手核对(详情页/badge重扫),不为此单独扫盘。"""
    existing_filenames = {
        ep["filename"] for episodes in structure.values() for ep in episodes
    }
    stale_records = (
        db.query(models.PlaybackRecord)
        .filter(
            models.PlaybackRecord.folder_name == folder_name,
            models.PlaybackRecord.is_stale.is_(False),
        )
        .all()
    )
    changed = False
    for record in stale_records:
        if record.filename not in existing_filenames:
            record.is_stale = True
            changed = True
    if changed:
        db.commit()


def _count_episode_files(
    db: Session, library_root: Path, media: "models.LocalMedia"
) -> tuple[int, int, str | None] | None:
    """跟详情页GET /library/detail完全同一套逻辑(算家族桶→scan_local_folder_structure),
    数出这个文件夹当前实际有多少"正片"视频文件(见_sum_real_episodes)——媒体库卡片
    "未看集数"角标用这个数字当分母,不用AnimeCatalog.total_episodes(那个只挂在单个
    bgm_id上,家族合并的文件夹会算错)。顺手核对一遍播放记录里有没有文件已经不存在了
    (见_flag_stale_playback_records)。
    返回(正片视频文件数, 正片桶里已看的文件数, 本次扫描时的目录签名字符串);
    文件夹不存在时返回None,不写0(0是"确实扫到0集",跟"没扫过/扫不到"要分开)。"""
    anime_path = library_root / media.folder_name
    signature = _folder_structure_signature(anime_path)
    if signature is None:
        return None
    family_root = None
    if media.bgm_id:
        family_root = bgm_series_cache.cached_auto_root(db, media.bgm_id) or media.bgm_id
    extra_buckets = bgm_series_cache.family_work_title_buckets(db, family_root)
    structure = scan_local_folder_structure(anime_path, library_root, extra_buckets)
    _flag_stale_playback_records(db, media.folder_name, structure)
    count = _sum_real_episodes(structure)
    watched = _count_watched_real_episodes(db, media.folder_name, structure)
    return count, watched, _serialize_signature(signature)


def recompute_episode_counts(db: Session, folder_name: str) -> None:
    """当场把这个文件夹的"未看集数"角标两列(episode_file_count / watched_episode_count)
    连同目录签名一起重算落库。给 services/organize.py 整理入库后调用——不再只是置 NULL
    等 /library/scan 后台补课或用户进详情页,那样 RSS 追更下了新一集角标要好久才更新。
    文件夹当前不可达(_count_episode_files 返回 None)时保持原值不动,不写 0。"""
    media = (
        db.query(models.LocalMedia)
        .filter(models.LocalMedia.folder_name == folder_name)
        .first()
    )
    if not media:
        return
    result = _count_episode_files(db, get_library_root(db), media)
    if result is None:
        return
    count, watched, signature = result
    media.episode_file_count = count
    media.watched_episode_count = watched
    media.episode_count_signature = signature
    media.episode_count_updated_at = datetime.now()
    db.commit()


def _backfill_missing_episode_counts(library_root: Path) -> None:
    """后台任务(不阻塞调用方):刷新"未看集数"角标要用的episode_file_count缓存列。
    不是只补NULL行——如果只看NULL,一个文件夹只要曾经被扫过一次,之后哪怕手动往里面
    拖了新文件(没走delete/regroup/自动整理入库这几个精确算术更新的路径),这个数字会
    永远卡在旧值,角标越来越不准。所以这里对**每一行**都先用_folder_structure_signature
    做一次廉价核对(一次scandir,不遍历文件),签名跟上次落库时不一致(或本来就是NULL)
    才会被丢进线程池真正重扫;签名没变的行直接跳过,不产生额外磁盘IO。
    每个线程各开一个session,互不干扰,扫完统一在主线程里落库。"""
    from concurrent.futures import ThreadPoolExecutor

    db = SessionLocal()
    try:
        rows = db.query(
            models.LocalMedia.id,
            models.LocalMedia.folder_name,
            models.LocalMedia.episode_file_count,
            models.LocalMedia.watched_episode_count,
            models.LocalMedia.episode_count_signature,
        ).all()
    finally:
        db.close()

    stale_ids: list[int] = []
    for media_id, folder_name, count, watched, stored_signature in rows:
        if count is None or watched is None:  # 任一列缺 → 重扫,两列成对补齐
            stale_ids.append(media_id)
            continue
        current_signature = _serialize_signature(_folder_structure_signature(library_root / folder_name))
        if current_signature != stored_signature:
            stale_ids.append(media_id)

    if not stale_ids:
        return

    def _scan_one(media_id: int):
        thread_db = SessionLocal()
        try:
            media = thread_db.query(models.LocalMedia).filter(models.LocalMedia.id == media_id).first()
            if media is None:
                return media_id, None
            return media_id, _count_episode_files(thread_db, library_root, media)
        except Exception as e:
            print(f"[LIBRARY] 补全未看集数角标失败 media_id={media_id}: {e}")
            return media_id, None
        finally:
            thread_db.close()

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(_scan_one, stale_ids))

    db = SessionLocal()
    try:
        for media_id, result in results:
            if result is None:
                continue
            count, watched, signature = result
            media = db.query(models.LocalMedia).filter(models.LocalMedia.id == media_id).first()
            if media:
                media.episode_file_count = count
                media.watched_episode_count = watched
                media.episode_count_signature = signature
                media.episode_count_updated_at = datetime.now()
        db.commit()
    finally:
        db.close()


def get_latest_activity(anime_path: Path) -> datetime | None:
    """
    只读番剧根目录自身的mtime(一次stat),不再额外iterdir()+逐个Season子目录
    stat——新集入库固定走services/organize.py::_organize_single_torrent,不管
    是否开自动改名,第一步永远是qbittorrent_client.set_torrent_location把这个
    种子的文件整体落到番剧根目录下,然后才由renameFile相对路径调用挪进具体的
    Season子文件夹;即使目标Season文件夹早就存在,"整体落到根目录"这一步依然
    会先触碰一次根目录自身的mtime。所以根目录自己的mtime就足够反映"这部番最近
    是不是有新内容落地",不需要再挨个看每个Season子文件夹。
    (已知局限:如果绕开程序手动把文件直接拖进某个Season子文件夹,不会触发这里
    的mtime变化,"最新更新"排序感知不到——这是"新集入库固定走organize.py"这个
    前提下可以接受的取舍。)
    """
    if not anime_path.exists():
        return None
    return datetime.fromtimestamp(anime_path.stat().st_mtime)


NO_SUMMARY = "暂无简介"


def _catalog_summary_stale(catalog) -> bool:
    """简介缺失 / 只剩占位串——早期一次失败或部分响应会把占位串烤进 AnimeCatalog,
    此后再没有任何流程回来刷新它(缓存只在"行不存在"时才补)。视图层据此后台补一次,
    让老坏行自愈。"""
    s = (getattr(catalog, "summary", None) or "").strip()
    return not s or s == NO_SUMMARY


async def update_anime_details_from_bgm(db: Session, bgm_id: int):
    """
    异步从 Bangumi 获取详情（封面、简介、总集数），并同步保存到本地数据库中
    """
    try:
        # 调用 bgm.py 里的接口获取详情
        bgm_data = await get_subject_detail(bgm_id)
        if not bgm_data:
            return

        info = normalize_bgm_subject(bgm_data)

        # 查询数据库本地是否已有该条目的缓存信息
        catalog = db.query(models.AnimeCatalog).filter(models.AnimeCatalog.bgm_id == bgm_id).first()
        if catalog:
            catalog.title = info["title"]
            catalog.title_original = info["title_original"]
            # 这次也没拿到真简介(占位串)时,不要拿它去覆盖已有的真简介
            if info["summary"] != NO_SUMMARY or _catalog_summary_stale(catalog):
                catalog.summary = info["summary"]
            catalog.cover_url = info["cover_url"]
            catalog.air_date = info["air_date"]
            catalog.total_episodes = info["total_eps"]  # 保存总集数到本地表
            catalog.total_eps = info["total_eps"]
        else:
            new_catalog = models.AnimeCatalog(
                bgm_id=bgm_id,
                title=info["title"],
                title_original=info["title_original"],
                summary=info["summary"],
                cover_url=info["cover_url"],
                air_date=info["air_date"],
                total_episodes=info["total_eps"],
                total_eps=info["total_eps"],
            )
            db.add(new_catalog)
        db.commit()
    except Exception as e:
        print(f"从 Bangumi 获取条目 {bgm_id} 失败: {e}")


async def _update_anime_details_from_bgm_task(bgm_id: int) -> None:
    """后台任务版本:list_library_animes遇到没有AnimeCatalog缓存的番剧时用这个,
    不能直接await update_anime_details_from_bgm(request_db, bgm_id)——那样会让
    列表接口的响应等一次真实的Bangumi网络请求,请求量一大(或者这次请求本身失败)
    就会一直卡列表接口。开一个独立session,跟services/bgm_series_cache.py::
    prefetch_rename_cache_task是同一种模式;这次列表响应先用兜底文案返回,
    等这个任务跑完,下一次任意一次列表请求就能看到补全后的封面/简介。
    """
    db = SessionLocal()
    try:
        await update_anime_details_from_bgm(db, bgm_id)
    finally:
        db.close()


async def _family_root_and_members(db: Session, bgm_id: int, title: str) -> tuple[int, list[models.AnimeFamilyCache]]:
    """缓存优先地拿到bgm_id所在家族的根id + 该家族全部成员行(AnimeFamilyCache)。
    命中缓存零网络;只有这个bgm_id从没被算过家族时,才用resolve_series_identity
    (它本身也是缓存优先,仅全新条目联网)填充一次。不走resolve_related_family_ids_cached
    ——那个每次都会带一轮联网的"有没有新成员"核实,这里不需要、也不想给远程加负担。"""
    cached = (
        db.query(models.AnimeFamilyCache)
        .filter(models.AnimeFamilyCache.bgm_id == bgm_id)
        .first()
    )
    if cached is None:
        # 没算过家族:填充一次(缓存优先,仅全新条目联网),再重查。
        await bgm_series_cache.resolve_series_identity(db, bgm_id, title)
        cached = (
            db.query(models.AnimeFamilyCache)
            .filter(models.AnimeFamilyCache.bgm_id == bgm_id)
            .first()
        )
    if cached is None:
        return bgm_id, []  # 家族解析彻底失败(网络问题):当作只有自己
    root = cached.source_bgm_id
    members = (
        db.query(models.AnimeFamilyCache)
        .filter(models.AnimeFamilyCache.source_bgm_id == root)
        .all()
    )
    return root, members


def _pick_cover_bgm_id_by_strategy(bgm_id: int, members: list[models.AnimeFamilyCache], strategy: str) -> int:
    """按策略从家族成员里挑作为封面的bgm_id。只在platform=="TV"且有season_ordinal的
    成员里选(跟build_season_episode_table同一套"真季"过滤);无TV季(纯剧场版家族)
    则回退用绑定条目bgm_id本身。latest_tv取最大season_ordinal、first_season取最小,
    并列按(date,bgm_id)确定性排序。"""
    tv_seasons = [m for m in members if m.platform == "TV" and m.season_ordinal]
    if not tv_seasons:
        return bgm_id
    # 先按(date,bgm_id)排,保证同一season_ordinal下取的代表成员确定;再按ordinal选头/尾。
    tv_seasons.sort(key=lambda m: (m.season_ordinal, m.date or "", m.bgm_id))
    chosen = tv_seasons[-1] if strategy == "latest_tv" else tv_seasons[0]
    return chosen.bgm_id


async def _resolve_cover_bgm_id_task(folder_name: str) -> None:
    """后台任务:按默认封面策略,给某个库条目解析出该用哪个家族成员的图,写进
    LocalMedia.cover_bgm_id。独立session(仿_update_anime_details_from_bgm_task),
    不阻塞列表响应。手动选过图(cover_is_custom)/无bgm_id/策略=matched 都直接跳过。"""
    db = SessionLocal()
    try:
        media = (
            db.query(models.LocalMedia)
            .filter(models.LocalMedia.folder_name == folder_name)
            .first()
        )
        if not media or not media.bgm_id or media.cover_is_custom or media.cover_bgm_id is not None:
            return
        strategy = get_setting(
            db, "library_cover_strategy", config_store.DEFAULTS["library_cover_strategy"]
        )
        if strategy == "matched":
            return

        _root, members = await _family_root_and_members(db, media.bgm_id, folder_name)
        target = _pick_cover_bgm_id_by_strategy(media.bgm_id, members, strategy)

        # 并发/重复触发下media可能已被别的任务改动,重取最新再写,避免覆盖用户刚做的手动选择。
        media = (
            db.query(models.LocalMedia)
            .filter(models.LocalMedia.folder_name == folder_name)
            .first()
        )
        if not media or media.cover_is_custom or media.cover_bgm_id is not None:
            return
        media.cover_bgm_id = target
        db.commit()

        # 仅当目标封面还没缓存时才补一次,让下次列表能取到cover_url(缓存优先)。
        if target != media.bgm_id:
            exists = db.query(models.AnimeCatalog).filter(models.AnimeCatalog.bgm_id == target).first()
            if not exists or not exists.cover_url:
                await update_anime_details_from_bgm(db, target)
    except Exception as e:
        print(f"[COVER] 解析默认封面失败 folder={folder_name}: {e}")
    finally:
        db.close()


class MatchRequest(BaseModel):
    folder_name: str
    bgm_id: int


@router.post("/library/match")
async def match_anime(
    req: MatchRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)
):
    """手动将本地文件夹绑定到指定的 Bangumi 条目，不修改物理文件夹名"""
    media = db.query(models.LocalMedia).filter(
        models.LocalMedia.folder_name == req.folder_name
    ).first()
    if not media:
        library_root = get_library_root(db)
        if not (library_root / req.folder_name).is_dir():
            raise HTTPException(status_code=404, detail="Folder not found on disk.")
        media = models.LocalMedia(folder_name=req.folder_name)
        db.add(media)

    prev_bgm_id = media.bgm_id
    media.bgm_id = req.bgm_id
    if req.bgm_id != prev_bgm_id:
        # 换绑到新条目:旧的封面选择(旧家族成员/旧手动选图)已经不适用,清掉让默认
        # 策略按新条目重新解析——否则「重新匹配」后网格/详情页会一直显示旧图,
        # 用户还得多点一次「选择图片 → 恢复默认」。
        media.cover_bgm_id = None
        media.cover_is_custom = False
    db.commit()

    await update_anime_details_from_bgm(db, req.bgm_id)
    if req.bgm_id != prev_bgm_id:
        # 同步把新条目的默认封面解析好,回到网格时图就是对的;背景图/LOGO(anime-meta)
        # 按 bgm_id 缓存,重进详情页会自己取新的,这里只需后台预热一次减少 pending。
        await _resolve_cover_bgm_id_task(req.folder_name)
        background_tasks.add_task(_resolve_anime_meta_task, req.bgm_id)
    return {"status": "success", "folder_name": req.folder_name, "bgm_id": req.bgm_id}


@router.get("/library/cover-candidates/{bgm_id}")
async def list_cover_candidates(bgm_id: int, db: Session = Depends(get_db)):
    """"选择图片"弹窗的数据源:返回bgm_id所在家族全部成员的封面候选[{bgm_id,title,cover_url}]。
    缓存优先——成员名/封面URL先取本地AnimeCatalog,只有本地没有(或cover_url为空)的成员
    才去网络补一次并落库,重复打开零网络;封面字节再经/media/image-proxy磁盘缓存。"""
    _root, members = await _family_root_and_members(db, bgm_id, "")
    family_ids = [m.bgm_id for m in members] or [bgm_id]

    catalogs = {
        c.bgm_id: c
        for c in db.query(models.AnimeCatalog).filter(models.AnimeCatalog.bgm_id.in_(family_ids)).all()
    }
    missing = [fid for fid in family_ids if fid not in catalogs or not catalogs[fid].cover_url]
    if missing:
        details = await get_subject_details_batch(missing)
        for fid, detail in details.items():
            if not detail:
                continue
            info = normalize_bgm_subject(detail)
            row = catalogs.get(fid)
            if row:
                row.title = info["title"]
                row.title_original = info["title_original"]
                row.summary = info["summary"]
                row.cover_url = info["cover_url"]
                row.air_date = info["air_date"]
                row.total_episodes = info["total_eps"]
                row.total_eps = info["total_eps"]
            else:
                row = models.AnimeCatalog(
                    bgm_id=fid,
                    title=info["title"],
                    title_original=info["title_original"],
                    summary=info["summary"],
                    cover_url=info["cover_url"],
                    air_date=info["air_date"],
                    total_episodes=info["total_eps"],
                    total_eps=info["total_eps"],
                )
                db.add(row)
                catalogs[fid] = row
        db.commit()

    # 只返回有封面的成员;按bgm_id降序(通常越新的作品id越大),跟补番一览的排序习惯一致。
    candidates = [
        {"bgm_id": fid, "title": catalogs[fid].title, "cover_url": catalogs[fid].cover_url}
        for fid in family_ids
        if fid in catalogs and catalogs[fid].cover_url
    ]
    candidates.sort(key=lambda c: c["bgm_id"], reverse=True)
    return {"data": candidates}


class CoverSelectRequest(BaseModel):
    bgm_id: int


@router.post("/library/{folder_name}/cover")
async def set_library_cover(folder_name: str, req: CoverSelectRequest, db: Session = Depends(get_db)):
    """手动选定该库条目的封面为家族里某个成员的图,标记为自定义(不再被默认策略覆盖)。"""
    media = db.query(models.LocalMedia).filter(models.LocalMedia.folder_name == folder_name).first()
    if not media:
        raise HTTPException(status_code=404, detail="媒体库条目不存在")
    media.cover_bgm_id = req.bgm_id
    media.cover_is_custom = True
    db.commit()
    # 确保所选封面已缓存,列表立即能取到cover_url。
    exists = db.query(models.AnimeCatalog).filter(models.AnimeCatalog.bgm_id == req.bgm_id).first()
    if not exists or not exists.cover_url:
        await update_anime_details_from_bgm(db, req.bgm_id)
    return {"folder_name": folder_name, "cover_bgm_id": req.bgm_id}


@router.delete("/library/{folder_name}/cover")
def reset_library_cover(folder_name: str, db: Session = Depends(get_db)):
    """恢复默认封面:清掉手动选择,下次列表请求按默认策略重新解析cover_bgm_id。"""
    media = db.query(models.LocalMedia).filter(models.LocalMedia.folder_name == folder_name).first()
    if not media:
        raise HTTPException(status_code=404, detail="媒体库条目不存在")
    media.cover_is_custom = False
    media.cover_bgm_id = None
    db.commit()
    return {"folder_name": folder_name, "cover_bgm_id": None}


# ----- 剧场版模式:独立剧场版/OVA 登记表(StandaloneMedia)的增删查 -----

def _norm_rel(path: str) -> str:
    """rel_path 归一化成正斜杠,与 scan_local_folder_structure 的 to_rel 同格式。
    自动加时来源是 RenamedFile.target_relative_path(反斜杠),必须转,否则和磁盘/系列分集对不上。"""
    return (path or "").replace("\\", "/")


class StandaloneAddRequest(BaseModel):
    library_folder: str
    rel_path: str
    filename: str
    bgm_id: int
    media_type: str | None = None  # "movie" / "ova"


class StandaloneRegroupRequest(BaseModel):
    ids: list[int]
    bgm_id: int


@router.get("/library/standalone")
async def list_standalone_media(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """剧场版模式的数据源:读 StandaloneMedia,每行补上封面/标题/简介(AnimeCatalog,缓存优先)、
    观看态(PlaybackRecord 按 library_folder+filename)、以及 rel_path 是否还在磁盘(missing)。
    返回扁平行,前端按 bgm_id 分组成卡。"""
    rows = db.query(models.StandaloneMedia).order_by(models.StandaloneMedia.created_at.desc()).all()
    if not rows:
        return []

    library_root = get_library_root(db)

    bgm_ids = {r.bgm_id for r in rows}
    catalogs = {
        c.bgm_id: c
        for c in db.query(models.AnimeCatalog).filter(models.AnimeCatalog.bgm_id.in_(bgm_ids)).all()
    }
    # 观看态:按 (folder_name, filename) 聚合最近一次观看时间
    watched_rows = (
        db.query(models.PlaybackRecord.folder_name, models.PlaybackRecord.filename,
                 func.max(models.PlaybackRecord.watched_at))
        .filter(models.PlaybackRecord.folder_name.in_({r.library_folder for r in rows}))
        .group_by(models.PlaybackRecord.folder_name, models.PlaybackRecord.filename)
        .all()
    )
    watched_map = {(f, n): w for f, n, w in watched_rows}

    result = []
    for r in rows:
        catalog = catalogs.get(r.bgm_id)
        if not catalog or _catalog_summary_stale(catalog) or not catalog.cover_url:
            # 封面/标题/简介还没缓存,或老坏行(简介只剩占位串):后台补一次,下次列表就有了。
            background_tasks.add_task(_update_anime_details_from_bgm_task, r.bgm_id)
        watched_at = watched_map.get((r.library_folder, r.filename))
        result.append({
            "id": r.id,
            "library_folder": r.library_folder,
            "rel_path": r.rel_path,
            "filename": r.filename,
            "bgm_id": r.bgm_id,
            "media_type": r.media_type,
            "title": (catalog.title if catalog else None),
            "cover_url": (catalog.cover_url if catalog else None),
            "summary": (catalog.summary if catalog else None),
            "is_watched": watched_at is not None,
            "watched_at": watched_at.strftime("%Y-%m-%d %H:%M:%S") if watched_at else None,
            "missing": not (library_root / r.rel_path).exists(),
        })
    return result


@router.post("/library/standalone")
async def add_standalone_media(req: StandaloneAddRequest, db: Session = Depends(get_db)):
    """手动/自动追加一部独立剧场版/OVA(按 rel_path upsert)。确保所选条目的 AnimeCatalog 已缓存,
    列表立即能取到封面/标题/简介。"""
    rel = _norm_rel(req.rel_path)
    row = db.query(models.StandaloneMedia).filter(models.StandaloneMedia.rel_path == rel).first()
    if row:
        row.library_folder = req.library_folder
        row.filename = req.filename
        row.bgm_id = req.bgm_id
        row.media_type = req.media_type
    else:
        row = models.StandaloneMedia(
            library_folder=req.library_folder,
            rel_path=rel,
            filename=req.filename,
            bgm_id=req.bgm_id,
            media_type=req.media_type,
            source="manual",
        )
        db.add(row)
    db.commit()

    exists = db.query(models.AnimeCatalog).filter(models.AnimeCatalog.bgm_id == req.bgm_id).first()
    if not exists or not exists.cover_url:
        await update_anime_details_from_bgm(db, req.bgm_id)
    db.refresh(row)
    return {"id": row.id, "rel_path": row.rel_path, "bgm_id": row.bgm_id}


@router.delete("/library/standalone/{item_id}")
def remove_standalone_media(item_id: int, db: Session = Depends(get_db)):
    """仅移出剧场版列表:只删登记行,不动磁盘文件。"""
    row = db.query(models.StandaloneMedia).filter(models.StandaloneMedia.id == item_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="记录不存在")
    db.delete(row)
    db.commit()
    return {"status": "success", "id": item_id}


@router.put("/library/standalone/regroup")
async def regroup_standalone_media(req: StandaloneRegroupRequest, db: Session = Depends(get_db)):
    """重选条目:把这张卡下各行的 bgm_id 改成新条目(换封面/标题/简介),用于自动分配选错时手动纠正。"""
    if not req.ids:
        return {"status": "success", "updated": 0}
    db.query(models.StandaloneMedia).filter(
        models.StandaloneMedia.id.in_(req.ids)
    ).update({models.StandaloneMedia.bgm_id: req.bgm_id}, synchronize_session=False)
    db.commit()
    exists = db.query(models.AnimeCatalog).filter(models.AnimeCatalog.bgm_id == req.bgm_id).first()
    if not exists or not exists.cover_url:
        await update_anime_details_from_bgm(db, req.bgm_id)
    return {"status": "success", "updated": len(req.ids), "bgm_id": req.bgm_id}


# ----- 归属调整:把内容拆成独立一部 / 合并到另一部 -----


class RegroupRequest(BaseModel):
    """把一批已经落地的文件,从当前所在的库文件夹改挂到另一个归属下。

    rel_paths 由前端从详情页/剧场版列表里已有的结构直接给出(一个季度桶下的全部
    视频文件,或剧场版模式里选中的那几个文件),后端不再重新推导"哪些文件属于这一部"
    ——那份结构前端本来就有,重推一遍只会多一个可能不一致的来源。
    """
    bgm_id: int  # 要改归属的那部作品(写进MediaGroupOverride的主键)
    target_root_bgm_id: int | None = None  # None=拆成独立一部;否则并进这个家族根
    rel_paths: list[str]  # 相对library_root的正斜杠路径
    # 恢复自动归属:删掉手动覆盖,并把文件搬回Bangumi自动算出的家族文件夹。
    # 为真时忽略target_root_bgm_id。
    restore_auto: bool = False


@router.get("/library/regroup/candidates/{bgm_id}")
async def list_regroup_candidates(bgm_id: int, db: Session = Depends(get_db)):
    """「归属」弹窗的数据源:这个条目所在Bangumi家族的全部成员 + 当前生效的归属。

    候选走_family_root_and_members(跟"选择图片"弹窗同一个函数),它读的是
    AnimeFamilyCache.source_bgm_id(Bangumi客观结构),**不经过用户覆盖**——
    所以对已经被拆出去的条目(比如无职转生第三季),这里照样能列出完整家族,
    用户才有得选、能把它合并回去。这正是拆出去之后"合并不回来"的修复点。
    """
    auto_root, members = await _family_root_and_members(db, bgm_id, "")

    member_ids = [m.bgm_id for m in members] or [bgm_id]
    catalogs = {
        c.bgm_id: c
        for c in db.query(models.AnimeCatalog).filter(models.AnimeCatalog.bgm_id.in_(member_ids)).all()
    }

    def _title(mid: int, cached_name: str | None = None) -> str:
        row = catalogs.get(mid)
        if row and (row.title or "").strip():
            return row.title.strip()
        return (cached_name or "").strip() or f"bgm-{mid}"

    payload_members = [
        {
            "bgm_id": m.bgm_id,
            "title": _title(m.bgm_id, m.name),
            "cover_url": (catalogs.get(m.bgm_id).cover_url if catalogs.get(m.bgm_id) else None),
            "platform": m.platform,
            "season_ordinal": m.season_ordinal,
            "folder_bucket": m.folder_bucket,
            "is_auto_root": m.bgm_id == auto_root,
        }
        for m in members
    ]
    # 家族根排最前,其余按bgm_id降序(越新的id越大),跟补番/选择图片的排序习惯一致
    payload_members.sort(key=lambda x: (not x["is_auto_root"], -x["bgm_id"]))

    override = (
        db.query(models.MediaGroupOverride)
        .filter(models.MediaGroupOverride.bgm_id == bgm_id)
        .first()
    )
    return {
        "bgm_id": bgm_id,
        "auto_root": {"bgm_id": auto_root, "title": _title(auto_root)},
        "members": payload_members,
        "current_root": override.root_bgm_id if override else auto_root,
        "is_overridden": override is not None,
    }


def _title_for_bgm_id(db: Session, bgm_id: int, fallback: str) -> str:
    """某个条目用于拼文件夹名的标题:家族缓存 -> 番剧元数据 -> 兜底。"""
    cached = (
        db.query(models.AnimeFamilyCache)
        .filter(models.AnimeFamilyCache.bgm_id == bgm_id)
        .first()
    )
    if cached and (cached.name or "").strip():
        return cached.name.strip()
    catalog = (
        db.query(models.AnimeCatalog)
        .filter(models.AnimeCatalog.bgm_id == bgm_id)
        .first()
    )
    if catalog and (catalog.title or "").strip():
        return catalog.title.strip()
    return fallback


@router.post("/library/regroup")
async def regroup_media(req: RegroupRequest, db: Session = Depends(get_db)):
    """拆成独立一部 / 合并到另一部:写归属覆盖 + 把文件搬到新文件夹 + 同步数据库,
    最后按新归属把名字和季度目录一并重排好,用户不需要再手动跑一次"修复媒体库"。

    搬家这一步只换顶层文件夹,子路径原样保留;重排交给library_repair那套既有逻辑
    (限定只扫这一个文件夹),不在这里复制第二份改名规则——两份早晚会漂移。

    归属覆盖(MediaGroupOverride)先写:写完之后重排和"修复媒体库"的合并扫描都会
    认这个新归属,不会再把刚拆出来的文件夹提议合并回原家族(见
    services/library_repair.py::scan_family_folder_merges的_root_of)。
    """
    from services.library_repair import (
        _list_all_relpaths, apply_rename_fixes, move_media_file_with_sync,
    )

    if not req.rel_paths:
        raise HTTPException(status_code=400, detail="没有要移动的文件")

    library_root = get_library_root(db)
    auto_root = await bgm_series_cache.resolve_auto_root(db, req.bgm_id)
    new_root = auto_root if req.restore_auto else (req.target_root_bgm_id or req.bgm_id)

    fallback = _norm_rel(req.rel_paths[0]).split("/")[0]
    new_folder = rename_engine.build_anime_folder_name(
        _title_for_bgm_id(db, new_root, fallback), new_root
    )

    # 先落归属:即使下面搬文件中途失败,归属本身已经是用户要的状态,
    # 重试这个操作是幂等的(已经搬过去的文件不会再出现在rel_paths里)。
    #
    # 归属跟Bangumi自动判定一致时**删覆盖**而不是写一条等价的显式覆盖——
    # 保持"没有覆盖行 == 沿用自动判定"这个语义干净:以后Bangumi家族结构真的变了,
    # 没被钉住的条目能自然跟着走,被钉住的会一直停在旧答案上。
    if new_root == auto_root:
        bgm_series_cache.clear_group_override(db, req.bgm_id)
    else:
        bgm_series_cache.set_group_override(db, req.bgm_id, new_root)

    # 字幕要跟着视频一起搬,否则搬完就成了没字幕的孤儿文件。按源文件夹缓存一份
    # 完整文件清单(含非视频文件),交给rename_engine那套"同目录+同文件名主干"的
    # 判定——跟下载整理时字幕跟随改名用的是同一个函数,判定标准一致。
    all_paths_by_folder: dict[str, list[str]] = {}

    def _siblings_of(current_rel: str, folder: str) -> list[str]:
        if folder not in all_paths_by_folder:
            all_paths_by_folder[folder] = _list_all_relpaths(library_root / folder, library_root)
        return rename_engine.find_sibling_subtitles(current_rel, all_paths_by_folder[folder])

    source_folders: set[str] = set()
    moved, skipped, failed = [], [], []
    for raw in req.rel_paths:
        current_rel = _norm_rel(raw)
        parts = current_rel.split("/")
        if len(parts) < 2:
            skipped.append({"path": current_rel, "reason": "不是番剧文件夹下的相对路径"})
            continue
        source_folders.add(parts[0])
        proposed_rel = "/".join([new_folder, *parts[1:]])
        if proposed_rel == current_rel:
            skipped.append({"path": current_rel, "reason": "已经在目标文件夹下"})
            continue
        if not (library_root / current_rel).exists():
            skipped.append({"path": current_rel, "reason": "文件不存在"})
            continue
        if (library_root / proposed_rel).exists():
            skipped.append({"path": current_rel, "reason": "目标位置已存在同名文件,未覆盖"})
            continue
        try:
            move_media_file_with_sync(
                db, library_root, current_rel, proposed_rel,
                _siblings_of(current_rel, parts[0]),
            )
            db.commit()
            moved.append({"from": current_rel, "to": proposed_rel})
        except OSError as e:
            db.rollback()
            failed.append({"path": current_rel, "error": str(e)})

    # 新文件夹要有自己的LocalMedia行,媒体库列表才会把它当成独立的一部显示出来。
    if moved:
        target_media = (
            db.query(models.LocalMedia)
            .filter(models.LocalMedia.folder_name == new_folder)
            .first()
        )
        if target_media is None:
            db.add(models.LocalMedia(folder_name=new_folder, bgm_id=new_root))
        elif target_media.bgm_id is None:
            target_media.bgm_id = new_root
        db.commit()

    # 源文件夹被搬空了就顺手清理,不留空壳目录和指向它的脏记录
    # (跟apply_family_folder_merges末尾同一套判断:还有任何文件就不删)。
    removed_folders = []
    for folder_name in source_folders:
        if folder_name == new_folder:
            continue
        folder_path = library_root / folder_name
        if not folder_path.is_dir():
            continue
        if any(p.is_file() for p in folder_path.rglob("*")):
            continue
        shutil.rmtree(folder_path, ignore_errors=True)
        stale = (
            db.query(models.LocalMedia)
            .filter(models.LocalMedia.folder_name == folder_name)
            .first()
        )
        if stale:
            db.delete(stale)
            db.commit()
        removed_folders.append(folder_name)

    # 搬过来的文件名里还带着旧归属的标题/季号,按新归属重排一遍——限定只扫这一个
    # 文件夹,不做全库扫描(那会对每部番都发一轮Bangumi请求)。复用"修复媒体库"
    # 同一套逻辑,被blocked的条目(目标已存在/同批撞车)照旧跳过,不会覆盖文件。
    # 刻意不调reset_season_cache:清空家族缓存是全库手动修复的动作,
    # 每次调整归属都清一次会白白引发大量联网重算。
    renamed = {"succeeded": [], "skipped": [], "failed": []}
    if moved:
        try:
            renamed = await apply_rename_fixes(db, selected_paths=None, only_folders={new_folder})
        except Exception as e:
            # 重排失败不影响"文件已经搬过去了"这个既成事实,把错误带回前端让用户知道
            # 可以再手动跑一次修复,不要把整个请求打成500。
            print(f"[REGROUP] 自动重排失败 folder={new_folder}: {e}")
            renamed = {"succeeded": [], "skipped": [], "failed": [{"error": str(e)}]}

    # "未看集数"角标:这批移动精确知道从哪个文件夹搬出多少个视频文件、搬进新文件夹
    # 多少个,直接算术调整,不用等下次扫描——文件已经在磁盘上挪好、数字在同一个请求
    # 里一起改完,不会有"文件挪了但数字没跟上"的窗口。
    if moved:
        moved_out_by_folder: dict[str, int] = {}
        for m in moved:
            src_folder = m["from"].split("/", 1)[0]
            moved_out_by_folder[src_folder] = moved_out_by_folder.get(src_folder, 0) + 1
        for src_folder, n in moved_out_by_folder.items():
            if src_folder == new_folder or src_folder in removed_folders:
                continue  # 搬空删掉的文件夹LocalMedia行已经没了,不用调
            src_media = db.query(models.LocalMedia).filter(models.LocalMedia.folder_name == src_folder).first()
            if src_media and src_media.episode_file_count is not None:
                src_media.episode_file_count = max(src_media.episode_file_count - n, 0)
                # 已看分子没法在这里精确加减(要知道搬走的那几个各自看没看),置 None
                # 交给下次 backfill / 详情页按新结构重算;在那之前这个文件夹不显示角标。
                src_media.watched_episode_count = None
                src_media.episode_count_updated_at = datetime.now()
        dest_media = db.query(models.LocalMedia).filter(models.LocalMedia.folder_name == new_folder).first()
        if dest_media:
            if dest_media.episode_file_count is not None:
                dest_media.episode_file_count += len(moved)
                dest_media.watched_episode_count = None
                dest_media.episode_count_updated_at = datetime.now()
            elif target_media is None:
                # target_media是上面"新文件夹要有自己的LocalMedia行"那段判断出的:这次是
                # 全新建的文件夹,搬进来的就是它现在拥有的全部文件,可以当准确初始值。
                # 已有文件夹但之前没扫过(target_media非None)则不猜,留None交给下次扫描/详情页补。
                dest_media.episode_file_count = len(moved)
                dest_media.watched_episode_count = None
                dest_media.episode_count_updated_at = datetime.now()
        db.commit()

    _detail_structure_cache.clear()  # 目录结构变了,详情页缓存整体失效
    return {
        "status": "success",
        "target_folder": new_folder,
        "moved": moved,
        "skipped": skipped,
        "failed": failed,
        "removed_folders": removed_folders,
        "renamed": renamed,
    }


@router.get("/library/scan")
async def scan_and_update_library(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """
    扫描媒体库。扫描后，如果发现新绑定的 bgm_id，会自动异步向 Bangumi 请求最新的数据
    """
    library_root = get_library_root(db)
    print("正在扫盘，目标路径：", library_root.resolve())

    if not library_root.exists():
        print(f"警告：目标路径 {library_root} 不存在，请检查设置页面配置！")
        return {"message": f"Library root {library_root} does not exist.", "added": [], "current_total": 0}

    # 一次os.scandir同时拿到子目录名和各自mtime:scandir的entry.stat()走的是目录读取时
    # 已缓存的元数据,避免了原先"iterdir列目录 + 再对每部番单独Path.stat()"的N次额外stat
    # (网络共享媒体库下每次stat都是一次SMB往返,番剧一多就明显卡)。
    folder_mtimes: dict[str, datetime] = {}
    with os.scandir(library_root) as it:
        for entry in it:
            if entry.is_dir():
                try:
                    folder_mtimes[entry.name] = datetime.fromtimestamp(entry.stat().st_mtime)
                except OSError:
                    folder_mtimes[entry.name] = None
    local_folders = list(folder_mtimes.keys())
    local_folder_set = set(local_folders)

    db_media = db.query(models.LocalMedia).all()
    db_folder_names = {m.folder_name for m in db_media}

    added = []
    bgm_pattern = re.compile(r"\[bgm-(\d+)\]")

    for folder in local_folders:
        if folder not in db_folder_names:
            bgm_id = None
            match = bgm_pattern.search(folder)
            if match:
                bgm_id = int(match.group(1))
            else:
                catalog_match = db.query(models.AnimeCatalog).filter(
                    (models.AnimeCatalog.title == folder) |
                    (models.AnimeCatalog.title_original == folder)
                ).first()
                if catalog_match:
                    bgm_id = catalog_match.bgm_id

            new_media = models.LocalMedia(folder_name=folder, bgm_id=bgm_id)
            db.add(new_media)
            added.append(folder)

            # 新入库且绑定了bgm_id的番:补详情元数据丢后台任务,不在扫盘里同步await一次
            # Bangumi网络请求(新增一批番时会把整个扫盘接口卡在串行网络请求上)。跟
            # list_library_animes遇到无缓存时同一种处理,下次列表请求即可看到补全结果。
            if bgm_id:
                background_tasks.add_task(_update_anime_details_from_bgm_task, bgm_id)

    for media in db_media:
        if media.folder_name not in local_folder_set:
            db.delete(media)

    db.commit()

    # 刷新每部番的"最近活动时间",不止新增文件夹——已有番剧新增文件也要能反映到排序上。
    # 直接用上面scandir已经拿到的mtime,不再对每部番单独get_latest_activity(再stat一次)。
    for media in db.query(models.LocalMedia).all():
        media.latest_activity_at = folder_mtimes.get(media.folder_name)
    db.commit()

    # 媒体库卡片"未看集数"角标:挂载页面/切回tab/点这个按钮都会走到这里,顺手把还没扫过
    # 集数的文件夹补一遍——扔进background_tasks,不阻塞这个接口本身的响应。开关关掉时
    # 完全跳过,不产生任何这个功能相关的磁盘IO。
    if get_setting(
        db, "library_unwatched_badge_enabled", config_store.DEFAULTS["library_unwatched_badge_enabled"]
    ) == "true":
        background_tasks.add_task(_backfill_missing_episode_counts, library_root)

    return {"status": "success", "added": added, "current_total": len(local_folders)}


@router.get("/library/animes")
async def list_library_animes(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """
    获取全部动漫列表。
    【重要修改】：不再保留左右侧原名对比。如果能取到 Bangumi 数据，直接使用 Bangumi 整理后的
    “中文标题”、“图片封面”、“剧集简介”、“总集数” 融合成一张卡片数据向前端渲染。

    读接口:不做全量扫盘(见GET /library/scan——全量扫盘入口,由前端挂载时和
    "刷新 & 扫盘"按钮显式调用),只在开了"未看集数"角标时顺手丢一个后台补课任务
    (_backfill_missing_episode_counts,内部按目录签名廉价短路,几乎不产生 IO)。
    也不再服务端排序(排序字段 latest_activity_at/last_watched_at 本来就随这个接口
    返回,前端本地排序即可)。
    """
    local_medias = db.query(models.LocalMedia).all()
    cover_strategy = get_setting(
        db, "library_cover_strategy", config_store.DEFAULTS["library_cover_strategy"]
    )

    # 每个文件夹最近一次观看时间,直接从PlaybackRecord表聚合,不碰硬盘
    last_watched_map = dict(
        db.query(models.PlaybackRecord.folder_name, func.max(models.PlaybackRecord.watched_at))
        .group_by(models.PlaybackRecord.folder_name)
        .all()
    )

    # "未看集数"角标:纯读两个缓存列(episode_file_count 正片总数 / watched_episode_count
    # 正片已看数),不碰硬盘。两列由 _count_episode_files 同一次扫描算出、成对维护,读不到
    # (任一为 None,还没被扫过)时角标直接不显示,不现扫。
    badge_enabled = get_setting(
        db, "library_unwatched_badge_enabled", config_store.DEFAULTS["library_unwatched_badge_enabled"]
    ) == "true"
    # 触发一次后台补课:_backfill 内部"签名没变就跳过",这里加进来只是让 NULL 行 / 目录
    # 变过的行在每次网格请求时持续被补/重试,不再只能靠挂载时那一次 /library/scan——
    # 尤其 RSS 追更整理入库后 episode_file_count 会被置 None,不然要等用户手动扫盘。
    if badge_enabled:
        background_tasks.add_task(_backfill_missing_episode_counts, get_library_root(db))

    response_data = []
    for media in local_medias:
        # 兜底默认值：若未关联 Bangumi，显示本地文件夹名去除 [bgm-xxx] 的纯文本
        display_title = re.sub(r"\s*\[bgm-\d+\]", "", media.folder_name).strip()
        cover_url = None
        summary = "暂无简介"
        total_episodes = 0  # 默认总集数

        if media.bgm_id:
            # 标题/简介/集数仍取绑定bgm_id的AnimeCatalog;封面另按cover_bgm_id(手动选图或
            # 默认策略解析出的家族成员)取——cover_bgm_id为空时回退到绑定bgm_id自身的图。
            catalog = db.query(models.AnimeCatalog).filter(models.AnimeCatalog.bgm_id == media.bgm_id).first()
            if catalog:
                # 统一融合成一个标准数据，直接使用 Bangumi 的中文名替换原本地物理文件夹名
                display_title = catalog.title or display_title
                summary = catalog.summary or "暂无简介"
                # 尝试从数据库对象中取 total_episodes，如果没这列，它在更新后会被赋值
                total_episodes = getattr(catalog, "total_episodes", 0) or 0
                # 老坏行(简介只剩占位串 / 封面空):后台补一次,下次列表就正常了
                if _catalog_summary_stale(catalog) or not catalog.cover_url:
                    background_tasks.add_task(_update_anime_details_from_bgm_task, media.bgm_id)
            else:
                # 若本地暂无缓存，扔进后台任务补一次更新，不阻塞这次列表响应——
                # 这次请求先用兜底文案返回，下一次任意一次列表请求就能看到补全结果。
                background_tasks.add_task(_update_anime_details_from_bgm_task, media.bgm_id)

            # 封面取值:cover_bgm_id优先,回退绑定bgm_id;对应AnimeCatalog缺失就后台补。
            cover_bid = media.cover_bgm_id or media.bgm_id
            cover_catalog = (
                catalog if cover_bid == media.bgm_id
                else db.query(models.AnimeCatalog).filter(models.AnimeCatalog.bgm_id == cover_bid).first()
            )
            if cover_catalog:
                cover_url = cover_catalog.cover_url
                # 简介跟随封面:手动选图 / 默认策略选中家族里另一部时,简介也换成那一部的,
                # 保证卡片上"封面↔简介"是同一部作品。标题/集数仍取绑定条目,不动。
                if cover_bid != media.bgm_id and not _catalog_summary_stale(cover_catalog):
                    summary = cover_catalog.summary
            else:
                background_tasks.add_task(_update_anime_details_from_bgm_task, cover_bid)

            # 惰性触发默认封面策略:未手动选图、还没解析过cover_bgm_id、且策略非matched时,
            # 后台按策略(最新TV季/第一季)解析出该用哪个家族成员的图,下次列表即生效。
            if (
                media.cover_bgm_id is None
                and not media.cover_is_custom
                and cover_strategy != "matched"
            ):
                background_tasks.add_task(_resolve_cover_bgm_id_task, media.folder_name)

        unwatched_count = 0
        # 两列都得有值才算——任一为 None 说明还没扫过 / 刚失效,不显示角标(上面已排了补课)
        if badge_enabled and media.episode_file_count is not None and media.watched_episode_count is not None:
            unwatched_count = max(media.episode_file_count - media.watched_episode_count, 0)

        response_data.append({
            "id": media.id,
            "folder_name": media.folder_name,
            "display_title": display_title,      # 合并后的完美中文标题
            "bgm_id": media.bgm_id,
            "cover_url": cover_url,              # 封面大图
            "summary": summary,                  # 简介
            "total_episodes": total_episodes,    # 总集数
            "latest_activity_at": media.latest_activity_at,
            "last_watched_at": last_watched_map.get(media.folder_name),
            "unwatched_count": unwatched_count,  # 未看集数角标;开关关闭或还没扫过时恒为0
        })

    for item in response_data:
        item["latest_activity_at"] = item["latest_activity_at"].isoformat() if item["latest_activity_at"] else None
        item["last_watched_at"] = item["last_watched_at"].isoformat() if item["last_watched_at"] else None

    return response_data


@router.get("/library/detail/{folder_name}")
def get_anime_seasons_and_episodes(folder_name: str, db: Session = Depends(get_db)):
    library_root = get_library_root(db)
    anime_path = library_root / folder_name

    # 先算目录签名(一次scandir)。签名为None说明文件夹不存在。
    signature = _folder_structure_signature(anime_path)
    if signature is None:
        _detail_structure_cache.pop(folder_name, None)
        raise HTTPException(status_code=404, detail="Anime folder not found on disk.")

    # 这部番的家族根:拆出去的文件夹自己的bgm_id名下没有家族行,必须换算成
    # Bangumi客观家族根才查得到成员(跟library_repair._resolve_season_table同一个道理)。
    # 详情页是同步接口、每次渲染都会走,只查缓存不联网。
    media = (
        db.query(models.LocalMedia)
        .filter(models.LocalMedia.folder_name == folder_name)
        .first()
    )
    family_root = None
    if media and media.bgm_id:
        family_root = bgm_series_cache.cached_auto_root(db, media.bgm_id) or media.bgm_id
    extra_buckets = bgm_series_cache.family_work_title_buckets(db, family_root)

    # 结构缓存的key要带上extra_buckets:磁盘目录没变、但家族缓存重算之后合法桶名
    # 可能变了(作品名目录 <-> Specials/Others),只按目录签名判断会读到陈旧结构。
    cache_key = (signature, frozenset(extra_buckets))
    cached = _detail_structure_cache.get(folder_name)
    if cached and cached[0] == cache_key:
        base_structure = cached[1]
    else:
        base_structure = scan_local_folder_structure(anime_path, library_root, extra_buckets)
        _detail_structure_cache[folder_name] = (cache_key, base_structure)

    # 顺手更新"未看集数"角标用的缓存列:详情页本来就在算这个文件夹的真实结构,
    # 免费拿到准确数字,不用等/library/scan的后台补课轮到这个文件夹。同时核对一遍
    # 播放记录里有没有文件已经不在磁盘上了(见_flag_stale_playback_records)。
    if media:
        _flag_stale_playback_records(db, folder_name, base_structure)
        media.episode_file_count = _sum_real_episodes(base_structure)
        media.watched_episode_count = _count_watched_real_episodes(db, folder_name, base_structure)
        media.episode_count_signature = _serialize_signature(signature)
        media.episode_count_updated_at = datetime.now()
        db.commit()

    # 提取已播放记录
    watched_records = db.query(models.PlaybackRecord).filter(
        models.PlaybackRecord.folder_name == folder_name
    ).all()

    # 修复点：转为字典，映射 filename -> record，方便取时间
    watched_map = {record.filename: record for record in watched_records}

    # 深拷一份再叠加观看态:不能直接改缓存里的base_structure(会把is_watched等固化进缓存)。
    structure = {
        season: [dict(ep) for ep in eps]
        for season, eps in base_structure.items()
    }
    for season, eps in structure.items():
        for ep in eps:
            record = watched_map.get(ep["filename"])
            if record:
                ep["is_watched"] = True
                # 格式化时间并注入
                ep["watched_at"] = record.watched_at.strftime("%Y-%m-%d %H:%M:%S") if record.watched_at else None
            else:
                ep["is_watched"] = False
                ep["watched_at"] = None

    # 每个桶对应家族里的哪一部(拆分/合并归属时要拿它当覆盖的主键)。
    # 只给"这个桶唯一对应一个成员"的情况——Season NN 是一对一,而剧场版/OVA 桶
    # 往往塞着几十部,给不出唯一的归属主体,前端那边也就不显示拆分入口。
    # 作品名目录天生一部一个桶,所以那些过去全挤在"Season 00"里、拿不到归属的旁支,
    # 现在自动都有了拆分/合并入口。
    # bucket_dates 给前端排序用:Season NN 之后的作品名目录按首播日期排,不按字符串序。
    season_owners: dict[str, dict] = {}
    bucket_dates: dict[str, str] = {}
    if family_root:
        by_bucket: dict[str, list] = {}
        for m in (
            db.query(models.AnimeFamilyCache)
            .filter(models.AnimeFamilyCache.source_bgm_id == family_root)
            .all()
        ):
            if m.folder_bucket:
                by_bucket.setdefault(m.folder_bucket, []).append(m)
        season_owners = {
            bucket: {"bgm_id": members[0].bgm_id, "name": members[0].name}
            for bucket, members in by_bucket.items()
            if len(members) == 1
        }
        for bucket, members in by_bucket.items():
            dates = sorted(m.date for m in members if m.date)
            if dates:
                bucket_dates[bucket] = dates[0]

    return {
        "folder_name": folder_name,
        "seasons": structure,
        "season_owners": season_owners,
        "bucket_dates": bucket_dates,
    }


async def _resolve_anime_meta_task(bgm_id: int) -> None:
    """后台任务版本,仿_update_anime_details_from_bgm_task:GET /anime-meta/{bgm_id}
    遇到从没解析过的bgm_id时用这个,不阻塞本次响应等一轮真实的TMDB/arm-server网络请求
    ——这次先返回status=pending,前端按降级态渲染,下次请求就有数据了。
    """
    db = SessionLocal()
    try:
        await anime_meta_resolver.resolve_one(db, bgm_id)
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"[ANIME_META] 后台解析bgm_id={bgm_id}失败: {e}")
    finally:
        db.close()


# 详情页反复进出时,同一部番的按需刷新最多隔这么久排一次队(避免 background_tasks 堆积)。
_META_REFRESH_MIN_INTERVAL = timedelta(hours=1)


async def _refresh_anime_meta_task(bgm_id: int) -> None:
    """已 resolved 但解析逻辑版本落后的行,进详情页时后台按新逻辑重取一次。
    失败不降级(旧图继续显示),成功则替换成新图。"""
    db = SessionLocal()
    try:
        await anime_meta_resolver.resolve_one(db, bgm_id, is_refresh=True)
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"[ANIME_META] 后台刷新bgm_id={bgm_id}失败: {e}")
    finally:
        db.close()


@router.get("/anime-meta/{bgm_id}")
def get_anime_meta(bgm_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """媒体库详情页头部(背景图/LOGO/分级/标签/类型/工作室)的数据源。
    查无记录(从没触发过解析)时后台丢一个解析任务,本次先返回status=pending,
    前端据此走降级态(模糊放大的Bangumi封面+文字标题),不阻塞等待网络请求。
    """
    row = db.query(models.AnimeMetaCache).filter(models.AnimeMetaCache.bgm_id == bgm_id).first()
    if row is None:
        background_tasks.add_task(_resolve_anime_meta_task, bgm_id)
        return {"bgm_id": bgm_id, "status": "pending"}

    # 已解析但解析逻辑版本落后:照旧返回当前(旧)图,后台按新逻辑重取一次替换掉。
    # 用 last_attempt_at 节流,避免反复进出详情页把刷新任务堆满。
    if row.status == "resolved" and (row.resolver_version or 0) < anime_meta_resolver.META_RESOLVER_VERSION:
        last = row.last_attempt_at
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if last is None or last < datetime.now(timezone.utc) - _META_REFRESH_MIN_INTERVAL:
            background_tasks.add_task(_refresh_anime_meta_task, bgm_id)

    return {
        "bgm_id": bgm_id,
        "status": row.status,
        "tmdb_id": row.tmdb_id,
        "backdrop_url": row.backdrop_url,
        "logo_url": row.logo_url,
        "content_rating": row.content_rating,
        "genres": row.genres.split(",") if row.genres else [],
        "tags": row.tags.split(",") if row.tags else [],
        "studios": row.studios.split(",") if row.studios else [],
        "creators": row.creators.split(",") if row.creators else [],
    }


@router.get("/library/play")
def play_video(file_path: str, db: Session = Depends(get_db)):
    library_root = get_library_root(db)
    target_path = library_root / file_path
    if not target_path.resolve().is_relative_to(library_root.resolve()):
        raise HTTPException(status_code=403, detail="Access denied.")

    if not target_path.exists():
        raise HTTPException(status_code=404, detail="Video file not found.")

    return FileResponse(target_path, media_type="video/mp4")


@router.delete("/library/animes/{folder_name}")
def delete_anime(folder_name: str, db: Session = Depends(get_db)):
    """删除整部番:物理删除媒体库里的文件夹 + 清掉LocalMedia关联记录。

    PlaybackRecord(播放记录)故意不清理——用户明确要求保留观看历史,
    孤立的记录不影响任何现有功能(/library/detail按folder_name查询,
    文件夹都没了自然查不到,不会渲染出任何东西)。
    """
    media = db.query(models.LocalMedia).filter(models.LocalMedia.folder_name == folder_name).first()
    if not media:
        raise HTTPException(status_code=404, detail="Anime not found in library.")

    library_root = get_library_root(db)
    anime_path = library_root / folder_name
    if not anime_path.resolve().is_relative_to(library_root.resolve()):
        raise HTTPException(status_code=403, detail="Access denied.")

    if anime_path.exists():
        try:
            shutil.rmtree(anime_path)
        except OSError as e:
            # 物理删除失败(权限/文件被占用):不删数据库行,保持磁盘和记录状态一致,
            # 不然下次扫描会因为记录已经没了而把这部番当成"新文件夹"重新入库。
            raise HTTPException(status_code=500, detail=f"删除文件夹失败: {e}")

    # 级联:清掉这部番登记在"剧场版模式"里的独立卡,避免留下指向已删文件夹的悬挂行。
    db.query(models.StandaloneMedia).filter(
        models.StandaloneMedia.library_folder == folder_name
    ).delete(synchronize_session=False)

    db.delete(media)
    db.commit()
    return {"status": "success", "folder_name": folder_name}


class EpisodeDeleteRequest(BaseModel):
    rel_path: str  # 相对library_root的路径,取值方式跟/library/play的file_path参数一致


@router.post("/library/episode/delete")
def delete_episode(req: EpisodeDeleteRequest, db: Session = Depends(get_db)):
    """删除单集视频文件,以及同目录下同名的字幕文件(.srt/.ass等,复用
    rename_engine.SUBTITLE_EXTS常量)。PlaybackRecord同样不清理,理由跟
    delete_anime一致。
    """
    library_root = get_library_root(db)
    target_path = library_root / req.rel_path
    if not target_path.resolve().is_relative_to(library_root.resolve()):
        raise HTTPException(status_code=403, detail="Access denied.")

    if not target_path.exists():
        raise HTTPException(status_code=404, detail="Video file not found.")

    for sibling in target_path.parent.glob(f"{glob.escape(target_path.stem)}.*"):
        if sibling == target_path:
            continue
        if sibling.suffix.lstrip(".").lower() in rename_engine.SUBTITLE_EXTS:
            try:
                sibling.unlink()
            except OSError as e:
                print(f"[LIBRARY] 删除字幕文件失败,不影响视频删除: {sibling}: {e}")

    target_path.unlink()

    # 级联:文件删了,清掉它在"剧场版模式"里的独立卡(rel_path 归一化后比对)。
    db.query(models.StandaloneMedia).filter(
        models.StandaloneMedia.rel_path == _norm_rel(req.rel_path)
    ).delete(synchronize_session=False)

    # "未看集数"角标:这一刻精确知道少了1个文件,直接算术-1,不用等下次扫描——
    # 只在当前值非None时才减(是None说明还没被扫过,留给/library/scan或详情页去补,
    # 不瞎猜),clamp到>=0防御性处理。但只有删的是正片桶(Season/剧场版/OVA/家族桶)里
    # 的文件才减:删的是"Other"/"Specials/Others"这类本来就没被计入分母的附赠内容,
    # 分母不该跟着掉,不然会把角标衬得比实际"还剩几集"更小。
    rel_parts = _norm_rel(req.rel_path).split("/")
    folder_name = rel_parts[0]
    media = db.query(models.LocalMedia).filter(models.LocalMedia.folder_name == folder_name).first()
    is_real_episode = True
    if len(rel_parts) >= 3:  # 文件在某个子目录下,不是直接堆在番剧根目录
        family_root = None
        if media and media.bgm_id:
            family_root = bgm_series_cache.cached_auto_root(db, media.bgm_id) or media.bgm_id
        extra_buckets = bgm_series_cache.family_work_title_buckets(db, family_root)
        is_real_episode = _bucket_name_for_subdir(rel_parts[1], extra_buckets) not in _NON_EPISODE_BUCKETS
    if media and media.episode_file_count is not None and is_real_episode:
        media.episode_file_count = max(media.episode_file_count - 1, 0)
        media.episode_count_updated_at = datetime.now()
        # 分子跟着分母走:被删的这一集之前看过(有非 stale 记录)的话,已看数也 -1,
        # 保持 unwatched = 分母-分子 不变(删的是没看的才会让角标 -1)。
        if media.watched_episode_count is not None:
            was_watched = (
                db.query(models.PlaybackRecord)
                .filter(
                    models.PlaybackRecord.folder_name == folder_name,
                    models.PlaybackRecord.filename == rel_parts[-1],
                    models.PlaybackRecord.is_stale.is_(False),
                )
                .first()
            )
            if was_watched:
                media.watched_episode_count = max(media.watched_episode_count - 1, 0)

    db.commit()
    return {"status": "success", "rel_path": req.rel_path}