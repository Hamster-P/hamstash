"""
bgm_id -> 是否日本产地,按AnimeOriginCache表持久化缓存,减少重复调用Bangumi详情接口。

产地信息基本终身不变,查过一次的bgm_id直接读数据库,只有全新出现的bgm_id才需要
真的去调Bangumi的详情接口——追更页当季100+部番全部冷查询实测要15秒左右,
热缓存命中后这个耗时基本消失。
"""
from sqlalchemy.orm import Session

import bangumi_client
from models import AnimeOriginCache


def _is_japanese(meta_tags: list[str]) -> bool:
    return "日本" in meta_tags


async def resolve_origin_batch(db: Session, bgm_ids: list[int]) -> dict[int, bool]:
    """批量解析一组bgm_id是否日本产地,优先读缓存表,缺的才去查Bangumi并写回缓存。"""
    unique_ids = list({bid for bid in bgm_ids if bid})
    if not unique_ids:
        return {}

    cached_rows = (
        db.query(AnimeOriginCache)
        .filter(AnimeOriginCache.bgm_id.in_(unique_ids))
        .all()
    )
    origin_map: dict[int, bool] = {row.bgm_id: row.is_japanese for row in cached_rows}

    missing_ids = [bid for bid in unique_ids if bid not in origin_map]
    if not missing_ids:
        return origin_map

    details = await bangumi_client.get_subject_details_batch(missing_ids)
    for bid in missing_ids:
        detail = details.get(bid)
        if detail is None:
            # 查询失败(网络问题/限流),按"不确定"处理——这一轮先不展示,
            # 但不写进缓存表,避免把一次网络抖动永久焊死成"非日本产地"的假数据。
            origin_map[bid] = False
            continue
        meta_tags = detail.get("meta_tags") or []
        is_japanese = _is_japanese(meta_tags)
        db.add(
            AnimeOriginCache(
                bgm_id=bid,
                is_japanese=is_japanese,
                meta_tags=",".join(meta_tags),
            )
        )
        origin_map[bid] = is_japanese
    db.commit()

    return origin_map
