"""对应前端 SettingsPage(设置):下载/媒体库路径、轮询间隔、qBittorrent连接检测、代理连通性诊断。"""
import asyncio
import time

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

import config_store
import qbittorrent_client
from database import get_db
from schemas import ProxyTestRequest, SettingsUpdate
from services.common import get_setting, upsert_setting
from services.proxy import get_proxy_url, get_system_proxy, set_proxy_url_cache

router = APIRouter(tags=["设置"])

# 代理诊断要挨个探的站点。选的都是各功能实际会请求的域名,而不是随便找个
# "能上网就行"的地址——国内网络下这几个的可达性差别很大(实测api.bgm.tv能直连,
# 但图床lain.bgm.tv和主站bgm.tv经常不行),分开报才看得出到底是哪一段断了。
PROXY_PROBE_TIMEOUT = 10.0
PROXY_PROBES = [
    ("Bangumi API", "https://api.bgm.tv/calendar", "追更页的放送数据"),
    ("Bangumi 主站", "https://bgm.tv/subject/1", "详情页内嵌的那块网页"),
    ("dmhy", "https://dmhy.org/topics/rss/rss.xml", "下载页数据源"),
    ("AnimeGarden", "https://api.animes.garden/resources", "下载页数据源"),
    ("nyaa", "https://nyaa.si", "下载页数据源"),
]


@router.get("/settings")
def get_settings(db: Session = Depends(get_db)):
    return {
        "download_root": get_setting(db, "download_root", config_store.DEFAULTS["download_root"]),
        "library_root": get_setting(db, "library_root", config_store.DEFAULTS["library_root"]),
        "potplayer_path": get_setting(db, "potplayer_path", config_store.DEFAULTS["potplayer_path"]),
        "player_mode": get_setting(db, "player_mode", config_store.DEFAULTS["player_mode"]),
        "default_source": get_setting(db, "default_source", config_store.DEFAULTS["default_source"]),
        "proxy_url": get_setting(db, "proxy_url", config_store.DEFAULTS["proxy_url"]),
        # 只读:实际生效的代理地址(手填留空时是探测到的系统代理)。跟proxy_url分开返回,
        # 不能把探测值回填进设置页输入框——那样用户一点保存就把探测结果固化成手动配置了。
        # 客户端Rust侧创建内嵌webview时读的是这个值(见src-tauri/src/lib.rs)。
        "effective_proxy_url": get_proxy_url() or "",
        "rename_poll_interval_seconds": int(
            get_setting(
                db,
                "rename_poll_interval_seconds",
                config_store.DEFAULTS["rename_poll_interval_seconds"],
            )
        ),
        "qbit_host": get_setting(db, "qbit_host", config_store.DEFAULTS["qbit_host"]),
        "qbit_port": int(get_setting(db, "qbit_port", config_store.DEFAULTS["qbit_port"])),
        "qbit_username": get_setting(db, "qbit_username", config_store.DEFAULTS["qbit_username"]),
        # qbit_password 不回传:设置页/引导向导只负责写入,不需要把已保存密码明文读回前端。
        "qbit_setup_completed": get_setting(
            db, "qbit_setup_completed", config_store.DEFAULTS["qbit_setup_completed"]
        ) == "true",
    }


@router.put("/settings")
def update_settings(payload: SettingsUpdate, db: Session = Depends(get_db)):
    old_library_root = get_setting(db, "library_root", config_store.DEFAULTS["library_root"])

    values = {
        "download_root": payload.download_root,
        "library_root": payload.library_root,
        "potplayer_path": payload.potplayer_path,
        "player_mode": payload.player_mode,
        "rename_poll_interval_seconds": str(payload.rename_poll_interval_seconds),
        "default_source": payload.default_source,
        "proxy_url": payload.proxy_url,
    }
    for key, value in values.items():
        upsert_setting(db, key, value)
    config_store.write_ini(values)  # 双写本地INI,避免数据库丢失/迁移时设定跟着丢
    set_proxy_url_cache(payload.proxy_url)  # 同步内存缓存,不重启也能立刻用上新代理设置

    # 库目录变了:数据库要挪到新目录下,但这是进程启动时才做的引导逻辑(见database.py),
    # 不在这里做运行中途的热搬运,提示前端需要重启应用才会真正生效。
    restart_required = payload.library_root != old_library_root
    return {**values, "restart_required": restart_required}


async def _probe(client: httpx.AsyncClient, name: str, url: str, note: str) -> dict:
    """探一个站点,把"能不能连上/多久/返回什么"如实记下来。

    连不上和连上了但返回4xx/5xx要分开报:前者是代理/网络的问题,后者说明这条链路
    其实是通的(比如nyaa偶尔返回403),对定位问题是完全不同的两件事。
    """
    started = time.monotonic()
    try:
        resp = await client.get(url)
        elapsed = int((time.monotonic() - started) * 1000)
        return {
            "name": name,
            "url": url,
            "note": note,
            "level": "ok" if resp.status_code < 400 else "warn",
            "status": resp.status_code,
            "elapsed_ms": elapsed,
            "detail": f"HTTP {resp.status_code}, {len(resp.content)} 字节",
        }
    except Exception as e:
        elapsed = int((time.monotonic() - started) * 1000)
        return {
            "name": name,
            "url": url,
            "note": note,
            "level": "fail",
            "status": None,
            "elapsed_ms": elapsed,
            # 带上异常类名:ConnectTimeout/ProxyError/ConnectError指向的原因完全不同,
            # 只有一句message的话根本分不出是代理没开、代理地址填错,还是被墙。
            "detail": f"{type(e).__name__}: {e}" if str(e) else type(e).__name__,
        }


@router.post("/settings/proxy-test")
async def test_proxy(payload: ProxyTestRequest):
    """设置页"测试代理"按钮:按实际运行时的取值规则挑代理,然后挨个探各功能依赖的站点。

    不改任何配置,纯读。测的是输入框里当前的值,所以可以先测通了再保存。
    """
    manual = payload.proxy_url.strip()
    detected = get_system_proxy()
    if manual:
        proxy, source = manual, "manual"
    elif detected:
        proxy, source = detected, "system"
    else:
        proxy, source = None, "none"

    checks: list[dict] = []
    async with httpx.AsyncClient(
        proxy=proxy,
        timeout=PROXY_PROBE_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": "hamstash/0.1 (personal project)"},
    ) as client:
        # 封面图单独拎出来先做,因为要用的URL得从calendar的响应里取——
        # 直接探"追更页真正会去请求的那张图",比写死一个可能早就下架的地址靠谱,
        # 顺便还能把API返回的scheme报出来(老版/calendar返回的是明文http://,
        # 而WebView把它当混合内容拦掉,正是追更页封面全空的原因)。
        cover_url = None
        cover_scheme_note = ""
        try:
            resp = await client.get("https://api.bgm.tv/calendar")
            resp.raise_for_status()
            for day in resp.json():
                for item in day.get("items", []):
                    raw = (item.get("images") or {}).get("large")
                    if raw:
                        cover_url = raw
                        break
                if cover_url:
                    break
        except Exception:
            pass

        if cover_url:
            scheme = cover_url.split("://")[0] if "://" in cover_url else "协议相对(//)"
            cover_scheme_note = f"API返回的地址是 {scheme}"
            # 跟media.py的图片代理做同样的归一化,保证探的就是前端最终会请求的地址
            if cover_url.startswith("//"):
                cover_url = "https:" + cover_url
            if cover_url.startswith("http://"):
                cover_url = "https://" + cover_url[len("http://"):]
        else:
            cover_url = "https://lain.bgm.tv/pic/cover/l/ce/e2/456080_C4q4C.jpg"
            cover_scheme_note = "拿不到实时封面地址,用了一个固定地址兜底"

        probes = [
            _probe(client, "Bangumi 图床", cover_url, f"封面图 — {cover_scheme_note}"),
            *[_probe(client, name, url, note) for name, url, note in PROXY_PROBES],
        ]
        checks = await asyncio.gather(*probes)

    return {
        "proxy_in_use": proxy or "",
        "proxy_source": source,  # manual=设置页手填 / system=探测到的系统代理 / none=直连
        "system_proxy_detected": detected or "",
        "checks": checks,
    }


@router.get("/qbittorrent/status")
async def qbittorrent_status():
    """健康检查:确认后端能不能正常登录qBittorrent。"""
    ok = await qbittorrent_client.test_connection()
    return {"connected": ok}
