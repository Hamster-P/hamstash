"""通用图片代理:前端各页面(追更/搜索/详情/影视库)展示的Bangumi封面图都改成
走这个接口,而不是<img>直接连Bangumi图床——WebView的<img>请求是前端自己发的,
不会经过后端,也就吃不到设置里配的网络代理(参考bangumi_client.py/resource_client.py
接proxy_url的思路,这里是给"图片"这类前端直连资源补上同样的代理能力)。

同时兼职做本地磁盘缓存:第一次拉到的封面字节落盘,之后同一张图直接读本地文件,
不用再摸网络——离线时(比如断网/代理临时不通)已经看过的封面依然能显示,
顺带让重复加载比每次都经代理转发快得多。Bangumi封面URL是内容寻址式的
(路径里带哈希片段,同一个URL长期指向同一张图),所以缓存不设过期时间。
"""
import asyncio
import hashlib
import secrets
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, Response

import paths
from services.proxy import get_proxy_url

router = APIRouter(tags=["图片代理"])

# 只允许代理Bangumi自己的图床域名,避免这个接口被当成任意URL的开放代理使用(SSRF)。
ALLOWED_IMAGE_HOSTS = {"lain.bgm.tv"}

# 转发时带上跟bangumi_client一致的UA,避免CDN对httpx默认UA区别对待。
FORWARD_HEADERS = {
    "User-Agent": "hamstash/0.1 (personal project)",
    "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
}

_CACHE_DIR = paths.get_image_cache_dir()
_EXT_BY_CONTENT_TYPE = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def _cache_path_for(url: str, content_type: str = "image/jpeg"):
    """按URL算缓存文件路径。不知道content-type时(缓存命中阶段,还没请求过)
    默认按.jpg找——绝大多数Bangumi封面就是jpg,找不到才需要真正发请求确认类型。
    """
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    ext = _EXT_BY_CONTENT_TYPE.get(content_type, ".jpg")
    return _CACHE_DIR / f"{digest}{ext}"


def _find_cached_file(url: str):
    """未知content-type时,把几种可能的扩展名都试一遍——
    缓存写入时用的是拉取到的真实content-type,读取时不能假设固定是.jpg。
    st_size > 0是防万一之前一次写入中途被打断留下的空文件,不当成有效缓存。
    """
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    for ext in _EXT_BY_CONTENT_TYPE.values():
        candidate = _CACHE_DIR / f"{digest}{ext}"
        if candidate.exists() and candidate.stat().st_size > 0:
            return candidate
    return None


def _write_cache(url: str, content_type: str, content: bytes) -> None:
    """先写临时文件再os.replace成最终文件名,原子写入——避免进程中途被杀掉/
    磁盘写满等情况留下一个半截的坏缓存文件,之后一直被_find_cached_file当成
    "命中"提供残缺数据。写失败(比如磁盘满了)只打日志,不影响本次请求已经
    成功拿到的图片正常返回给前端,缓存只是个加分项,不是关键路径。
    """
    target = _cache_path_for(url, content_type)
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path = target.with_name(f"{target.name}.tmp{secrets.token_hex(4)}")
        tmp_path.write_bytes(content)
        tmp_path.replace(target)
    except OSError as e:
        print(f"[image_proxy] 写入图片缓存失败,不影响本次返回: {e}")

# 追更/影视库这类页面一次性会同时请求几十张封面图,全部并发怼向本地代理容易
# 让代理/对面CDN瞬时过载导致部分请求超时——限制一下同时在跑的图片请求数,
# 超出的排队等,而不是一股脑全冲过去。
_CONCURRENCY_LIMIT = asyncio.Semaphore(6)
_MAX_ATTEMPTS = 2  # 失败重试一次,应对代理/CDN的瞬时抖动

# 配了代理时,所有图片请求复用同一个连接池化的client,而不是每张图都新建一个——
# 后者对同一个host(lain.bgm.tv)没有连接复用,每张图都要重新走一遍TCP/TLS握手,
# 代理场景下(本来就比直连多一跳)会进一步放大延迟。用一个哨兵值区分"还没建过"
# 和"代理设置变了要重建",避免代理地址变更后还在用指向旧代理的client。
_UNSET = object()
_client_lock = asyncio.Lock()
_cached_client: httpx.AsyncClient | None = None
_cached_client_proxy: object = _UNSET


async def _get_pooled_client(proxy: str) -> httpx.AsyncClient:
    global _cached_client, _cached_client_proxy
    async with _client_lock:
        if _cached_client is None or _cached_client_proxy != proxy:
            if _cached_client is not None:
                await _cached_client.aclose()
            _cached_client = httpx.AsyncClient(
                proxy=proxy,
                timeout=15.0,
                follow_redirects=True,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
            _cached_client_proxy = proxy
        return _cached_client


@router.get("/media/image-proxy")
async def image_proxy(url: str = Query(...)):
    # Bangumi老版接口(比如/calendar,追更页用的)经常返回协议相对URL,
    # 形如"//lain.bgm.tv/pic/cover/l/xxx.jpg"(没有http:/https:前缀)——
    # 新版/v0/...接口返回的是完整URL。这里统一补全,不然urlparse出来
    # scheme是空字符串,会被下面的校验误判成"不支持的来源"直接拒绝。
    if url.startswith("//"):
        url = "https:" + url

    # 老版/calendar返回的是明文"http://lain.bgm.tv/..."(实测追更页111部封面全是http)。
    # 前端WebView的页面源是http://localhost:3000(dev)或http://tauri.localhost(prod),
    # 两者都以.localhost结尾,Chromium把它判定成secure context,于是明文http的子资源
    # 算作混合内容被拦掉——表现就是追更页封面全空,而走/v0/接口(返回https)的详情页
    # 封面正常。图床本身https可用,这里无条件升级,让下面302回去的地址也是https。
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or parsed.hostname not in ALLOWED_IMAGE_HOSTS:
        raise HTTPException(status_code=400, detail="不支持的图片来源")

    # 缓存命中:直接读本地文件,不摸网络——不管有没有配代理、网络通不通都能命中,
    # 这是离线能力的关键,顺带比每次都经代理转发快得多。FileResponse不传
    # media_type时会按文件名后缀自动猜,我们缓存文件名后缀就是真实content-type
    # 映射来的,交给它自动识别即可。
    cached = _find_cached_file(url)
    if cached is not None:
        return FileResponse(cached)

    # 缓存未命中:不管有没有配代理都要真正经过后端拉一次字节(proxy为None时
    # httpx.AsyncClient(proxy=None)就是直连),不然字节压根不经过我们的进程,
    # 没机会写入缓存。
    proxy = get_proxy_url()
    async with _CONCURRENCY_LIMIT:
        last_error: Exception | None = None
        for _ in range(_MAX_ATTEMPTS):
            try:
                client = await _get_pooled_client(proxy)
                resp = await client.get(url, headers=FORWARD_HEADERS)
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "image/jpeg")
                _write_cache(url, content_type, resp.content)
                return Response(content=resp.content, media_type=content_type)
            except (httpx.HTTPError, httpx.InvalidURL) as e:
                # httpx.InvalidURL不是httpx.HTTPError的子类,单独列出来兜底——
                # 代理地址格式已经在保存时校验过了(schemas.py),这里只是防万一
                # (比如升级前保存的旧值),不至于让整个请求变成没有提示的500。
                last_error = e

    raise HTTPException(status_code=502, detail=f"获取图片失败: {last_error}")
