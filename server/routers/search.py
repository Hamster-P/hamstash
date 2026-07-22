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
    month: int | None = None
):
    # 默认将 limit 顶满到 100 条，并传入筛选条件
    result = await bangumi_client.search_anime(
        keyword=keyword, 
        limit=100, 
        year=year, 
        month=month
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

    cover_url = detail.get("images", {}).get("large", "")
    total_eps = detail.get("total_episodes") or detail.get("eps")

    db_anime = models.AnimeCatalog(
        bgm_id=detail.get("id"),
        title=detail.get("name_cn") or detail.get("name"),
        title_original=detail.get("name"),
        summary=detail.get("summary"),
        cover_url=cover_url,
        air_date=detail.get("date"),
        total_eps=total_eps,
    )
    db.add(db_anime)
    db.commit()
    db.refresh(db_anime)
    return db_anime
