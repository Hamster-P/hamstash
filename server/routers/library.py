# routers/library.py
import os
import re
import platform
import asyncio
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import func
from sqlalchemy.orm import Session
from pydantic import BaseModel

import models
from database import get_db
from services.common import get_setting
from bangumi_client import get_subject_detail, normalize_bgm_subject
from datetime import datetime

router = APIRouter(tags=["影视库"])

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".flv"}
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

    for item in anime_path.iterdir():
        if item.is_dir():
            # rename_engine.py 实际会创建的子目录名只有这几种:Season NN、Season 00(OVA)、
            # 剧场版、Other——这几个要保留各自的桶,不能被下面的兜底正则一起归进"Specials/Others"
            season_match = re.match(r"^(Season\s*|S|Specials|SP|第\s*)(\d+|[a-zA-Z]+)", item.name, re.IGNORECASE)
            is_known_bucket = bool(season_match) or item.name in ("剧场版", "劇場版", "Other", "OVA")
            season_name = item.name if is_known_bucket else "Specials/Others"
            episodes = []
            for sub_item in item.rglob("*"):
                if sub_item.is_file() and sub_item.suffix.lower() in VIDEO_EXTENSIONS:
                    episodes.append({
                        "filename": sub_item.name,
                        "rel_path": str(sub_item.relative_to(library_root)).replace("\\", "/")
                    })
            if episodes:
                # 用extend而不是直接赋值:避免不同子目录被归到同一个桶名时(比如两个都落进
                # "Specials/Others"兜底桶)后处理的目录把前一个目录的集数覆盖掉
                structure.setdefault(season_name, []).extend(episodes)
        else:
            if item.is_file() and item.suffix.lower() in VIDEO_EXTENSIONS:
                # 视频直接堆在番剧根目录(没有Season子目录)时,靠文件名里的关键词
                # 区分是剧场版/OVA合集还是正常的单季番,不再一律标成"Season 1"
                is_movie = re.search(r"剧场版|劇場版|movie|OVA", item.name, re.IGNORECASE)
                bucket_name = "剧场版/OVA" if is_movie else "Season 1"
                structure.setdefault(bucket_name, []).append({
                    "filename": item.name,
                    "rel_path": str(item.relative_to(library_root)).replace("\\", "/")
                })
    for episodes in structure.values():
        episodes.sort(key=lambda x: x["filename"])
    return structure


def get_latest_activity(anime_path: Path) -> datetime | None:
    """
    用番剧根目录自身 + 各Season子目录自身的mtime取最大值,
    近似代表这部番"最近一次有新内容落地"的时间——不逐个视频文件stat,
    换取几百部番一次扫描也能保持轻量。
    """
    if not anime_path.exists():
        return None
    mtimes = [anime_path.stat().st_mtime]
    for item in anime_path.iterdir():
        if item.is_dir():
            mtimes.append(item.stat().st_mtime)
    return datetime.fromtimestamp(max(mtimes))


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
async def scan_and_update_library(db: Session = Depends(get_db)):
    """
    扫描媒体库。扫描后，如果发现新绑定的 bgm_id，会自动异步向 Bangumi 请求最新的数据
    """
    library_root = get_library_root(db)
    print("正在扫盘，目标路径：", library_root.resolve())

    if not library_root.exists():
        print(f"警告：目标路径 {library_root} 不存在，请检查设置页面配置！")
        return {"message": f"Library root {library_root} does not exist.", "added": [], "current_total": 0}

    local_folders = [f.name for f in library_root.iterdir() if f.is_dir()]
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
            
            # 如果新入库的动漫绑定了 bgm_id，立即尝试更新其 Bangumi 详细元数据
            if bgm_id:
                await update_anime_details_from_bgm(db, bgm_id)
            
    for media in db_media:
        if media.folder_name not in local_folders:
            db.delete(media)

    db.commit()

    # 刷新每部番的"最近活动时间",不止新增文件夹——已有番剧新增文件也要能反映到排序上。
    for media in db.query(models.LocalMedia).all():
        media.latest_activity_at = get_latest_activity(library_root / media.folder_name)
    db.commit()

    return {"status": "success", "added": added, "current_total": len(local_folders)}


@router.get("/library/animes")
async def list_library_animes(sort: str = "default", db: Session = Depends(get_db)):
    """
    获取全部动漫列表。
    【重要修改】：不再保留左右侧原名对比。如果能取到 Bangumi 数据，直接使用 Bangumi 整理后的
    “中文标题”、“图片封面”、“剧集简介”、“总集数” 融合成一张卡片数据向前端渲染。

    sort: default(原始顺序) / recent_watched(最近观看优先) / recent_updated(最新更新优先)
    """
    await scan_and_update_library(db)
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
                # 若本地暂无缓存，则异步触发一次单条更新，以备下一次加载使用
                await update_anime_details_from_bgm(db, media.bgm_id)

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

    if sort == "recent_watched":
        response_data.sort(key=lambda x: x["last_watched_at"] or datetime.min, reverse=True)
    elif sort == "recent_updated":
        response_data.sort(key=lambda x: x["latest_activity_at"] or datetime.min, reverse=True)

    for item in response_data:
        item["latest_activity_at"] = item["latest_activity_at"].isoformat() if item["latest_activity_at"] else None
        item["last_watched_at"] = item["last_watched_at"].isoformat() if item["last_watched_at"] else None

    return response_data


@router.get("/library/detail/{folder_name}")
def get_anime_seasons_and_episodes(folder_name: str, db: Session = Depends(get_db)):
    library_root = get_library_root(db) 
    anime_path = library_root / folder_name
    if not anime_path.exists() or not anime_path.is_dir():
        raise HTTPException(status_code=404, detail="Anime folder not found on disk.") 

    structure = scan_local_folder_structure(anime_path, library_root) 

    # 提取已播放记录
    watched_records = db.query(models.PlaybackRecord).filter(
        models.PlaybackRecord.folder_name == folder_name
    ).all() 
    
    # 修复点：转为字典，映射 filename -> record，方便取时间
    watched_map = {record.filename: record for record in watched_records}

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