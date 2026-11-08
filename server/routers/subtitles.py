"""用户手动触发的字幕搜索/预览/保存；不创建 RSS 或后台补字幕任务。"""
import asyncio
import re
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db
import models
from routers.library import get_library_root
from services import subtitles
from services.folder_size import refresh_folder_size
from services.rss_season_complete import _episode_offset
from bangumi_client import get_subject_detail

router = APIRouter(prefix="/library/subtitles", tags=["字幕"])
_aliases = {}


class SearchRequest(BaseModel):
    rel_path: str = Field(min_length=1, max_length=2048)
    query: str = Field(min_length=1, max_length=240)
    bgm_id: int | None = None
    preferred_language: str = Field(default="chs", pattern=r"^(?:any|chs|cht|chs-jp|cht-jp|chs-en|cht-en|en|ja|ko|unknown)$")
    preferred_release: str = Field(default="", max_length=80)


class Selection(BaseModel):
    rel_path: str = Field(min_length=1, max_length=2048)
    candidate_id: str = Field(min_length=1, max_length=100)
    language: str | None = Field(default=None, pattern=r"^(?:chs|cht|chs-jp|cht-jp|chs-en|cht-en|en|ja|ko|unknown)$")


class SaveRequest(BaseModel):
    items: list[Selection] = Field(min_length=1, max_length=100)


def subject_aliases(detail: dict) -> list[str]:
    result = [detail.get("name", ""), detail.get("name_cn", "")]
    for field in detail.get("infobox", []):
        if not isinstance(field, dict) or field.get("key") not in {"别名", "英文名", "中文名", "日文名", "原名"}:
            continue
        value = field.get("value")
        if isinstance(value, str):
            result.append(value)
        elif isinstance(value, list):
            result.extend(item.get("v", "") for item in value if isinstance(item, dict))
    return list(dict.fromkeys(value for value in result if isinstance(value, str) and value))


@router.post("/search")
async def search_subtitles(req: SearchRequest, db: Session = Depends(get_db)):
    path = subtitles.video_path(get_library_root(db), req.rel_path)
    query = req.query.strip()
    if len(query) < 3:
        raise HTTPException(400, "请输入至少 3 个字符的作品名或文件名")
    aliases = [re.sub(r"\s*(?:S\d+)?E\d+(?:\.\d+)?\s*$", "", query, flags=re.I).strip()]
    offset, total = 0, None
    if req.bgm_id:
        catalog = db.query(models.AnimeCatalog).filter_by(bgm_id=req.bgm_id).first()
        if catalog:
            aliases.extend([catalog.title or "", catalog.title_original or ""])
        family = db.query(models.AnimeFamilyCache).filter_by(bgm_id=req.bgm_id).first()
        if family:
            aliases.append(family.name)
            offset, total = _episode_offset(db, family), family.eps
        cached_aliases = _aliases.get(req.bgm_id)
        if not cached_aliases or time.monotonic() - cached_aliases[0] > 6 * 3600:
            try:
                names = subject_aliases(await asyncio.wait_for(get_subject_detail(req.bgm_id), timeout=8))
                _aliases[req.bgm_id] = time.monotonic(), names
            except Exception:
                _aliases[req.bgm_id] = time.monotonic() - 6 * 3600 + 300, []
            if len(_aliases) > 256:
                _aliases.pop(next(iter(_aliases)))
        aliases.extend(_aliases.get(req.bgm_id, (0, []))[1])
    try:
        relative = req.rel_path.replace("\\", "/")
        record = db.query(models.RenamedFile).filter(models.RenamedFile.target_relative_path.in_(
            [relative, relative.replace("/", "\\")])).first()
        video_hint = path.stem
        if record:
            video_hint += " " + (record.torrent_title or "") + " " + (record.original_path or "")
        result = await subtitles.search(path, query, aliases, offset, total, video_hint)
        duration = await asyncio.to_thread(subtitles.video_duration, path)
        for item in result["items"]:
            item["duration_gap"] = abs(item["source_duration"] - duration) if duration and item["source_duration"] else None
        result["items"].sort(key=lambda item: (len(item["warnings"]), subtitles.language_rank(item["language"], req.preferred_language),
            0 if req.preferred_release.strip() and req.preferred_release.strip().casefold() in item["release"].casefold() else 1,
            item["duration_gap"] if item["duration_gap"] is not None else float("inf")))
        recommendation = subtitles.recommend(result["items"], duration, req.preferred_language, req.preferred_release.strip())
        return {**result, "video_duration": duration, "filename": path.name, "recommendation_id": recommendation}
    except OSError:
        raise HTTPException(503, "视频文件不可读取，请检查媒体库连接")


@router.post("/prepare")
async def prepare_subtitle(req: Selection, db: Session = Depends(get_db)):
    path = subtitles.video_path(get_library_root(db), req.rel_path)
    try:
        return await subtitles.prepare(path, req.candidate_id, req.language)
    except OSError:
        raise HTTPException(503, "视频文件不可读取，请检查媒体库连接")


@router.post("/plan")
async def plan_subtitles(req: SaveRequest, db: Session = Depends(get_db)):
    root = get_library_root(db)
    results, reserved = [], {}
    for selection in req.items:
        try:
            path = subtitles.video_path(root, selection.rel_path)
            preview = await subtitles.prepare(path, selection.candidate_id, selection.language)
            item = subtitles.get_candidate(path, selection.candidate_id)
            data = subtitles._contents[selection.candidate_id][0]
            plan = await asyncio.to_thread(subtitles.target_plan, path, data, item.public["format"], preview["language"], reserved)
            results.append({"rel_path": selection.rel_path, "candidate_id": selection.candidate_id,
                            **preview, **plan, "target_name": plan["filename"]})
        except HTTPException as exc:
            results.append({"rel_path": selection.rel_path, "candidate_id": selection.candidate_id, "error": exc.detail})
        except OSError:
            results.append({"rel_path": selection.rel_path, "candidate_id": selection.candidate_id, "error": "无法读取字幕或视频目录"})
    return {"results": results}


@router.post("/save")
async def save_subtitles(req: SaveRequest, db: Session = Depends(get_db)):
    root = get_library_root(db)
    results, folders, seen = [], set(), set()
    for selection in req.items:
        key = selection.rel_path, selection.candidate_id
        if key in seen:
            continue
        seen.add(key)
        try:
            path = subtitles.video_path(root, selection.rel_path)
            await subtitles.prepare(path, selection.candidate_id, selection.language)
            result = await asyncio.to_thread(subtitles.save, path, selection.candidate_id, selection.language)
            results.append({"rel_path": selection.rel_path, "candidate_id": selection.candidate_id, **result})
            if not result["already_exists"]:
                folders.add(path.relative_to(root.resolve()).parts[0])
        except HTTPException as exc:
            results.append({"rel_path": selection.rel_path, "candidate_id": selection.candidate_id, "error": exc.detail})
        except OSError:
            results.append({"rel_path": selection.rel_path, "candidate_id": selection.candidate_id, "error": "字幕保存失败，请检查目录权限或媒体库连接"})
    # 文件已成功保存，缓存刷新失败不把成功伪装成失败（避免用户重试又保存一份）。
    cache_warning = None
    if folders:
        try:
            for folder in folders:
                media = db.query(models.LocalMedia).filter_by(folder_name=folder).first()
                if media:
                    refresh_folder_size(media, root)
            db.commit()
        except Exception:
            db.rollback()
            cache_warning = "字幕已保存，文件夹大小暂未刷新，可稍后重新扫描"
    return {"results": results, "warning": cache_warning}
