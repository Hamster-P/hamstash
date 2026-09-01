"""数据目录/配置文件该放哪的判断。

PyInstaller冻结成exe运行时(比如注册成Windows服务的部署方式)用独立于安装目录的
%ProgramData%,升级/卸载重装安装目录本身时才不会把用户数据搞丢;
源码/开发环境下沿用代码目录本身,方便调试直接看文件,行为不变。
"""
import os
import sys
from pathlib import Path

IS_FROZEN = getattr(sys, "frozen", False)


def get_data_dir() -> Path:
    """数据库/配置文件的存放根目录。"""
    if IS_FROZEN:
        return Path(os.getenv("ProgramData", r"C:\ProgramData")) / "hamstash"
    return Path(__file__).resolve().parent


def get_settings_ini_default() -> Path:
    data_dir = get_data_dir()
    return data_dir / "settings.ini" if IS_FROZEN else data_dir / "data" / "settings.ini"


def get_image_cache_dir() -> Path:
    """封面图片本地缓存目录,跟设置/数据库同一套"升级/卸载重装不丢数据"的规则。"""
    data_dir = get_data_dir()
    return data_dir / "image_cache" if IS_FROZEN else data_dir / "data" / "image_cache"


def get_id_mapping_snapshot_file() -> Path:
    """BangumiExtLinker映射快照(bgm_id->anidb_id等)本地磁盘副本,跟image_cache同一套
    "升级/卸载重装不丢数据"规则。下载失败时用这份旧副本兜底,而不是直接失效。"""
    data_dir = get_data_dir()
    base = data_dir if IS_FROZEN else data_dir / "data"
    return base / "id_mapping_snapshot.json"


def get_db_location_pointer_file() -> Path:
    """记录"数据库文件当前实际在哪"的一个极小指针文件,供database.py引导逻辑用。
    丢了也无所谓:引导逻辑会当作数据库还在老的默认位置,顶多重新走一遍搬运判断。
    """
    data_dir = get_data_dir()
    return data_dir / "db_location.txt" if IS_FROZEN else data_dir / "data" / "db_location.txt"


def get_install_dir() -> Path:
    """软件安装根目录,给下载暂存/媒体库根目录的默认值当锚点用。

    打包运行时(PyInstaller frozen)exe的实际布局是
    `$INSTDIR\\backend\\hamstash-server\\hamstash-server.exe`(见
    client/src-tauri/windows/hooks.nsh 里NSSM注册服务时用的路径),
    往上三层就是NSIS安装时用户选的$INSTDIR。源码/开发环境下没有安装目录这个概念,
    退回到本仓库根目录(server目录的上一级)。
    """
    if IS_FROZEN:
        return Path(sys.executable).resolve().parent.parent.parent
    return Path(__file__).resolve().parent.parent


def get_default_download_root() -> Path:
    """下载暂存目录默认值:安装目录下的 AnimeDownloads 文件夹。"""
    return get_install_dir() / "AnimeDownloads"


def get_default_library_root() -> Path:
    """媒体库根目录默认值:安装目录下的 animelibrary 文件夹。"""
    return get_install_dir() / "animelibrary"
