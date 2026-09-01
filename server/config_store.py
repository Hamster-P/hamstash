"""
本地INI配置文件,跟数据库里的AppSetting表内容保持同步(双写),
防止升级/迁移/误删数据库时设定跟着丢——INI是纯文本,出问题时人工也能直接打开改。
"""
import configparser
import os

from paths import get_default_download_root, get_default_library_root, get_settings_ini_default

# 开发环境下放在源码目录的data/子目录;PyInstaller冻结成exe(Windows服务部署)时
# 自动指向%ProgramData%\hamstash\settings.ini,详见paths.py。
# Docker部署可以继续用SETTINGS_INI_PATH=/app/data/settings.ini这类容器内路径覆盖。
INI_PATH = os.getenv("SETTINGS_INI_PATH", str(get_settings_ini_default()))
DEFAULTS = {
    # 默认落在软件安装目录下(见paths.py::get_install_dir),而不是写死的盘符路径——
    # 装在哪个盘就默认用哪个盘,同时安装脚本(hooks.nsh)会在安装时预先建好这两个
    # 文件夹,首次启动不用用户自己手动建目录。
    "download_root": str(get_default_download_root()),
    "library_root": str(get_default_library_root()),
    "rename_poll_interval_seconds": "300",
    # RSS引擎(services/rss_poller.py)自动轮询间隔,默认30分钟——跟qBittorrent自带
    # RSS刷新的默认节奏保持一致,同时也是对dmhy之前被限流历史的保守考量。
    "rss_poll_interval_seconds": "1800",
    "potplayer_path": r"C:\Program Files\DAUM\PotPlayer\PotPlayerMini64.exe",
    "player_mode": "external",  # external=用户指定的PotPlayer等外置播放器 / builtin=内置mpv
    "default_source": "dmhy",  # 下载页默认选中的数据源: dmhy / animegarden / nyaa
    "default_home_view": "tracking",  # 软件启动时默认显示的页面: tracking/search/library
    # 访问Bangumi/dmhy/animegarden/nyaa/mikan等外部站点时用的代理地址,例如
    # http://127.0.0.1:8000(Clash等工具的本地混合端口)。留空=直连,不走代理。
    # 只影响这几个外部站点的请求,不影响本地qBittorrent连接。
    "proxy_url": "",
    "qbit_host": os.getenv("QBIT_HOST", "127.0.0.1"),
    "qbit_port": os.getenv("QBIT_PORT", "8080"),
    "qbit_username": os.getenv("QBIT_USERNAME", "admin"),
    "qbit_password": os.getenv("QBIT_PASSWORD", ""),
    "qbit_setup_completed": "false",  # 引导向导是否已经走完,前端据此决定要不要弹首次引导
    "library_sort_mode": "default",  # 影视库列表页排序方式记忆: default/recent_watched/recent_updated
    # 媒体库默认封面策略(未手动选图的条目适用):
    # latest_tv=家族树里最新一季TV的图 / first_season=第一季的图 / matched=直接用匹配条目本身的图。
    # 无TV季的纯剧场版家族一律回退用匹配条目本身的图。
    "library_cover_strategy": "latest_tv",
    # 下载源的用户覆盖配置(JSON 字符串),形如
    # {"dmhy":{"enabled":true,"search_url":"...","rss_url":"..."}, "nyaa":{"enabled":false}, ...}。
    # 留空=全部用各 source adapter 的内置默认。用于站点换域名/换镜像、临时停用某个源。
    # 读取见 sources/base.py::source_overrides,注册的源见 sources/registry.py。
    "download_sources": "",
    # 详情页元数据(TMDB背景图/LOGO/分级等)后台重试轮询间隔,默认1小时——
    # TMDB/arm-server数据变化不快,不用像下载轮询那样高频;暂不在设置页开放调整。
    "metadata_poll_interval_seconds": "3600",
}


def _ensure_dir() -> None:
    d = os.path.dirname(INI_PATH)
    if d:
        os.makedirs(d, exist_ok=True)


def read_ini() -> dict:
    _ensure_dir()
    parser = configparser.ConfigParser()
    if os.path.exists(INI_PATH):
        parser.read(INI_PATH, encoding="utf-8")
    section = dict(parser["settings"]) if parser.has_section("settings") else {}
    return {**DEFAULTS, **section}


def write_ini(values: dict) -> None:
    _ensure_dir()
    parser = configparser.ConfigParser()
    parser["settings"] = {k: str(v) for k, v in values.items()}
    with open(INI_PATH, "w", encoding="utf-8") as f:
        parser.write(f)


def update_ini_value(key: str, value: str) -> None:
    """只更新INI里的单个键,不影响其他键——跟write_ini()不同,后者会用传入的
    dict整个替换settings小节,单独调用会把没传的键全部冲掉。"""
    merged = read_ini()
    merged[key] = str(value)
    write_ini(merged)