"""TMDB(themoviedb.org)客户端 —— 详情页背景图/LOGO/分级/标签/类型/工作室的唯一来源。

Bangumi不提供这些字段(没有横版背景图,也没有分级/标签/工作室),接入TMDB是
仿Jellyfin详情页样式的前提。TMDB_API_KEY是项目级共享key(在TMDB官网以
"Developer/个人非商业用途"申请,免费),内置进代码全体用户共享,不走设置页/
数据库——这是TMDB官方认可的用法,Jellyfin自己的TMDB插件也是同一种做法。

跟bangumi_client.py同一种风格:客户端本身无状态,不做缓存,每次请求各自开client;
缓存下沉到调用方(services/anime_meta_resolver.py写AnimeMetaCache表)。
"""
import httpx

from services.proxy import get_proxy_url

BASE_URL = "https://api.themoviedb.org/3"
IMAGE_BASE_URL = "https://image.tmdb.org/t/p"

TMDB_API_KEY = "90549bd0f1c9cb7773ff2c009c6e9b16"

HEADERS = {
    "User-Agent": "hamstash/0.1 (personal project)",
    "Accept": "application/json",
}

# 有语言的图优先中文,没标语言(null,纯图无文字)的图也保留——LOGO/背景图经常是
# 后者,不带include_image_language=...,null这个值,TMDB默认只返回"en"语言的图,
# 动画类条目常常直接查不到任何结果。
_APPEND = "images,content_ratings,keywords,external_ids"
_IMAGE_LANGUAGES = "zh,ja,en,null"

# 分级优先顺序:日版>美版>随便挑一个有的,没有就是None(前端不渲染这一行)。
_CONTENT_RATING_PREFERENCE = ("JP", "US")

# LOGO语言优先顺序:中文>日文>英文>随便挑一个有的。
_LOGO_LANGUAGE_PREFERENCE = ("zh", "ja", "en")

_APPEND_MOVIE = "images,keywords,credits,release_dates,external_ids"


async def get_tv_detail(tmdb_id: int, season: int | None = None) -> dict:
    url = f"{BASE_URL}/tv/{tmdb_id}"
    async with httpx.AsyncClient(proxy=get_proxy_url(), follow_redirects=True) as client:
        resp = await client.get(
            url,
            params={
                "api_key": TMDB_API_KEY,
                "language": "zh-CN",
                "append_to_response": _APPEND,
                "include_image_language": _IMAGE_LANGUAGES,
            },
            headers=HEADERS,
            timeout=15.0,
        )
        resp.raise_for_status()
        return resp.json()


async def get_movie_detail(tmdb_id: int) -> dict:
    """剧场版/OVA走这个,不是get_tv_detail——TMDB的TV库和电影库ID命名空间是分开的,
    拿电影ID去请求/tv/{id}会404(实测铃芽之旅案例)。"""
    url = f"{BASE_URL}/movie/{tmdb_id}"
    async with httpx.AsyncClient(proxy=get_proxy_url(), follow_redirects=True) as client:
        resp = await client.get(
            url,
            params={
                "api_key": TMDB_API_KEY,
                "language": "zh-CN",
                "append_to_response": _APPEND_MOVIE,
                "include_image_language": _IMAGE_LANGUAGES,
            },
            headers=HEADERS,
            timeout=15.0,
        )
        resp.raise_for_status()
        return resp.json()


async def search_tv(query: str, year: int | None = None) -> dict | None:
    """两跳ID桥接(BangumiExtLinker快照+arm-server)都查不到anidb_id/tmdb_id时的
    第三级兜底:直接拿原文标题去TMDB搜——TMDB自己的收录速度比AniDB快得多,
    新番当天社区就可能建好条目,而AniDB/Fribb那边往往要等上一阵子才收录
    (实测"恶女不才，请多关照"这个案例,AniDB还没收录,但TMDB已经有条目了)。
    返回第一条搜索结果(TMDB自己按相关度排序),查不到返回None。"""
    params = {"api_key": TMDB_API_KEY, "language": "zh-CN", "query": query}
    if year:
        params["first_air_date_year"] = year
    async with httpx.AsyncClient(proxy=get_proxy_url(), follow_redirects=True) as client:
        resp = await client.get(f"{BASE_URL}/search/tv", params=params, headers=HEADERS, timeout=15.0)
        resp.raise_for_status()
        results = resp.json().get("results") or []
        return results[0] if results else None


async def search_movie(query: str, year: int | None = None) -> dict | None:
    """跟search_tv同一套逻辑,查电影库,给剧场版/OVA当兜底。"""
    params = {"api_key": TMDB_API_KEY, "language": "zh-CN", "query": query}
    if year:
        params["primary_release_year"] = year
    async with httpx.AsyncClient(proxy=get_proxy_url(), follow_redirects=True) as client:
        resp = await client.get(f"{BASE_URL}/search/movie", params=params, headers=HEADERS, timeout=15.0)
        resp.raise_for_status()
        results = resp.json().get("results") or []
        return results[0] if results else None


def _image_url(file_path: str | None, size: str) -> str | None:
    if not file_path:
        return None
    return f"{IMAGE_BASE_URL}/{size}{file_path}"


def _pick_logo(logos: list[dict]) -> dict | None:
    """LOGO跟背景图相反,就是要带文字的图,按语言优先顺序选,没有偏好语言的就退回第一张。
    同语言可能有多张,TMDB本身按社区评分降序返回——只保留每个语言第一次出现的那张
    (评分最高的),不能用字典推导式直接{lang: item for item in logos}(会保留同语言
    最后一张,拿到评分较低的图)。"""
    if not logos:
        return None
    by_language: dict[str | None, dict] = {}
    for logo in logos:
        lang = logo.get("iso_639_1")
        if lang not in by_language:
            by_language[lang] = logo
    for lang in _LOGO_LANGUAGE_PREFERENCE:
        if by_language.get(lang):
            return by_language[lang]
    return logos[0]


def _pick_content_rating(data: dict) -> str | None:
    results = (data.get("content_ratings") or {}).get("results") or []
    by_country = {r.get("iso_3166_1"): r.get("rating") for r in results if r.get("rating")}
    for country in _CONTENT_RATING_PREFERENCE:
        if by_country.get(country):
            return by_country[country]
    return next(iter(by_country.values()), None)


def _pick_movie_certification(data: dict) -> str | None:
    """电影没有content_ratings,分级藏在release_dates.results[].release_dates[].certification里,
    同一国家可能有多条发行记录(院线/流媒体/数字发行等),取第一条非空的certification。"""
    results = (data.get("release_dates") or {}).get("results") or []
    by_country: dict[str, str] = {}
    for r in results:
        country = r.get("iso_3166_1")
        if not country or country in by_country:
            continue
        for rd in r.get("release_dates") or []:
            cert = rd.get("certification")
            if cert:
                by_country[country] = cert
                break
    for country in _CONTENT_RATING_PREFERENCE:
        if by_country.get(country):
            return by_country[country]
    return next(iter(by_country.values()), None)


def normalize_tmdb_tv(data: dict) -> dict:
    """提取详情页要展示的字段,列表字段(genres/tags/studios/creators)返回list[str],
    调用方(anime_meta_resolver.py)负责join成逗号分隔字符串再落库。"""
    images = data.get("images") or {}
    logo = _pick_logo(images.get("logos") or [])

    return {
        # 背景图直接用顶层backdrop_path,不在images.backdrops[]候选池里自己挑——
        # 这个字段就是TMDB官网本身展示这个条目时用的默认图,比我们自己按语言/分辨率
        # 启发式猜的更可靠(实测两个真实条目backdrop_path都跟官网显示的完全一致)。
        "backdrop_url": _image_url(data.get("backdrop_path"), "w1280"),
        "logo_url": _image_url(logo["file_path"], "w500") if logo else None,
        "content_rating": _pick_content_rating(data),
        "genres": [g["name"] for g in (data.get("genres") or []) if g.get("name")],
        "tags": [k["name"] for k in ((data.get("keywords") or {}).get("results") or []) if k.get("name")],
        "studios": [n["name"] for n in (data.get("networks") or []) if n.get("name")],
        "creators": [c["name"] for c in (data.get("created_by") or []) if c.get("name")],
    }


def normalize_tmdb_movie(data: dict) -> dict:
    """跟normalize_tmdb_tv返回同样的字段形状,给剧场版/OVA用。字段来源跟剧集版有几处
    不同:没有content_ratings(用release_dates的certification)、没有keywords.results
    (电影是keywords.keywords)、没有created_by/networks(用credits.crew的导演/
    production_companies当creators/studios)。"""
    images = data.get("images") or {}
    logo = _pick_logo(images.get("logos") or [])
    directors = [
        c["name"] for c in ((data.get("credits") or {}).get("crew") or [])
        if c.get("job") == "Director" and c.get("name")
    ]

    return {
        # 同normalize_tmdb_tv:直接用顶层backdrop_path,不自己挑。
        "backdrop_url": _image_url(data.get("backdrop_path"), "w1280"),
        "logo_url": _image_url(logo["file_path"], "w500") if logo else None,
        "content_rating": _pick_movie_certification(data),
        "genres": [g["name"] for g in (data.get("genres") or []) if g.get("name")],
        "tags": [k["name"] for k in ((data.get("keywords") or {}).get("keywords") or []) if k.get("name")],
        "studios": [c["name"] for c in (data.get("production_companies") or []) if c.get("name")],
        "creators": directors,
    }
