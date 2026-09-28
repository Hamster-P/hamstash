"""对应前端 TrackingPage(追更):本季连载时刻表。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

import bangumi_client
import config_store
from database import get_db
from services.anime_origin_cache import resolve_origin_batch
from services.common import get_setting, upsert_setting

router = APIRouter(tags=["追更"])

TRACKING_LAYOUTS = {"vertical", "horizontal"}


class TrackingLayoutUpdate(BaseModel):
    mode: str


@router.get("/tracking/layout")
def get_tracking_layout(db: Session = Depends(get_db)):
    """追更页纵向/横向记忆。重新打开时恢复上次选择。"""
    return {"mode": get_setting(db, "tracking_layout", config_store.DEFAULTS["tracking_layout"])}


@router.put("/tracking/layout")
def set_tracking_layout(req: TrackingLayoutUpdate, db: Session = Depends(get_db)):
    if req.mode not in TRACKING_LAYOUTS:
        raise HTTPException(status_code=400, detail=f"未知排列: {req.mode}")
    upsert_setting(db, "tracking_layout", req.mode)
    config_store.update_ini_value("tracking_layout", req.mode)
    return {"mode": req.mode}


@router.get("/bangumi/schedule")
async def get_bangumi_schedule(db: Session = Depends(get_db)):
    """直接从 Bangumi 获取连载日历,替代原有的 Mikan 爬虫。"""
    # 可以在这里加入类似之前的内存缓存逻辑提高响应速度
    calendar_data = await bangumi_client.get_calendar()

    all_items = [
        (day["weekday"]["id"] - 1, item)  # Bangumi 的 weekday id 是 1-7(周一到周日),前端需要 0-6
        for day in calendar_data
        for item in day.get("items", [])
    ]
    origin_map = await resolve_origin_batch(db, [item["id"] for _, item in all_items])

    enriched = []
    for weekday_idx, item in all_items:
        # 只保留Bangumi官方meta_tags标了"日本"产地的条目,过滤掉国漫/其他产地番剧。
        if not origin_map.get(item["id"]):
            continue

        info = bangumi_client.normalize_bgm_subject(item)

        score = None
        if item.get("rating"):
            score = item["rating"].get("score")

        enriched.append(
            {
                "bgm_id": info["bgm_id"],
                "title": info["title"],
                "weekday": weekday_idx,
                "cover_url": info["cover_url"] or None,
                "total_eps": info["total_eps"] or None,
                "score": score,
            }
        )

    return enriched
