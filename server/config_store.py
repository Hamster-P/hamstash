"""
本地INI配置文件,跟数据库里的AppSetting表内容保持同步(双写),
防止升级/迁移/误删数据库时设定跟着丢——INI是纯文本,出问题时人工也能直接打开改。
"""
import configparser
import os

from paths import get_settings_ini_default

# 开发环境下放在源码目录的data/子目录;PyInstaller冻结成exe(Windows服务部署)时
# 自动指向%ProgramData%\hamstash\settings.ini,详见paths.py。
# Docker部署可以继续用SETTINGS_INI_PATH=/app/data/settings.ini这类容器内路径覆盖。
INI_PATH = os.getenv("SETTINGS_INI_PATH", str(get_settings_ini_default()))
DEFAULTS = {
    "download_root": r"D:\AnimeDownloads",
    "library_root": r"D:\AnimeLibrary",
    "rename_poll_interval_seconds": "300",
    # RSS引擎(services/rss_poller.py)自动轮询间隔,默认30分钟——跟qBittorrent自带
    # RSS刷新的默认节奏保持一致,同时也是对dmhy之前被限流历史的保守考量。
    "rss_poll_interval_seconds": "1800",
    "potplayer_path": r"C:\Program Files\DAUM\PotPlayer\PotPlayerMini64.exe",
    "player_mode": "external",  # external=用户指定的PotPlayer等外置播放器 / builtin=内置mpv
    "default_source": "dmhy",  # 下载页默认选中的数据源: dmhy / animegarden / nyaa
    # 访问Bangumi/dmhy/animegarden/nyaa/mikan等外部站点时用的代理地址,例如
    # http://127.0.0.1:8000(Clash等工具的本地混合端口)。留空=直连,不走代理。
    # 只影响这几个外部站点的请求,不影响本地qBittorrent连接。
    "proxy_url": "",
    "qbit_host": os.getenv("QBIT_HOST", "127.0.0.1"),
    "qbit_port": os.getenv("QBIT_PORT", "8080"),
    "qbit_username": os.getenv("QBIT_USERNAME", "admin"),
    "qbit_password": os.getenv("QBIT_PASSWORD", ""),
    "qbit_setup_completed": "false",  # 引导向导是否已经走完,前端据此决定要不要弹首次引导
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