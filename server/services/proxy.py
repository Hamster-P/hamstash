"""
代理地址解析与缓存。

设置页手填的地址优先；留空时回落到操作系统层面配置的代理。缓存有两层：
手动设置走内存缓存(应用启动/设置保存时更新)，系统代理探测走60秒TTL缓存
(见get_proxy_url/get_system_proxy各自的说明)。
"""
import time
import urllib.request

from database import SessionLocal
from services.common import get_setting

import config_store

_proxy_url_cache: str | None = None

# httpx能接的代理协议。Windows注册表里代理协议写的是"socks"时,
# urllib会归一化成socks4://——httpx不支持,必须丢掉,否则构造client时会抛
# httpx.InvalidURL(它不是httpx.HTTPError的子类,各处的except抓不到,会变成裸500)。
_SUPPORTED_PROXY_SCHEMES = ("http://", "https://", "socks5://")

_SYSTEM_PROXY_TTL_SECONDS = 60.0
_system_proxy_cache: str | None = None
_system_proxy_checked_at: float = 0.0


def _detect_system_proxy() -> str | None:
    """探测操作系统层面配置的代理。

    urllib.request.getproxies()在Windows上先读HTTP_PROXY/HTTPS_PROXY环境变量,
    读不到再回落到WinINET注册表(就是"设置-网络和Internet-代理"那一屏,
    Clash等工具开"系统代理"时写的也是这里),而且会帮我们把缺少scheme的
    "127.0.0.1:7890"补成"http://127.0.0.1:7890"。用标准库就够,不用自己碰winreg。

    注意:Clash的TUN/增强模式是在网络层透明转发的,系统代理那一栏是空的,
    这里探测不到任何东西——那种模式下本来也不需要应用层代理,直连即可。
    """
    try:
        proxies = urllib.request.getproxies()
    except Exception:
        return None

    candidate = proxies.get("https") or proxies.get("http")
    if not candidate:
        return None
    candidate = candidate.strip()
    if not candidate.lower().startswith(_SUPPORTED_PROXY_SCHEMES):
        return None
    return candidate


def get_system_proxy() -> str | None:
    """带TTL的系统代理探测结果。设置页的代理诊断也会直接调它来展示"探测到了什么"。


    get_proxy_url()被图片代理这类高频路径调用(一张封面一次),不能每次都去读注册表;
    但也不能只在启动时探测一次,否则用户中途开关系统代理必须重启才生效。
    折中成60秒缓存:最多一分钟才读一次注册表,开关代理最多一分钟后自动跟上。
    """
    global _system_proxy_cache, _system_proxy_checked_at

    now = time.monotonic()
    if now - _system_proxy_checked_at >= _SYSTEM_PROXY_TTL_SECONDS:
        _system_proxy_cache = _detect_system_proxy()
        _system_proxy_checked_at = now
    return _system_proxy_cache


def init_proxy_url_cache() -> None:
    """应用启动时调用一次,把proxy_url读进内存缓存。
    必须在进程开始处理请求之前调用——get_proxy_url()现在只读内存缓存,
    不再每次都开DB session(见下面说明),没有这一步缓存会一直是None。
    """
    global _proxy_url_cache
    db = SessionLocal()
    try:
        value = get_setting(db, "proxy_url", config_store.DEFAULTS["proxy_url"])
        _proxy_url_cache = value or None
    finally:
        db.close()


def set_proxy_url_cache(value: str | None) -> None:
    """设置页保存代理地址成功后调用,让内存缓存跟数据库保持同步——
    不这样做的话只能重启进程才能让get_proxy_url()看到最新值。"""
    global _proxy_url_cache
    _proxy_url_cache = value or None


def get_proxy_url() -> str | None:
    """
    给bangumi_client/resource_client/media这些请求外部动漫资源站/图床的模块用。
    直接读内存缓存,不查数据库——之前这里是每次调用都开一个SessionLocal做同步阻塞查询,
    在async路由里(尤其是media.py的图片代理,一张图片调用一次)会卡住FastAPI单线程的
    事件循环,图片一多(追更/搜索页几十上百张)所有并发请求都跟着排队变卡。
    缓存由init_proxy_url_cache()在应用启动时填充、set_proxy_url_cache()在设置保存时更新。
    返回None代表直连,不传给httpx的proxy参数。
    注意:只用于访问外部站点,本地qBittorrent连接(qbittorrent_client.py)不应该走代理。

    设置页手填的地址优先;留空时回落到操作系统层面配置的代理(见_detect_system_proxy),
    这样开了Clash系统代理的用户不用再去设置页把端口抄一遍。
    """
    if _proxy_url_cache:
        return _proxy_url_cache
    return get_system_proxy()
