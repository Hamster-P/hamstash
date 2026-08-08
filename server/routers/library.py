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
from bangumi_client import get_subject_detail, normalize_bgm_subject
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


def scan_local_folder_structure(anime_path: Path, library_root: Path):
    """
    扫描单部动漫文件夹下的物理结构。
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
                # 剧场版、Other——这几个要保留各自的桶,不能被下面的兜底正则一起归进"Specials/Others"
                season_match = re.match(r"^(Season\s*|S|Specials|SP|第\s*)(\d+|[a-zA-Z]+)", entry.name, re.IGNORECASE)
                is_known_bucket = bool(season_match) or entry.name in ("剧场版", "劇場版", "Other", "OVA")
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
            # 优先从本地数据库里的 AnimeCatalog 查详情缓存
            catalog = db.query(models.AnimeCatalog).filter(models.AnimeCatalog.bgm_id == media.bgm_id).first()
            if catalog:
                # 统一融合成一个标准数据，直接使用 Bangumi 的中文名替换原本地物理文件夹名
                display_title = catalog.title or display_title
                cover_url = catalog.cover_url
                summary = catalog.summary or "暂无简介"
                # 尝试从数据库对象中取 total_episodes，如果没这列，它在更新后会被赋值
                total_episodes = getattr(catalog, "total_episodes", 0) or 0
            else:
                # 若本地暂无缓存，扔进后台任务补一次更新，不阻塞这次列表响应——
                # 这次请求先用兜底文案返回，下一次任意一次列表请求就能看到补全结果。
                background_tasks.add_task(_update_anime_details_from_bgm_task, media.bgm_id)

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

    cached = _detail_structure_cache.get(folder_name)
    if cached and cached[0] == signature:
        base_structure = cached[1]
    else:
        base_structure = scan_local_folder_structure(anime_path, library_root)
        _detail_structure_cache[folder_name] = (signature, base_structure)

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

    return {
        "folder_name": folder_name,
        "seasons": structure
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
    return {"status": "success", "rel_path": req.rel_path}