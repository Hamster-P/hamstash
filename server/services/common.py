"""
共通工具函数与常量。

被多个路由模块(download / rss / settings)以及后台整理任务复用,
避免每个页面各自的路由文件里重复实现同一套小工具。
"""
import re
import time
import urllib.request

from sqlalchemy.orm import Session

import config_store
import rename_engine
from database import SessionLocal
from models import AnimeFamilyCache, AnimeFolder, AppSetting, RenamedFile

RSS_FOLDER = "anime-hub"  # 我们在qBittorrent的RSS订阅目录树里统一挂在这个文件夹下
ORGANIZE_TAG = "hub-organized"  # 打上这个标签代表后台整理任务已经处理过这个种子


def get_setting(db: Session, key: str, default: str) -> str:
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    return row.value if row and row.value else default


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


def staging_folder(download_root: str, anime_title: str, main_bgm_id: int | None) -> str:
    folder_name = rename_engine.build_anime_folder_name(anime_title, main_bgm_id)   
    return f"{download_root.rstrip(chr(92)).rstrip('/')}\\{folder_name}"


def upsert_anime_folder(
    db: Session,
    staging_folder_path: str,
    anime_title: str,
    main_bgm_id: int | None,
    season_bgm_id: int | None,
    auto_rename: bool,
) -> None:
    """
    暂存文件夹 -> 番名/系列根ID/季度专属ID/是否自动改名 的对照,供后台整理任务反查使用。
    main_bgm_id决定文件夹归属(同系列不同季共享同一个值),
    season_bgm_id是这次提交时的季度专属ID,给季度文字判断和集数偏移量计算用。
    同一个暂存文件夹如果被再次提交(同一部番又下载了一次,或者调整了auto_rename开关),
    以最新这次为准更新记录。
    """
    folder = (
        db.query(AnimeFolder)
        .filter(AnimeFolder.staging_folder == staging_folder_path)
        .first()
    )
    if folder:
        folder.anime_title = anime_title
        folder.main_bgm_id = main_bgm_id
        folder.season_bgm_id = season_bgm_id
        folder.auto_rename = auto_rename
    else:
        folder = AnimeFolder(
            staging_folder=staging_folder_path,
            anime_title=anime_title,
            main_bgm_id=main_bgm_id,
            season_bgm_id=season_bgm_id,
            auto_rename=auto_rename,
        )
        db.add(folder)
    db.commit()


def upsert_renamed_file(
    db: Session,
    torrent_hash: str,
    original_path: str,
    status: str,
    target: str | None = None,
    error: str | None = None,
    release_version: int = 1,
) -> None:
    row = (
        db.query(RenamedFile)
        .filter(
            RenamedFile.torrent_hash == torrent_hash,
            RenamedFile.original_path == original_path,
        )
        .first()
    )
    if row:
        row.status = status
        row.target_full_path = target
        row.error = error
        row.release_version = release_version
    else:
        row = RenamedFile(
            torrent_hash=torrent_hash,
            original_path=original_path,
            status=status, 
            target_full_path=target, 
            error=error,
            release_version=release_version,
        )
        db.add(row)
    db.commit()

async def resolve_series_identity(bgm_id: int | None, fallback_title: str):
    """
    返回 (folder_title, main_bgm_id, season_hint_text):
    - folder_title/main_bgm_id: 系列最早一部的名字/ID,决定文件夹归属,
      不管提交时是第几季,同系列都落进同一个文件夹。
    - season_hint_text: 这一季自己在Bangumi的官方名字,专门给季度文字判断用
      (根条目的名字通常不带"第几季"这种信息)。
    """
    import bangumi_client  # 延迟导入,避免模块加载顺序问题

    if not bgm_id:
        return fallback_title, None, fallback_title

    try:
        season_detail = await bangumi_client.get_subject_detail(bgm_id)
        season_title = season_detail.get("name_cn") or season_detail.get("name") or fallback_title
    except Exception:
        season_title = fallback_title

    try:
        main_id = await bangumi_client.resolve_root_subject_id(bgm_id)
        main_detail = await bangumi_client.get_subject_detail(main_id)
        folder_title = main_detail.get("name_cn") or main_detail.get("name") or season_title
    except Exception:
        main_id, folder_title = bgm_id, season_title

    return folder_title, main_id, season_title


async def resolve_tv_season_ordinal_cached(
    db: Session, season_bgm_id: int | None, main_bgm_id: int | None
) -> str | None:
    """
    bangumi_client.resolve_family_season_map()的持久化缓存包装:按bgm_id查
    AnimeFamilyCache表,命中直接返回season_ordinal,不发任何网络请求;没命中
    (这个bgm_id从没被算过——可能是全新番,也可能是老系列新出的剧场版/新一季)
    才触发一次完整的家族重新计算,并把整个家族的全部成员一次性写回缓存表
    (不止写被问到的这一个,顺带把家族里其他成员的结果也刷新一遍,自然覆盖
    "系列新增了一部作品"这种场景,不需要额外的过期/失效机制)。

    缓存没有TTL——这类关联家族的变动率很低(一部番一年也就新增1~2个成员),
    也没有证据表明Bangumi已收录条目的platform/集数/关联关系会事后变化,
    命中缓存直接用是安全的;真遇到需要强制刷新的场景,手动删掉对应行即可。
    """
    if not season_bgm_id or not main_bgm_id:
        return None

    cached = db.query(AnimeFamilyCache).filter(AnimeFamilyCache.bgm_id == season_bgm_id).first()
    if cached:
        return cached.season_ordinal

    import bangumi_client  # 延迟导入,避免跟bangumi_client反过来import本模块形成循环import

    family_map = await bangumi_client.resolve_family_season_map(main_bgm_id)
    for bid, info in family_map.items():
        # folder_bucket只是给人查表用的展示字段,用跟preview_rename_file()同一个
        # resolve_folder_bucket()算,保证不会出现"缓存表说进A桶、实际文件却进了B桶"的错位。
        media_type = rename_engine.classify_media_type("", info.get("platform"))
        folder_bucket = rename_engine.resolve_folder_bucket(
            media_type, main_bgm_id, info.get("season_ordinal")
        )

        row = db.query(AnimeFamilyCache).filter(AnimeFamilyCache.bgm_id == bid).first()
        if row is None:
            row = AnimeFamilyCache(bgm_id=bid)
            db.add(row)
        row.source_bgm_id = main_bgm_id
        row.name = info.get("name") or ""
        row.date = info.get("date")
        row.platform = info.get("platform")
        row.eps = info.get("eps")
        row.total_episodes = info.get("total_episodes")
        row.season_ordinal = info.get("season_ordinal")
        row.folder_bucket = folder_bucket
    db.commit()

    result = family_map.get(season_bgm_id)
    return result["season_ordinal"] if result else None


def sanitize_path_segment(text: str) -> str:
    """去掉qBittorrent RSS路径分隔符\\和/以及首尾空白,避免层级错乱。"""
    return re.sub(r"[\\/]+", "_", text).strip() or "未命名"

def get_current_version_at_target(db: Session, target_full_path: str):
    """
    查某个媒体库目标路径,当前已经落地的是第几版、以及是哪个种子占着这个位置。
    返回 (version, occupying_torrent_hash, occupying_torrent_file_count):
    occupying_torrent_file_count是"占着这个位置的种子,总共有多少个文件已经标记done"——
    等于1才说明它是单集种子,可以安全整体删除;大于1说明是合集,不能整体删除。
    """
    row = (
        db.query(RenamedFile)
        .filter(RenamedFile.target_full_path == target_full_path, RenamedFile.status == "done")
        .order_by(RenamedFile.release_version.desc())
        .first()
    )
    if not row:
        return 0, None, 0

    done_count = (
        db.query(RenamedFile)
        .filter(RenamedFile.torrent_hash == row.torrent_hash, RenamedFile.status == "done")
        .count()
    )
    return row.release_version, row.torrent_hash, done_count
