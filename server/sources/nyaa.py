"""nyaa.si 源 adapter。

搜索:HTML 爬搜索页,限定 Anime 大类(c=1_0)。不是 RSS——RSS 固定只给最新 75 条、不翻页。
RSS:?page=rss,<link> 只给 .torrent 下载链接,但 item 里有 nyaa:infoHash,自己拼磁力链接
(不用再去下载 .torrent 文件本体,那一步在部分网络环境经常超时)。

注意:nyaa 上绝大多数资源是英文/罗马音标题,中文关键词大概率没有结果,是站点本身特性。
"""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlencode
from xml.etree import ElementTree

from bs4 import BeautifulSoup

from sources.base import (
    ResourceItem,
    SearchCriteria,
    SearchResult,
    SourceAdapter,
    build_effective_keyword,
    build_magnet_from_hash,
    configured_url,
    extra_query_terms,
    extract_fansub_name,
    http_get,
    parse_size_text,
    strip_query_quotes,
)

DEFAULT_BASE_URL = "https://nyaa.si"
ANIME_CATEGORY = "1_0"  # Anime 大类(含全部字幕/生肉子分类),不含漫画/音乐
_NYAA_NS = {"nyaa": "https://nyaa.si/xmlns/nyaa"}


class NyaaAdapter(SourceAdapter):
    id = "nyaa"
    label = "nyaa.si"
    supports_subject = False
    rate_limit_cooldown_ms = None
    rss_window_cap = 75
    URL_FIELDS = [
        ("base_url", "站点地址", DEFAULT_BASE_URL),
    ]

    def _base_url(self) -> str:
        return configured_url(self.id, "base_url", DEFAULT_BASE_URL)

    def _parse_page(self, html: str) -> list[ResourceItem]:
        """解析搜索页 <table class="torrent-list"><tbody><tr>:
        0=分类图标 1=标题(/view/{id} 详情链接) 2=下载(torrent+磁力) 3=大小 4=data-timestamp ..."""
        soup = BeautifulSoup(html, "html.parser")
        base_url = self._base_url()
        results: list[ResourceItem] = []
        for row in soup.select("table.torrent-list tbody tr"):
            title_link = row.select_one('a[href^="/view/"]:not([href*="#comments"])')
            if title_link is None:
                continue
            magnet_link = row.select_one('a[href^="magnet:"]')
            if magnet_link is None:
                continue

            title = title_link.get("title") or title_link.get_text(strip=True)
            detail_url = base_url + title_link["href"]

            tds = row.find_all("td")
            size_text = tds[3].get_text(strip=True) if len(tds) > 3 else None
            timestamp = tds[4].get("data-timestamp") if len(tds) > 4 else None
            created_at = None
            if timestamp:
                created_at = (
                    datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
                    .strftime("%a, %d %b %Y %H:%M:%S +0000")
                )

            results.append(
                ResourceItem(
                    source="nyaa",
                    provider="nyaa",
                    title=title,
                    fansub_name=extract_fansub_name(title),
                    magnet=magnet_link["href"],
                    size=parse_size_text(size_text),
                    created_at=created_at,
                    bgm_id=None,
                    detail_url=detail_url,
                )
            )
        return results

    async def search(self, criteria: SearchCriteria, bgm_id: int | None, page: int) -> SearchResult:
        extra_terms = extra_query_terms(criteria.quality, criteria.format)
        effective_keyword = build_effective_keyword(criteria.keyword or "", extra_terms, criteria.fansub_name)
        resp = await http_get(
            self._base_url(),
            params={"c": ANIME_CATEGORY, "f": 0, "q": effective_keyword, "p": page},
        )
        results = self._parse_page(resp.text)
        return SearchResult(results=results, has_more=len(results) > 0)

    def build_rss_urls(self, rule) -> list[str]:
        # nyaa 搜索把字面引号当必须原样出现的内容,查询前先去掉引号短语语法
        query = urlencode(
            {"page": "rss", "c": ANIME_CATEGORY, "f": 0, "q": strip_query_quotes(rule.keyword or "")}
        )
        return [f"{self._base_url()}/?{query}"]

    def parse_feed(self, xml_bytes: bytes) -> list[ResourceItem]:
        """nyaa 的 RSS <link> 只给 .torrent,但有 nyaa:infoHash,自己拼磁力链接。"""
        root = ElementTree.fromstring(xml_bytes)
        items: list[ResourceItem] = []
        for item in root.iter("item"):
            title_el = item.find("title")
            guid_el = item.find("guid")
            hash_el = item.find("nyaa:infoHash", _NYAA_NS)
            if title_el is None or guid_el is None or hash_el is None or not hash_el.text:
                continue
            title = (title_el.text or "").strip()
            info_hash = hash_el.text.strip()
            items.append(
                ResourceItem(
                    source="nyaa",
                    provider="nyaa",
                    title=title,
                    fansub_name=extract_fansub_name(title),
                    magnet=build_magnet_from_hash(info_hash, title),
                    guid=(guid_el.text or "").strip(),
                    info_hash=info_hash,
                )
            )
        return items
