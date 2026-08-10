"""对应前端 SettingsPage(设置):下载/媒体库路径、轮询间隔、qBittorrent连接检测、代理连通性诊断。"""
import asyncio
import time

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

import config_store
import qbittorrent_client
from database import get_db
from routers.library import scan_and_update_library
from schemas import ProxyTestRequest, SettingsUpdate
from services import library_repair
from services.common import get_setting, upsert_setting
from services.proxy import get_proxy_url, get_system_proxy, set_proxy_url_cache

router = APIRouter(tags=["设置"])

# 代理诊断要挨个探的站点。选的都是各功能实际会请求的域名,而不是随便找个
# "能上网就行"的地址——国内网络下这几个的可达性差别很大(实测api.bgm.tv能直连,
# 但图床lain.bgm.tv和主站bgm.tv经常不行),分开报才看得出到底是哪一段断了。
PROXY_PROBE_TIMEOUT = 10.0
# Bangumi 相关的探测点是固定的(追更/详情页都要用),下载源的探测点则按当前启用/勾选的源
# 动态生成(见 _source_probes),换了源/改了地址/停用了某个源,测试对象自动跟着变。
BANGUMI_PROBES = [
    ("Bangumi API", "https://api.bgm.tv/calendar", "追更页的放送数据"),
    ("Bangumi 主站", "https://bgm.tv/subject/1", "详情页内嵌的那块网页"),
]


def _source_probes(source_ids: list[str] | None) -> list[dict]:
    """按要探测的下载源 id 生成探测点(名字/地址/说明)。
    source_ids=None(设置页没传)时回落到"当前已启用的源";传了就按传入的交集来
    (设置页把当前勾选启用的源传过来,能反映还没保存的改动)。地址取各源当前配置的主 URL。"""
    from sources.registry import enabled_sources, get_source, has_source
    if source_ids is None:
        adapters = enabled_sources()
    else:
        adapters = [get_source(sid) for sid in source_ids if has_source(sid)]
    return [a.proxy_probe() for a in adapters]


@router.get("/settings")
def get_settings(db: Session = Depends(get_db)):
    return {
        "download_root": get_setting(db, "download_root", config_store.DEFAULTS["download_root"]),
        "library_root": get_setting(db, "library_root", config_store.DEFAULTS["library_root"]),
        "potplayer_path": get_setting(db, "potplayer_path", config_store.DEFAULTS["potplayer_path"]),
        "player_mode": get_setting(db, "player_mode", config_store.DEFAULTS["player_mode"]),
        "default_source": get_setting(db, "default_source", config_store.DEFAULTS["default_source"]),
        "default_home_view": get_setting(
            db, "default_home_view", config_store.DEFAULTS["default_home_view"]
        ),
        "proxy_url": get_setting(db, "proxy_url", config_store.DEFAULTS["proxy_url"]),
        # 下载源覆盖配置的原始 JSON 字符串;设置页拿它 + GET /resources/sources 的默认值渲染编辑器
        "download_sources": get_setting(db, "download_sources", config_store.DEFAULTS["download_sources"]),
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
        "rss_poll_interval_seconds": int(
            get_setting(
                db,
                "rss_poll_interval_seconds",
                config_store.DEFAULTS["rss_poll_interval_seconds"],
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
        "rss_poll_interval_seconds": str(payload.rss_poll_interval_seconds),
        "default_source": payload.default_source,
        "default_home_view": payload.default_home_view,
        "proxy_url": payload.proxy_url,
        # 必须放进这个 values dict:write_ini 会用它整段重写 INI 的 [settings] 段,
        # 漏掉的话每次保存都会把下载源覆盖配置从 INI 抹掉(SQLite 副本还在,但两边会不一致)。
        "download_sources": payload.download_sources,
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

        source_probes = _source_probes(payload.sources)
        probes = [
            _probe(client, "Bangumi 图床", cover_url, f"封面图 — {cover_scheme_note}"),
            *[_probe(client, name, url, note) for name, url, note in BANGUMI_PROBES],
            *[_probe(client, p["name"], p["url"], p["note"]) for p in source_probes],
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


@router.get("/library/repair/scan")
async def scan_library_repair(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """"修复媒体库"按钮:分两部分:
    1) 按当前改名规则重算每个已入库文件的目标路径,跟磁盘实际路径不一致的记为改名建议;
    2) RenamedFile/AnimeFolder里指向磁盘上已不存在的文件/目录的孤儿记录。

    第1步的遍历入口是LocalMedia表,如果用户很久没点过"影视库"页的"刷新 & 扫描"
    (GET /library/scan),LocalMedia可能落后于磁盘实际内容(新增的番剧文件夹还没
    登记进去),会导致这些番剧完全不出现在扫描结果里,看起来像是"没问题"其实只是
    没扫到——所以这里先重新跑一遍scan_and_update_library同步LocalMedia,
    保证不会因为索引过期而漏检。这一步不动任何视频文件,只增删LocalMedia这一张
    索引表的行,是"影视库"页本来就在用的常规操作,不算破坏性写入。

    扫描之前还要先清掉季度关系缓存(见library_repair.reset_season_cache):这张表
    可能是旧版本算法写的,拿它算出来的改名建议会改错用户的文件。
    """
    library_repair.reset_season_cache(db)
    await scan_and_update_library(background_tasks, db)
    rename_mismatches = await library_repair.scan_rename_mismatches(db)
    orphans = library_repair.scan_orphaned_records(db)
    return {
        "rename_mismatches": rename_mismatches,
        **orphans,
    }


class RepairApplyRequest(BaseModel):
    fix_renames: bool = False
    rename_paths: list[str] | None = None  # None代表扫描结果里未被阻塞的全部改名建议
    clean_local_media: bool = False
    clean_renamed_files: bool = False
    clean_anime_folders: bool = False


@router.post("/library/repair/apply")
async def apply_library_repair(
    payload: RepairApplyRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """按用户勾选的分类实际执行修复。应用前重新跑一次对应的只读检查(TOCTOU防护),
    不信任前端传来的、可能已经过期的扫描结果快照。
    """
    result: dict = {}

    if payload.fix_renames:
        selected = set(payload.rename_paths) if payload.rename_paths is not None else None
        result["renames"] = await library_repair.apply_rename_fixes(db, selected)

    if payload.clean_local_media:
        result["local_media"] = await scan_and_update_library(background_tasks, db)

    if payload.clean_renamed_files or payload.clean_anime_folders:
        result["orphans"] = library_repair.apply_orphan_cleanup(db, {
            "clean_renamed_files": payload.clean_renamed_files,
            "clean_anime_folders": payload.clean_anime_folders,
        })

    return result
