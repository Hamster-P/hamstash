"""AnimeGarden(api.animes.garden)源 adapter —— 主力源。

聚合多个上游站点的二道源,JSON API,数据结构好(自带字幕组名和 Bangumi 编号),
支持按 bgm_id(subject)精确查 + 结构化 fansub 参数 + 真分页。

一致性要点:AnimeGarden 的搜索 JSON /resources 与 /feed.xml 是同一后端、同一套过滤参数,
只是输出格式不同。所以本 adapter 的订阅轮询直接覆写 poll() 走 search()(JSON 取数),
不走 feed.xml —— "搜索页看到的"和"订阅抓到的"完全同源,一致性最强。
"""
from __future__ import annotations

import asyncio
from urllib.parse import urlencode
from xml.etree import ElementTree

from sources.base import (
    ResourceItem,
    SearchCriteria,
    SearchResult,
    SourceAdapter,
    animegarden_size_to_bytes,
    build_effective_keyword,
    configured_url,
    extra_query_terms,
    extract_fansub_name,
    http_get,
    strip_query_quotes,
)

DEFAULT_API_URL = "https://api.animes.garden/resources"
DEFAULT_FEED_URL = "https://api.animes.garden/feed.xml"


class AnimeGardenAdapter(SourceAdapter):
    id = "animegarden"
    label = "AnimeGarden"
    supports_subject = True
    rate_limit_cooldown_ms = None
    rss_window_cap = 100
    URL_FIELDS = [
        ("api_url", "API 地址", DEFAULT_API_URL),
        ("feed_url", "Feed 地址", DEFAULT_FEED_URL),
    ]

    def _api_url(self) -> str:
        return configured_url(self.id, "api_url", DEFAULT_API_URL)

    def _feed_url(self) -> str:
        return configured_url(self.id, "feed_url", DEFAULT_FEED_URL)

    def _shape(self, resources: list[dict]) -> list[ResourceItem]:
        results: list[ResourceItem] = []
        for item in resources:
            fansub = item.get("fansub") or {}
            # guid 复用 AnimeGarden 详情页地址(provider/providerId),与旧版 feed.xml 里
            # <guid> 完全同一形态(https://animes.garden/detail/{provider}/{providerId})——
            # 这样从旧版(走 feed.xml)升级到新版(走 search 取数)时,RssMatchedItem 的判重
            # 仍能命中,已下载过的条目不会因为取数方式变化被重新推送下载。providerId 缺失时
            # 退回 magnet 兜底(见 poll)。
            provider = item.get("provider")
            provider_id = item.get("providerId")
            guid = (
                f"https://animes.garden/detail/{provider}/{provider_id}"
                if provider and provider_id is not None
                else None
            )
            results.append(
                ResourceItem(
                    source="animegarden",
                    provider=provider,
                    title=item.get("title", ""),
                    fansub_name=fansub.get("name", "未知字幕组"),
                    magnet=item.get("magnet"),
                    size=animegarden_size_to_bytes(item.get("size")),
                    created_at=item.get("createdAt"),
                    bgm_id=item.get("subjectId"),
                    # 聚合源,同一条资源背后可能是任意上游站点,没有自己的规范详情页
                    detail_url=None,
                    guid=guid,
                )
            )
        return results

    async def _fetch_page(self, extra_params: dict, page: int, page_size: int = 100) -> tuple[list[dict], bool]:
        resp = await http_get(self._api_url(), params={**extra_params, "page": page, "pageSize": page_size})
        data = resp.json()
        resources = data.get("resources", [])
        complete = bool((data.get("pagination") or {}).get("complete", True))
        return resources, complete

    async def search(self, criteria: SearchCriteria, bgm_id: int | None, page: int) -> SearchResult:
        # fansub 是 AnimeGarden 原生结构化参数,单独传比拼进关键词更精确;
        # 画质/格式没有独立参数,拼进 search;字幕语言完全交给本地 matches_criteria。
        extra_terms = extra_query_terms(criteria.quality, criteria.format)
        effective_keyword = build_effective_keyword(criteria.keyword or "", extra_terms)

        subject_params: dict = {"subject": bgm_id} if bgm_id else {}
        if effective_keyword:
            subject_params["search"] = effective_keyword
        if criteria.fansub_name and criteria.fansub_name != "全部":
            subject_params["fansub"] = criteria.fansub_name

        if bgm_id:
            # 并非所有资源都回填了 subjectId(某些代理商/正版引进批次没有),只按 subject 查会
            # 静默漏掉这些——额外并发一次纯文本查询兜底,两边按 magnet 去重合并,subject 优先。
            text_params: dict = {}
            if effective_keyword:
                text_params["search"] = effective_keyword
            if criteria.fansub_name and criteria.fansub_name != "全部":
                text_params["fansub"] = criteria.fansub_name
            (subject_resources, subject_complete), (text_resources, text_complete) = await asyncio.gather(
                self._fetch_page(subject_params, page),
                self._fetch_page(text_params, page),
            )
            subject_items = self._shape(subject_resources)
            text_items = self._shape(text_resources)
            seen = {it.magnet for it in subject_items}
            results = subject_items + [it for it in text_items if it.magnet not in seen]
            has_more = (not subject_complete) or (not text_complete)
        else:
            resources, complete = await self._fetch_page(subject_params, page)
            results = self._shape(resources)
            has_more = not complete
        return SearchResult(results=results, has_more=has_more)

    async def poll(self, rule) -> list[ResourceItem]:
        """取数同源:直接用 search() 抓候选(而非 feed.xml),保证订阅抓到的与搜索看到的一致。
        search 内部已做 subject+文本 union 去重;这里给每条补上 guid(用 magnet 当稳定去重键,
        供 RssMatchedItem 判重)。"""
        criteria = SearchCriteria(
            keyword=rule.keyword,
            fansub_name=rule.fansub_name,
            quality=rule.quality,
            subtitle=rule.subtitle,
            format=rule.format,
            release_type=rule.release_type,
        )
        result = await self.search(criteria, rule.bgm_id, page=1)
        for it in result.results:
            if not it.guid:
                it.guid = it.magnet
        return result.results

    # 以下 build_rss_urls / parse_feed 仅作为 feed.xml 后备保留(poll 已改走 search,不再调用),
    # 便于将来需要时回退,也让 adapter 自洽。
    def build_rss_urls(self, rule) -> list[str]:
        feed = self._feed_url()
        if rule.bgm_id:
            params = {"subject": rule.bgm_id}
            if rule.fansub_name:
                params["fansub"] = rule.fansub_name
            urls = [f"{feed}?{urlencode(params)}"]
            if rule.keyword:
                kw_params = {"search": strip_query_quotes(rule.keyword)}
                if rule.fansub_name:
                    kw_params["fansub"] = rule.fansub_name
                urls.append(f"{feed}?{urlencode(kw_params)}")
            return urls
        params = {"search": strip_query_quotes(rule.keyword or "")}
        if rule.fansub_name:
            params["fansub"] = rule.fansub_name
        return [f"{feed}?{urlencode(params)}"]

    def parse_feed(self, xml_bytes: bytes) -> list[ResourceItem]:
        root = ElementTree.fromstring(xml_bytes)
        items: list[ResourceItem] = []
        for item in root.iter("item"):
            title_el = item.find("title")
            guid_el = item.find("guid")
            enclosure_el = item.find("enclosure")
            if title_el is None or guid_el is None or enclosure_el is None:
                continue
            magnet = enclosure_el.get("url")
            if not magnet or not magnet.startswith("magnet:"):
                continue
            title = (title_el.text or "").strip()
            items.append(
                ResourceItem(
                    source="animegarden",
                    provider="animegarden",
                    title=title,
                    fansub_name=extract_fansub_name(title),
                    magnet=magnet,
                    guid=(guid_el.text or "").strip(),
                    info_hash=None,
                )
            )
        return items

    async def find_bgm_id_by_title(self, title: str) -> int | None:
        """在最近资源里找匹配标题的条目,顺手拿现成 subjectId(省一次 Mikan 详情页爬取)。
        找不到返回 None,调用方走原来的 Mikan 兜底。"""
        try:
            resources, _ = await self._fetch_page({"search": strip_query_quotes(title)}, page=1, page_size=20)
        except Exception:
            return None
        for item in self._shape(resources):
            if item.bgm_id:
                return item.bgm_id
        return None
