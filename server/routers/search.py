"""对应前端 SearchPage(搜索):Bangumi关键词检索 + 一键加入库。"""
import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

import bangumi_client
import models
from database import get_db
from services import bgm_series_cache

router = APIRouter(tags=["搜索"])

# "补番"一览的整份响应缓存有效期。命中期内直接返回 related_anime_cache 里存的结果,
# 完全不碰 Bangumi;过期才重新联网核实新成员 + 刷新详情。家族一年也就新增 1~2 部,
# 7 天延迟可接受(用户确认)。
RELATED_CACHE_TTL = timedelta(days=7)


def _utcnow_naive() -> datetime:
    """naive UTC——与 SQLite func.now()(CURRENT_TIMESTAMP,UTC)存进 checked_at 的口径一致。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


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

    整份结果按家族根缓存进 related_anime_cache,RELATED_CACHE_TTL 内直接返回、零联网
    (见 models.RelatedAnimeCache)。
    """
    # 家族根:纯读库。补番按钮只在已下载/匹配的番出现,下载流程已预热 anime_family_cache,
    # 命中率高;查不到就拿自己当根兜底。
    fam = (
        db.query(models.AnimeFamilyCache)
        .filter(models.AnimeFamilyCache.bgm_id == bgm_id)
        .first()
    )
    root = fam.source_bgm_id if fam else bgm_id

    cached = (
        db.query(models.RelatedAnimeCache)
        .filter(models.RelatedAnimeCache.root_bgm_id == root)
        .first()
    )
    if cached and cached.checked_at and _utcnow_naive() - cached.checked_at < RELATED_CACHE_TTL:
        return {"data": json.loads(cached.payload)}

    # 过期/未缓存:走原逻辑(resolve_related_family_ids_cached 内含 has_new_family_members
    # 核实 + 必要时全量重算),再批量拉详情。
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

    # 落库前重新确认家族根:首次解析可能刚把 bgm_id 归进一个此前不存在的家族,
    # 此时应按真正的 source_bgm_id 存,避免下次带真根来查时又落一次空。
    fam = (
        db.query(models.AnimeFamilyCache)
        .filter(models.AnimeFamilyCache.bgm_id == bgm_id)
        .first()
    )
    root = fam.source_bgm_id if fam else root
    # 空结果也缓存,避免家族树反复算不出时每次点补番都白跑一遍网络;7 天后自然重试。
    if results or not cached:
        _upsert_related_cache(db, root, results)
    return {"data": results}


def _upsert_related_cache(db: Session, root_bgm_id: int, results: list[dict]) -> None:
    payload = json.dumps(results, ensure_ascii=False)
    row = (
        db.query(models.RelatedAnimeCache)
        .filter(models.RelatedAnimeCache.root_bgm_id == root_bgm_id)
        .first()
    )
    if row:
        row.payload = payload
        row.checked_at = _utcnow_naive()
    else:
        db.add(models.RelatedAnimeCache(
            root_bgm_id=root_bgm_id, payload=payload, checked_at=_utcnow_naive(),
        ))
    db.commit()


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
