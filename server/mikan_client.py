import asyncio
import re
import time

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://mikanani.me"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

WEEKDAY_MAP = {
    "星期一": 0,
    "星期二": 1,
    "星期三": 2,
    "星期四": 3,
    "星期五": 4,
    "星期六": 5,
    "星期日": 6,
}

# 简单的内存缓存,避免每次打开追更页都重新爬一遍+查一遍详情。
_schedule_cache = {"data": None, "expires_at": 0}
CACHE_TTL_SECONDS = 6 * 60 * 60  # 6小时


async def fetch_schedule():
    """抓取Mikan首页,返回按星期分组的连载番剧列表(mikan_id + 标题 + 星期)。"""
    async with httpx.AsyncClient(
        headers=HEADERS, timeout=15.0, follow_redirects=True
    ) as client:
        resp = await client.get(f"{BASE_URL}/")
        resp.raise_for_status()
        html = resp.text

    soup = BeautifulSoup(html, "html.parser")

    results = []
    seen_ids = set()

    for link in soup.select('a[href*="/Home/Bangumi/"]'):
        href = link.get("href", "")
        match = re.search(r"/Home/Bangumi/(\d+)", href)
        if not match:
            continue
        mikan_id = int(match.group(1))
        title = link.get("title") or link.get_text(strip=True)
        if not title or mikan_id in seen_ids:
            continue

        # 往前查找最近的"星期几"文字节点,判断这个番剧属于哪一天分组。
        # 剧场版/OVA分组不在星期分类下,找不到就跳过。
        weekday_label = None
        prev_node = link.find_previous(
            string=lambda s: s and s.strip() in WEEKDAY_MAP
        )
        if prev_node:
            weekday_label = prev_node.strip()
        if weekday_label is None:
            continue

        seen_ids.add(mikan_id)
        results.append(
            {
                "mikan_id": mikan_id,
                "title": title,
                "weekday": WEEKDAY_MAP[weekday_label],
            }
        )

    return results


async def resolve_bgm_id(mikan_id: int):
    """解析单个Mikan番剧详情页,提取对应的Bangumi(bgm.tv)条目编号。"""
    url = f"{BASE_URL}/Home/Bangumi/{mikan_id}"
    async with httpx.AsyncClient(
        headers=HEADERS, timeout=15.0, follow_redirects=True
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        html = resp.text

    match = re.search(r"bgm\.tv/subject/(\d+)", html)
    if not match:
        return None
    return int(match.group(1))


async def resolve_bgm_ids_batch(mikan_ids: list[int]):
    """并发解析多个mikan_id对应的bgm_id,限制并发避免把Mikan打挂。"""
    semaphore = asyncio.Semaphore(8)

    async def resolve_one(mid: int):
        async with semaphore:
            try:
                bgm_id = await resolve_bgm_id(mid)
            except Exception:
                bgm_id = None
            return mid, bgm_id

    tasks = [resolve_one(mid) for mid in mikan_ids]
    results = await asyncio.gather(*tasks)
    return dict(results)


def get_cache():
    return _schedule_cache


def is_cache_valid():
    return _schedule_cache["data"] is not None and _schedule_cache[
        "expires_at"
    ] > time.time()


def set_cache(data):
    _schedule_cache["data"] = data
    _schedule_cache["expires_at"] = time.time() + CACHE_TTL_SECONDS
