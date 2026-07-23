"""通用图片代理:前端各页面(追更/搜索/详情/影视库)展示的Bangumi封面图都改成
走这个接口,而不是<img>直接连Bangumi图床——WebView的<img>请求是前端自己发的,
不会经过后端,也就吃不到设置里配的网络代理(参考bangumi_client.py/resource_client.py
接proxy_url的思路,这里是给"图片"这类前端直连资源补上同样的代理能力)。
"""
import asyncio
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse, Response

from services.common import get_proxy_url

router = APIRouter(tags=["图片代理"])

# 只允许代理Bangumi自己的图床域名,避免这个接口被当成任意URL的开放代理使用(SSRF)。
ALLOWED_IMAGE_HOSTS = {"lain.bgm.tv"}

# 转发时带上跟bangumi_client一致的UA,避免CDN对httpx默认UA区别对待。
FORWARD_HEADERS = {
    "User-Agent": "hamstash/0.1 (personal project)",
    "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
}

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

    proxy = get_proxy_url()
    if not proxy:
        # 没配代理时没必要把图片字节流绕后端转发一遍——直接302让浏览器自己去连
        # 图床,走浏览器原生的并发连接池/HTTP缓存/CDN优化,跟改造前一样快;
        # 只有配了代理才需要真的经过后端(带上代理)去拉取。
        return RedirectResponse(url)

    async with _CONCURRENCY_LIMIT:
        last_error: Exception | None = None
        for _ in range(_MAX_ATTEMPTS):
            try:
                client = await _get_pooled_client(proxy)
                resp = await client.get(url, headers=FORWARD_HEADERS)
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "image/jpeg")
                return Response(content=resp.content, media_type=content_type)
            except (httpx.HTTPError, httpx.InvalidURL) as e:
                # httpx.InvalidURL不是httpx.HTTPError的子类,单独列出来兜底——
                # 代理地址格式已经在保存时校验过了(schemas.py),这里只是防万一
                # (比如升级前保存的旧值),不至于让整个请求变成没有提示的500。
                last_error = e

    raise HTTPException(status_code=502, detail=f"获取图片失败: {last_error}")
