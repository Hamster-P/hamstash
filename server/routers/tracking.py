"""对应前端 TrackingPage(追更):本季连载时刻表。"""
from fastapi import APIRouter

import bangumi_client

router = APIRouter(tags=["追更"])


@router.get("/bangumi/schedule")
async def get_bangumi_schedule():
    """直接从 Bangumi 获取连载日历,替代原有的 Mikan 爬虫。"""
    # 可以在这里加入类似之前的内存缓存逻辑提高响应速度
    calendar_data = await bangumi_client.get_calendar()

    enriched = []
    for day in calendar_data:
        # Bangumi 的 weekday id 是 1-7 (周一到周日),前端需要 0-6
        weekday_idx = day["weekday"]["id"] - 1

        for item in day.get("items", []):
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
                    "total_eps": info["total_eps"],
                    "score": score,
                }
            )

    return enriched
