"""BangumiExtLinker 映射快照(bgm_id -> anidb_id/mal_id/tmdb_id等)的下载与内存缓存。

这是 bgm_id -> tmdb_id 两跳映射的第一跳。快照是 Rhilip/BangumiExtLinker 项目维护的
一份静态 JSON(约4MB,23000+条),不是实时接口,没必要每次查询都重新下载——
下载一次,进程内存驻留,超过刷新间隔才重新拉一次。快照本身偶尔会缺失/给错
(比如把剧集的tmdb_id写成了对应电影版的id),第二跳(arm_client.py按anidb_id
现查)会用更准的实时结果覆盖这里给出的tmdb_id,这里只负责"尽量给出anidb_id
这把钥匙",拿不到才退回快照自带的tmdb_id兜底。
"""
import asyncio
import json
import time

import httpx

import paths
from services.proxy import get_proxy_url

SNAPSHOT_URL = "https://raw.githubusercontent.com/Rhilip/BangumiExtLinker/main/data/anime_map.json"

_REFRESH_INTERVAL_SECONDS = 24 * 3600

_lock = asyncio.Lock()
_snapshot: dict[int, dict] | None = None
_loaded_at: float = 0.0


def _parse_snapshot(raw: bytes) -> dict[int, dict]:
    data = json.loads(raw)
    result: dict[int, dict] = {}
    for item in data:
        try:
            bgm_id = int(item["bgm_id"])
        except (KeyError, TypeError, ValueError):
            continue
        result[bgm_id] = item
    return result


def _load_from_disk() -> dict[int, dict] | None:
    path = paths.get_id_mapping_snapshot_file()
    if not path.exists():
        return None
    try:
        return _parse_snapshot(path.read_bytes())
    except (OSError, ValueError) as e:
        print(f"[ID_MAPPING] 读取本地快照副本失败: {e}")
        return None


def _write_to_disk(raw: bytes) -> None:
    path = paths.get_id_mapping_snapshot_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_bytes(raw)
        tmp_path.replace(path)
    except OSError as e:
        print(f"[ID_MAPPING] 写入本地快照副本失败,不影响本次使用: {e}")


async def _download() -> dict[int, dict] | None:
    try:
        async with httpx.AsyncClient(proxy=get_proxy_url(), follow_redirects=True, timeout=30.0) as client:
            resp = await client.get(SNAPSHOT_URL)
            resp.raise_for_status()
            raw = resp.content
    except httpx.HTTPError as e:
        print(f"[ID_MAPPING] 下载BangumiExtLinker快照失败: {e}")
        return None

    try:
        snapshot = _parse_snapshot(raw)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"[ID_MAPPING] 解析BangumiExtLinker快照失败: {e}")
        return None

    _write_to_disk(raw)
    return snapshot


async def get_snapshot() -> dict[int, dict]:
    """命中内存直接返回;过期或从未加载过时重新下载,下载失败退回本地磁盘副本
    (再没有就退回空dict,调用方按"这一跳查不到"处理,不整体报错)。"""
    global _snapshot, _loaded_at

    now = time.monotonic()
    if _snapshot is not None and (now - _loaded_at) < _REFRESH_INTERVAL_SECONDS:
        return _snapshot

    async with _lock:
        # 双重检查:等锁的时候可能已经被另一个协程刷新过了
        now = time.monotonic()
        if _snapshot is not None and (now - _loaded_at) < _REFRESH_INTERVAL_SECONDS:
            return _snapshot

        downloaded = await _download()
        if downloaded is not None:
            _snapshot = downloaded
            _loaded_at = now
            return _snapshot

        if _snapshot is not None:
            # 刷新失败但内存里还有旧快照,继续用旧的,不因为一次网络失败就整体清空
            return _snapshot

        from_disk = _load_from_disk()
        _snapshot = from_disk if from_disk is not None else {}
        _loaded_at = now
        return _snapshot


async def lookup(bgm_id: int) -> dict | None:
    snapshot = await get_snapshot()
    return snapshot.get(bgm_id)
