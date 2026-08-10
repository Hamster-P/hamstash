"""下载源注册表。新增一个源 = 在这里 import 它的 adapter 并加进 _ADAPTERS。

id 必须与历史 SubscriptionRule.source 落库值一致(dmhy/nyaa/animegarden),不能改名,
否则历史订阅轮询会找不到源。
"""
from __future__ import annotations

from sources.animegarden import AnimeGardenAdapter
from sources.base import SourceAdapter, source_enabled
from sources.dmhy import DmhyAdapter
from sources.nyaa import NyaaAdapter

# 注册顺序 = 前端下拉展示顺序(dmhy 是历史默认源,排最前)
_ADAPTERS: dict[str, SourceAdapter] = {
    a.id: a
    for a in (DmhyAdapter(), AnimeGardenAdapter(), NyaaAdapter())
}


def get_source(source_id: str) -> SourceAdapter:
    """按 id 取 adapter。找不到抛 KeyError——注意:禁用某源只是在设置里把 enabled=false,
    adapter 本身仍留在注册表里(否则历史订阅轮询会崩),所以这里 KeyError 只会发生在
    真正未知的 source 字面量上。"""
    adapter = _ADAPTERS.get(source_id)
    if adapter is None:
        raise KeyError(f"未知数据源: {source_id}")
    return adapter


def has_source(source_id: str) -> bool:
    return source_id in _ADAPTERS


def all_sources() -> list[SourceAdapter]:
    return list(_ADAPTERS.values())


def enabled_sources() -> list[SourceAdapter]:
    """启用中的源(设置里 enabled != false)——给前端下拉/新建订阅用。"""
    return [a for a in _ADAPTERS.values() if source_enabled(a.id)]
