"""对应前端 SearchPage(搜索):Bangumi关键词检索 + 一键加入库。"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

import bangumi_client
import models
from database import get_db
from services import bgm_series_cache

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


@router.get("/bangumi/related/{bgm_id}")
async def get_related_anime(bgm_id: int, db: Session = Depends(get_db)):
    """
    影视库详情页"补番"按钮用:找到这部番所在Bangumi系列的全部关联作品
    (续集/前传/主线故事/不同演绎/总集篇/全集/番外篇),批量拉取详情(含封面/评分)。

    家族成员id优先走services/bgm_series_cache.py::resolve_related_family_ids_cached——
    命中AnimeFamilyCache且核实过没有新成员时,不发resolve_root_subject_id/
    resolve_family_season_map这两个较重的网络调用,只有一轮轻量核实;详见该函数文档。

    resolve_family_season_map/缓存都只保留了name/date/platform/eps这几个裁剪过的
    字段用于算季度序号,没有封面图这些原始payload——这里对家族bgm_id列表单独拉一次
    完整详情,是一轮批量并发请求,不是这次要优化的瓶颈。

    返回结构刻意跟/bangumi/search的条目字段(id/name/name_cn/date/eps/images/
    rating)保持一致,前端复用同一个展示组件(BangumiResultsList)渲染。
    """
    family_ids = await bgm_series_cache.resolve_related_family_ids_cached(db, bgm_id)
    if not family_ids:
        family_ids = [bgm_id]

    details_map = await bangumi_client.get_subject_details_batch(family_ids)
    results = []
    for fid, detail in details_map.items():
        if not detail:
            continue
        results.append({
            "id": detail.get("id") or fid,
            "name": detail.get("name") or "",
            "name_cn": detail.get("name_cn") or "",
            "date": detail.get("date"),
            "eps": detail.get("total_episodes") or detail.get("eps"),
            "images": detail.get("images"),
            "rating": detail.get("rating"),
        })
    results.sort(key=lambda item: item["id"], reverse=True)  # 按bgm_id降序
    return {"data": results}


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
