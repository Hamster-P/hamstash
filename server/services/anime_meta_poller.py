"""详情页元数据(TMDB背景图/LOGO/分级等)后台重试轮询,模式照抄organize_loop/
rss_poll_loop(services/organize.py::organize_loop):可配置间隔、每轮重读一次
配置、两层try/except隔离异常不让循环整体挂掉。

只处理LocalMedia里真实存在的库条目,不做全量Bangumi扫描——这是性能边界,
库有几百部也就几百个bgm_id,不是Bangumi全站两万多条目。
"""
import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_

import config_store
from database import SessionLocal
from models import AnimeMetaCache, LocalMedia
from services import anime_meta_resolver
from services.common import get_setting

# 单轮最多处理这么多条,避免库很大时一次性把TMDB/arm-server打爆
# (对已解析成功的条目不会重复占用这个配额,只有pending/unresolved_retry才排队)。
_BATCH_SIZE = 20

_PENDING_STATUSES = ("pending", "unresolved_retry")

# 解析逻辑升级后,已resolved但resolver_version落后的行按新逻辑重取:每轮取这么多条,
# 比pending批次更保守(这些行前端已经有旧图能显示,不急)。
_REFRESH_BATCH_SIZE = 10
# 单条刷新失败后至少隔这么久再试一次(不新增计数列,用last_attempt_at节流)。
_REFRESH_RETRY_BACKOFF = timedelta(hours=24)


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

        await _refresh_stale_resolved(db, bgm_ids)
    finally:
        db.close()


async def _refresh_stale_resolved(db, bgm_ids: set[int]) -> None:
    """已resolved但resolver_version落后于当前解析逻辑版本的行,按新逻辑重取一次。

    重取期间前端照旧显示旧图(行仍是resolved、URL不变);成功后整行覆盖成新图,
    失败则保持原样、隔_REFRESH_RETRY_BACKOFF再试。只处理库里真实存在的bgm_id。
    """
    cutoff = datetime.now(timezone.utc) - _REFRESH_RETRY_BACKOFF
    stale = (
        db.query(AnimeMetaCache.bgm_id)
        .filter(
            AnimeMetaCache.bgm_id.in_(bgm_ids),
            AnimeMetaCache.status == "resolved",
            or_(
                AnimeMetaCache.resolver_version.is_(None),
                AnimeMetaCache.resolver_version < anime_meta_resolver.META_RESOLVER_VERSION,
            ),
            or_(
                AnimeMetaCache.last_attempt_at.is_(None),
                AnimeMetaCache.last_attempt_at < cutoff,
            ),
        )
        .limit(_REFRESH_BATCH_SIZE)
        .all()
    )

    for (bgm_id,) in stale:
        try:
            await anime_meta_resolver.resolve_one(db, bgm_id, is_refresh=True)
            db.commit()
        except Exception as e:
            db.rollback()
            print(f"[ANIME_META] 刷新bgm_id={bgm_id}失败: {e}")


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
