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
    """应用启动时调用,保证数据库结构与 models.py 一致。"""
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
