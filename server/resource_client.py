"""下载源检索入口(薄封装)。

具体各源的抓取/解析/RSS 逻辑已经收敛进 sources/ 包的 SourceAdapter 里(见 sources/registry.py),
这里只保留对外的 search_by_source 门面,负责:按 source 取 adapter → 调 search →
用与 RSS 轮询共用的唯一权威谓词 matches_criteria 做服务端过滤 → 归一成前端要的 dict。

历史遗留的 build_*_rss_url / 各源 _parse_* / URL 常量都已迁走。RSS 轮询侧改用
sources.registry(见 services/rss_poller.py)。
"""
from __future__ import annotations

from sources.base import SearchCriteria, matches_criteria
from sources.registry import get_source, has_source


async def search_by_source(
    keyword: str,
    source: str,
    bgm_id: int | None = None,
    page: int = 1,
    fansub_name: str | None = None,
    quality: str | None = None,
    subtitle: str | None = None,
    format: str | None = None,
    release_type: str | None = None,
) -> dict:
    """按用户明确选择的数据源搜索,不做静默降级/自动切换。

    服务端权威过滤:拿到站点结果后,用 matches_criteria(与 RSS 轮询命中判断是同一个函数)
    过滤再返回——这样"搜索页看到的"和"订阅实际下载的"在同一数据窗口内逐项一致。
    字幕组/画质/格式在各 adapter 里已尽量转成站点服务端查询条件收窄范围,这里再统一
    用本地谓词精确兜底(尤其字幕语言,单个 CJK 字符发给站点搜索查不到,只能本地过滤)。

    每次只请求这一个源的这一页(page 从 1 开始),不预取多页——dmhy 有防刷机制,
    翻页交给前端"加载更多"按需触发。返回 {"results", "has_more", "rss_window_cap"}。
    """
    if not has_source(source):
        raise ValueError(f"未知数据源: {source}")
    adapter = get_source(source)
    criteria = SearchCriteria(
        keyword=keyword,
        fansub_name=fansub_name,
        quality=quality,
        subtitle=subtitle,
        format=format,
        release_type=release_type,
    )
    result = await adapter.search(criteria, bgm_id, page)
    filtered = [it for it in result.results if matches_criteria(it, criteria)]
    return {
        "results": [it.to_api_dict() for it in filtered],
        "has_more": result.has_more,
        "rss_window_cap": adapter.rss_window_cap,
    }


async def find_bgm_id_by_title(title: str) -> int | None:
    """在 AnimeGarden 最近资源里找匹配标题的条目,顺手拿现成 subjectId(省一次 Mikan 爬取)。
    找不到返回 None。委托给 AnimeGarden adapter。"""
    from sources.animegarden import AnimeGardenAdapter
    adapter = get_source("animegarden")
    if isinstance(adapter, AnimeGardenAdapter):
        return await adapter.find_bgm_id_by_title(title)
    return None
