"""
SQLite 启动时自动升级 schema:
- 创建缺失的表
- 给已有表补齐缺失的列

不会删除列、不会改列类型。遇到这类需求请手写迁移脚本。
"""
from __future__ import annotations

import logging

from sqlalchemy import Boolean, DateTime, Float, Integer, Text, inspect, text
from sqlalchemy.sql import elements, sqltypes

from database import Base, engine

import models  # noqa: F401 — 确保所有 Model 都注册到 Base.metadata

logger = logging.getLogger(__name__)


def upgrade_db() -> None:
    """应用启动时调用,保证数据库结构与 models.py 一致。

    末尾还会做一次"缓存内容跟不跟得上当前这个build"的检查:季度解析算法改过之后,
    已经缓存的家族不会自己重算,新算法对老用户完全不生效
    (详见 services/bgm_series_cache.SEASON_ALGO_VERSION)。schema 和缓存内容
    都属于"让数据库跟上当前build"这件事,放在一起,也一起获得 main.py 那边的
    try/except 和 library_root_health_loop 的重试。
    """
    Base.metadata.create_all(bind=engine)

    inspector = inspect(engine)
    for table_name, table in Base.metadata.tables.items():
        if not inspector.has_table(table_name):
            continue

        existing = {col["name"] for col in inspector.get_columns(table_name)}
        for column in table.columns:
            if column.name in existing:
                continue

            sql, backfill = _build_add_column_sql(table_name, column)
            logger.info("[DB] %s", sql)
            with engine.begin() as conn:
                conn.execute(text(sql))
                if backfill:
                    conn.execute(
                        text(
                            f'UPDATE "{table_name}" SET "{column.name}" = datetime(\'now\') '
                            f'WHERE "{column.name}" IS NULL'
                        )
                    )

    _refresh_stale_caches()


def _refresh_stale_caches() -> None:
    """建表补列之后,再让内容层面也跟上当前build:季度解析算法版本变了就清空
    家族缓存(只清纯缓存,不碰用户数据,详见reset_cache_if_algo_changed)。

    延迟import:db_migrate建表这一步必须先跑完,services那边import的模块链更长,
    放模块顶会让"建表"依赖上一堆跟建表无关的东西。
    """
    from database import SessionLocal
    from services.bgm_series_cache import SEASON_ALGO_CHANGED_KEY, reset_cache_if_algo_changed
    from services.common import upsert_setting
    from services.rss_migration import reset_unknown_fansub_rules

    db = SessionLocal()
    try:
        if reset_cache_if_algo_changed(db):
            _mark_family_cache_needs_rewarm()
            # 季度编号规则变了,**已经落地的文件不会自己跟着重排**——实测案例:
            # 魔法少女奈叶 EXCEEDS 的 E07/E08 因为跨版本下载而分处 Season 04/05。
            # 缓存重算只让"以后的下载"正确,存量得靠用户跑一次修复媒体库,
            # 所以这里立个标记,让媒体库页提示他。
            upsert_setting(db, SEASON_ALGO_CHANGED_KEY, "1")
        # 一次性数据迁移:AnimeGarden 适配器改用 publisher 兜底真实字幕组名之后,
        # 旧的"未知字幕组"订阅会静默失效,必须放宽成"不限"。幂等,详见该函数。
        reset_unknown_fansub_rules(db)
    finally:
        db.close()


# 家族缓存刚被清空过的标记,由main.py的lifespan读一次:清空之后媒体库详情页拿不到
# 作品名目录的桶名(会暂时折叠进Specials/Others),起个后台任务把库里已绑定的番
# 重新预热一遍,用户不用为此手动跑一次"修复媒体库"。
_family_cache_needs_rewarm = False


def _mark_family_cache_needs_rewarm() -> None:
    global _family_cache_needs_rewarm
    _family_cache_needs_rewarm = True


def consume_family_cache_rewarm_flag() -> bool:
    """读取并清掉标记(只会被消费一次)。"""
    global _family_cache_needs_rewarm
    flag = _family_cache_needs_rewarm
    _family_cache_needs_rewarm = False
    return flag


def _build_add_column_sql(table_name: str, column) -> tuple[str, bool]:
    col_type = column.type.compile(dialect=engine.dialect)
    parts = [
        f'ALTER TABLE "{table_name}" ADD COLUMN "{column.name}"',
        col_type,
    ]

    backfill = _needs_datetime_backfill(column)
    default_sql = None if backfill else _default_sql(column)

    if default_sql is not None:
        parts.append(f"DEFAULT {default_sql}")
        if not column.nullable:
            parts.append("NOT NULL")
    elif not column.nullable and not backfill:
        fallback = _fallback_default(column)
        if fallback is not None:
            parts.append(f"DEFAULT {fallback}")
            parts.append("NOT NULL")

    return " ".join(parts), backfill


def _needs_datetime_backfill(column) -> bool:
    """SQLite ADD COLUMN 不支持 CURRENT_TIMESTAMP 这类非常量默认值,需先加列再回填。"""
    if not isinstance(column.type, DateTime):
        return False
    if column.server_default is None:
        return False

    arg = column.server_default.arg
    if isinstance(arg, elements.TextClause):
        return arg.text.strip().lower() in ("now()", "current_timestamp")
    if hasattr(arg, "name") and str(getattr(arg, "name", "")).lower() == "now":
        return True
    return False


def _default_sql(column) -> str | None:
    if column.server_default is not None:
        arg = column.server_default.arg
        if isinstance(arg, elements.TextClause):
            return arg.text.strip()
        if arg is not None and not hasattr(arg, "name"):
            return _literal_sql(arg)

    default = column.default
    if default is not None and hasattr(default, "arg"):
        arg = default.arg
        if not callable(arg):
            return _literal_sql(arg)

    return None


def _fallback_default(column) -> str | None:
    col_type = column.type
    if isinstance(col_type, Boolean):
        return "0"
    if isinstance(col_type, (Integer, Float)):
        return "0"
    if isinstance(col_type, (sqltypes.String, Text)):
        return "''"
    return None


def _literal_sql(value) -> str:
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, (int, float)):
        return str(value)
    if value is None:
        return "NULL"
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"
