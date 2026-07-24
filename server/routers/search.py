"""对应前端 SearchPage(搜索):Bangumi关键词检索 + 一键加入库。"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

import bangumi_client
import models
from database import get_db

router = APIRouter(tags=["搜索"])


@router.get("/bangumi/search")
async def search_bangumi(
    keyword: str = "",
    year: int | None = None,
    month: int | None = None,
    offset: int = 0,
):
    # 默认将 limit 顶满到 100 条，并传入筛选条件
    result = await bangumi_client.search_anime(
        keyword=keyword,
        limit=100,
        year=year,
        month=month,
        offset=offset,
    )
    return result


@router.post("/anime/import")
async def import_anime_from_bangumi(bgm_id: int, db: Session = Depends(get_db)):
    existing = (
        db.query(models.AnimeCatalog).filter(models.AnimeCatalog.bgm_id == bgm_id).first()
    )
    if existing:
        return existing

    detail = await bangumi_client.get_subject_detail(bgm_id)
    info = bangumi_client.normalize_bgm_subject(detail)

    db_anime = models.AnimeCatalog(
        bgm_id=info["bgm_id"],
        title=info["title"],
        title_original=info["title_original"],
        summary=info["summary"],
        cover_url=info["cover_url"],
        air_date=info["air_date"],
        total_eps=info["total_eps"],
        total_episodes=info["total_eps"],
    )
    db.add(db_anime)
    db.commit()
    db.refresh(db_anime)
    return db_anime
