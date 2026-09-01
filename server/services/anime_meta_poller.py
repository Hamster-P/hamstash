"""详情页元数据(TMDB背景图/LOGO/分级等)后台重试轮询,模式照抄organize_loop/
rss_poll_loop(services/organize.py::organize_loop):可配置间隔、每轮重读一次
配置、两层try/except隔离异常不让循环整体挂掉。

只处理LocalMedia里真实存在的库条目,不做全量Bangumi扫描——这是性能边界,
库有几百部也就几百个bgm_id,不是Bangumi全站两万多条目。
"""
import asyncio

import config_store
from database import SessionLocal
from models import AnimeMetaCache, LocalMedia
from services import anime_meta_resolver
from services.common import get_setting

# 单轮最多处理这么多条,避免库很大时一次性把TMDB/arm-server打爆
# (对已解析成功的条目不会重复占用这个配额,只有pending/unresolved_retry才排队)。
_BATCH_SIZE = 20

_PENDING_STATUSES = ("pending", "unresolved_retry")


async def _poll_once() -> None:
    db = SessionLocal()
    try:
        bgm_ids = {
            row[0]
            for row in db.query(LocalMedia.bgm_id).filter(LocalMedia.bgm_id.isnot(None)).distinct()
        }
        if not bgm_ids:
            return

        existing_status = {
            row.bgm_id: row.status
            for row in db.query(AnimeMetaCache).filter(AnimeMetaCache.bgm_id.in_(bgm_ids)).all()
        }
        todo = [bid for bid in bgm_ids if existing_status.get(bid, "pending") in _PENDING_STATUSES]
        todo = todo[:_BATCH_SIZE]

        for bgm_id in todo:
            try:
                await anime_meta_resolver.resolve_one(db, bgm_id)
                db.commit()
            except Exception as e:
                db.rollback()
                print(f"[ANIME_META] 解析bgm_id={bgm_id}失败: {e}")
    finally:
        db.close()


async def anime_meta_poll_loop() -> None:
    while True:
        try:
            db = SessionLocal()
            try:
                interval = int(
                    get_setting(
                        db,
                        "metadata_poll_interval_seconds",
                        config_store.DEFAULTS["metadata_poll_interval_seconds"],
                    )
                )
            finally:
                db.close()
        except Exception:
            interval = int(config_store.DEFAULTS["metadata_poll_interval_seconds"])

        try:
            await _poll_once()
        except Exception as e:
            print(f"[ANIME_META] 轮询循环出错: {e}")

        await asyncio.sleep(interval)
