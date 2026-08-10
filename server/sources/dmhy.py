"""dmhy(動漫花園)源 adapter。

搜索:HTML 爬网页搜索页 topics/list(不是 RSS——RSS 固定上限 500 条、不支持翻页)。
RSS:topics/rss/rss.xml?keyword=,<enclosure> 直接给完整磁力链接。

只请求调用方要的这一页,不预取多页——dmhy 有防刷机制,曾经一次性顺序拉多页被判定成
"5 秒内多次搜索"把 IP 的搜索功能封了一段时间。翻页交给前端"加载更多"按需触发,
前端两次搜索之间还有 rate_limit_cooldown_ms 的冷却。
"""
from __future__ import annotations

from xml.etree import ElementTree

from bs4 import BeautifulSoup

from sources.base import (
    ResourceItem,
    SearchCriteria,
    SearchResult,
    SourceAdapter,
    build_effective_keyword,
    configured_url,
    extra_query_terms,
    extract_fansub_name,
    http_get,
    parse_size_text,
    strip_query_quotes,
)

DEFAULT_SEARCH_URL = "https://dmhy.org/topics/list"
DEFAULT_RSS_URL = "https://dmhy.org/topics/rss/rss.xml"


class DmhyAdapter(SourceAdapter):
    id = "dmhy"
    label = "動漫花園 (dmhy)"
    supports_subject = False
    rate_limit_cooldown_ms = 5000  # 防刷:前端两次搜索之间强制冷却 5 秒
    rss_window_cap = 500
    URL_FIELDS = [
        ("search_url", "搜索页地址", DEFAULT_SEARCH_URL),
        ("rss_url", "RSS 地址", DEFAULT_RSS_URL),
    ]

    def _search_url(self) -> str:
        return configured_url(self.id, "search_url", DEFAULT_SEARCH_URL)

    def _rss_url(self) -> str:
        return configured_url(self.id, "rss_url", DEFAULT_RSS_URL)

    def _parse_page(self, html: str) -> list[ResourceItem]:
        """解析 topics/list 一页结果。表格 <table id="topic_list"><tbody><tr>,每行 9 个 <td>:
        0=日期 1=分类 2=标题(内含字幕组 team_id 链接+详情页链接) 3=下载图标 4=大小 ... 8=发布人。"""
        soup = BeautifulSoup(html, "html.parser")
        search_url = self._search_url()
        # detail_url 依赖 base(去掉 /topics/list 那段);换镜像时跟着配置走,不引用模块常量
        detail_base = search_url.rsplit("/topics/list", 1)[0]
        results: list[ResourceItem] = []
        for row in soup.select("table#topic_list tbody tr"):
            title_link = row.select_one('td.title a[href^="/topics/view/"]')
            if title_link is None:
                continue
            magnet_link = row.select_one('a[href^="magnet:"]')
            if magnet_link is None:
                continue

            title = title_link.get_text(" ", strip=True)
            detail_url = detail_base + title_link["href"]

            team_link = row.select_one('a[href^="/topics/list/team_id/"]')
            fansub_name = team_link.get_text(strip=True) if team_link else extract_fansub_name(title)

            tds = row.find_all("td")
            size_text = tds[4].get_text(strip=True) if len(tds) > 4 else None
            # 日期格里有个 display:none 的隐藏 span 重复了文字,只取第一个直接文本节点
            created_at = tds[0].find(string=True, recursive=False).strip() if tds else None

            results.append(
                ResourceItem(
                    source="dmhy",
                    provider="dmhy",
                    title=title,
                    fansub_name=fansub_name or "未知字幕组",
                    magnet=magnet_link["href"],
                    size=parse_size_text(size_text),
                    created_at=created_at,
                    bgm_id=None,
                    detail_url=detail_url,
                )
            )
        return results

    async def search(self, criteria: SearchCriteria, bgm_id: int | None, page: int) -> SearchResult:
        # dmhy 没有结构化字幕组/画质参数,字幕组和画质/格式都拼进 keyword 全文搜
        extra_terms = extra_query_terms(criteria.quality, criteria.format)
        effective_keyword = build_effective_keyword(criteria.keyword or "", extra_terms, criteria.fansub_name)
        resp = await http_get(
            self._search_url(),
            params={"keyword": effective_keyword, "sort_id": 0, "team_id": 0, "order": "date-desc", "page": page},
        )
        results = self._parse_page(resp.text)
        # dmhy 没有总数概念,只能拿"这一页是不是空的"乐观判断
        return SearchResult(results=results, has_more=len(results) > 0)

    def build_rss_urls(self, rule) -> list[str]:
        from urllib.parse import urlencode
        query = urlencode({"keyword": strip_query_quotes(rule.keyword or "")})
        return [f"{self._rss_url()}?{query}"]

    def parse_feed(self, xml_bytes: bytes) -> list[ResourceItem]:
        """dmhy 的 RSS:<enclosure url="magnet:?..."> 直接给完整磁力链接。"""
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
                    source="dmhy",
                    provider="dmhy",
                    title=title,
                    fansub_name=extract_fansub_name(title),
                    magnet=magnet,
                    guid=(guid_el.text or "").strip(),
                    info_hash=None,
                )
            )
        return items
