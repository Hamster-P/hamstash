"""对应前端 TrackingPage(追更):本季连载时刻表。"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

import bangumi_client
from database import get_db
from services.anime_origin_cache import resolve_origin_batch

router = APIRouter(tags=["追更"])


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
