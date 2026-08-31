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
from services import bgm_series_cache
from bangumi_client import get_subject_detail, normalize_bgm_subject, get_subject_details_batch
from datetime import datetime

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
# ----------------- 数据模型定义 -----------------
class WatchedRequest(BaseModel):
    folder_name: str
    filename: str

# ----------------- 播放记录 API -----------------
@router.post("/library/watch")
def mark_as_watched(req: WatchedRequest, db: Session = Depends(get_db)):
    """标记为已播放，如果已存在则更新最后观看时间"""
    record = db.query(models.PlaybackRecord).filter(
        models.PlaybackRecord.folder_name == req.folder_name,
        models.PlaybackRecord.filename == req.filename
    ).first() #[cite: 15]
    
    if not record:
        new_record = models.PlaybackRecord(
            folder_name=req.folder_name,
            filename=req.filename,
            watched_at=datetime.now() # 记录当前时间
        )
        db.add(new_record)
    else:
        # 修复点：如果记录存在，更新观看时间
        record.watched_at = datetime.now()
        
    db.commit() #[cite: 15]
    return {"status": "success", "message": "Marked as watched"}

@router.post("/library/unwatch")
def unmark_watched(req: WatchedRequest, db: Session = Depends(get_db)):
    """取消已播放标记（用于误触恢复）"""
    db.query(models.PlaybackRecord).filter(
        models.PlaybackRecord.folder_name == req.folder_name,
        models.PlaybackRecord.filename == req.filename
    ).delete()
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
                # rename_engine.py 实际会创建的子目录名只有这几种:Season NN、Season 00(OVA)、
                # 剧场版、Other、以及算不出季号时按作品名命名的目录(extra_buckets)——
                # 这几个要保留各自的桶,不能被下面的兜底正则一起归进"Specials/Others"。
                # 季度目录的正则用rename_engine那一份,不在这里另写一个:
                # work_title_bucket()要靠同一个正则避开会被误认成季度目录的作品名。
                season_match = rename_engine.SEASON_DIR_PATTERN.match(entry.name)
                is_known_bucket = (
                    bool(season_match)
                    or entry.name in ("剧场版", "劇場版", "Other", "OVA")
                    or entry.name in (extra_buckets or ())
                )
                season_name = entry.name if is_known_bucket else "Specials/Others"
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
async def match_anime(req: MatchRequest, db: Session = Depends(get_db)):
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

    media.bgm_id = req.bgm_id
    db.commit()

    await update_anime_details_from_bgm(db, req.bgm_id)
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
        if not catalog:
            # 封面/标题/简介还没缓存:后台补一次,这次先用兜底,下次列表就有了。
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

    return {"status": "success", "added": added, "current_total": len(local_folders)}


@router.get("/library/animes")
async def list_library_animes(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """
    获取全部动漫列表。
    【重要修改】：不再保留左右侧原名对比。如果能取到 Bangumi 数据，直接使用 Bangumi 整理后的
    “中文标题”、“图片封面”、“剧集简介”、“总集数” 融合成一张卡片数据向前端渲染。

    纯读接口:不再顺带触发扫盘(见GET /library/scan——现在是唯一的扫盘入口,
    由前端挂载时和"刷新 & 扫盘"按钮显式调用),也不再服务端排序(排序字段
    latest_activity_at/last_watched_at本来就随这个接口返回,前端自己按需要
    的模式本地排序即可,不用为了换排序方式重新请求)。
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
                if cover_bid != media.bgm_id and cover_catalog.summary:
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
    db.commit()
    return {"status": "success", "rel_path": req.rel_path}