"""arm-server(https://arm.haglund.dev)客户端 —— bgm_id->tmdb_id 两跳映射的第二跳。

背景每24小时自动同步 Fribb/anime-lists 数据,比 BangumiExtLinker 的静态快照更新更及时、
也更准(实测同一部番快照给的tmdb_id有类型错误,这里查出来的是对的)。查询key是
anidb_id或mal_id(不是bgm_id,bgm_id本身arm-server不认),所以调用前要先从
services/id_mapping_cache.py的快照里拿到anidb_id/mal_id。

跟bangumi_client.py同一种风格:客户端本身无状态,不做缓存,每次请求各自开client;
缓存下沉到调用方(services/anime_meta_resolver.py写AnimeMetaCache表)。
"""
import httpx

from services.proxy import get_proxy_url

BASE_URL = "https://arm.haglund.dev/api/v2/ids"
HEADERS = {
    "User-Agent": "hamstash/0.1 (personal project)",
    "Accept": "application/json",
}


async def _query(source: str, id_value: int) -> dict | None:
    async with httpx.AsyncClient(proxy=get_proxy_url(), follow_redirects=True) as client:
        resp = await client.get(
            BASE_URL, params={"source": source, "id": id_value}, headers=HEADERS, timeout=10.0
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()


async def query_by_anidb(anidb_id: int) -> dict | None:
    return await _query("anidb", anidb_id)


async def query_by_mal(mal_id: int) -> dict | None:
    return await _query("myanimelist", mal_id)
