import asyncio
import httpx
import json
import os

import config_store
from database import SessionLocal
from services.common import get_setting

_cached_cookie: str | None = None
_cached_base_url: str | None = None
_cookie_lock = asyncio.Lock()


def _get_qbit_settings() -> tuple[str, int, str, str]:
    """从数据库读取qBittorrent连接信息(host/port/账号/密码)。
    开一个短生命周期的SessionLocal,不依赖FastAPI的请求级session注入
    (参考services/organize.py里后台任务读设置的用法)。"""
    db = SessionLocal()
    try:
        host = get_setting(db, "qbit_host", config_store.DEFAULTS["qbit_host"])
        port = int(get_setting(db, "qbit_port", config_store.DEFAULTS["qbit_port"]))
        username = get_setting(db, "qbit_username", config_store.DEFAULTS["qbit_username"])
        password = get_setting(db, "qbit_password", config_store.DEFAULTS["qbit_password"])
        return host, port, username, password
    finally:
        db.close()


async def login_with(host: str, port: int, username: str, password: str) -> str:
    """用指定的凭据登录一次,返回session cookie字符串。不读/不写缓存——
    给引导向导"保存前先测试这份待保存的凭据"用,不影响当前已保存/已缓存的那份。"""
    base_url = f"http://{host}:{port}"
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{base_url}/api/v2/auth/login",
            data={"username": username, "password": password},
        )
        resp.raise_for_status()

        # 新版本qBittorrent登录成功返回204空内容,
        # 旧版本返回200+文本"Ok.",这里两种都视为成功。
        if resp.status_code not in (200, 204):
            raise RuntimeError(f"qBittorrent登录失败,状态码: {resp.status_code}")

        # cookie名字可能是 SID,也可能是带端口号后缀的 QBT_SID_{port},
        # 遍历找第一个包含"SID"字样的cookie,不写死具体名字。
        cookie_name = None
        cookie_value = None
        for name, value in resp.cookies.items():
            if "SID" in name.upper():
                cookie_name = name
                cookie_value = value
                break

        if not cookie_value:
            raise RuntimeError("未能获取qBittorrent登录会话(用户名密码错误,或WebUI未启用)")

        return f"{cookie_name}={cookie_value}"


async def _login() -> tuple[str, str]:
    """用数据库里当前保存的配置登录一次,返回(cookie, base_url)。"""
    host, port, username, password = _get_qbit_settings()
    cookie = await login_with(host, port, username, password)
    return cookie, f"http://{host}:{port}"


def invalidate_session() -> None:
    """丢弃缓存的session,下次调用强制用最新保存的配置重新登录
    (保存了新的连接信息之后调用一下,避免继续用旧凭据的缓存)。"""
    global _cached_cookie, _cached_base_url
    _cached_cookie = None
    _cached_base_url = None


async def _get_session(force_refresh: bool = False) -> tuple[str, str]:
    """返回缓存的(cookie, base_url),没有缓存或强制刷新时才真正重新登录。
    避免每次API调用都重新登录一次——之前那样高频调用容易触发qBittorrent自带的防爆破锁定。"""
    global _cached_cookie, _cached_base_url
    async with _cookie_lock:
        if _cached_cookie is None or force_refresh:
            _cached_cookie, _cached_base_url = await _login()
        return _cached_cookie, _cached_base_url


async def _request(method: str, path: str, **kwargs) -> httpx.Response:
    """统一的请求入口:用缓存的session发请求;响应403(session过期/失效)时
    强制刷新一次再重试一次。其余状态码原样把Response返回给调用方自行处理
    (比如很多接口把409/404当成功忽略)。"""
    cookie, base_url = await _get_session()
    async with httpx.AsyncClient(headers={"Cookie": cookie}) as client:
        resp = await client.request(method, f"{base_url}{path}", **kwargs)

    if resp.status_code == 403:
        cookie, base_url = await _get_session(force_refresh=True)
        async with httpx.AsyncClient(headers={"Cookie": cookie}) as client:
            resp = await client.request(method, f"{base_url}{path}", **kwargs)

    return resp


async def get_preferences(base_url: str, cookie: str) -> dict:
    """查询当前qBittorrent WebUI偏好设置,给引导向导展示现状用(RSS处理开没开等)。"""
    async with httpx.AsyncClient(headers={"Cookie": cookie}) as client:
        resp = await client.get(f"{base_url}/api/v2/app/preferences")
        resp.raise_for_status()
        return resp.json()


async def set_preferences(base_url: str, cookie: str, prefs: dict) -> None:
    """修改qBittorrent WebUI偏好设置,给引导向导"应用推荐设置"用。"""
    async with httpx.AsyncClient(headers={"Cookie": cookie}) as client:
        resp = await client.post(
            f"{base_url}/api/v2/app/setPreferences",
            data={"json": json.dumps(prefs)},
        )
        resp.raise_for_status()


async def add_torrent(magnet: str, save_path: str | None = None, category: str = "anime-hub"):
    """添加一个磁力链下载任务（强制使用 multipart/form-data 格式）。"""
    await ensure_category(category)

    # 构建基础字典
    form_dict = {
        "urls": magnet,
        "category": category
    }

    if save_path:
        # 兼容处理：依然把路径统一转化为正斜杠，防止转义灾难
        clean_path = str(save_path).replace("\\", "/").rstrip("/")
        form_dict["savepath"] = clean_path
        form_dict["autoTMM"] = "false"

    # 核心修改：利用 (None, str_val) 语法将所有表单项转化为 multipart 字段提交
    multipart_fields = {
        key: (None, str(value)) for key, value in form_dict.items()
    }

    # 注意这里用 files= 而不是 data=
    resp = await _request("post", "/api/v2/torrents/add", files=multipart_fields)
    resp.raise_for_status()
    return resp.text

async def ensure_category(name: str, save_path: str = "") -> None:
    """
    确保qBittorrent里存在这个分类,不存在就创建。
    add_torrent的category参数如果指向一个不存在的分类,qBittorrent会静默忽略,
    种子变成"未分类"——这个函数就是为了在推送前把这个隐患堵上。
    """
    resp = await _request(
        "post", "/api/v2/torrents/createCategory", data={"category": name, "savePath": save_path}
    )
    if resp.status_code == 409:
        return  # 已存在,忽略
    resp.raise_for_status()



async def _fetch_torrents_info(params: dict) -> list[dict]:
    """查询 /torrents/info 的公共逻辑,按传入的params过滤条件不同,给各个上层查询函数复用。"""
    resp = await _request("get", "/api/v2/torrents/info", params=params)
    resp.raise_for_status()
    return resp.json()


async def get_completed_torrents(category: str, exclude_tag: str) -> list[dict]:
    """列出某分类下已完成、且还没打上exclude_tag标签的种子。"""
    torrents = await _fetch_torrents_info({"category": category, "filter": "completed"})
    return [
        t for t in torrents
        if exclude_tag not in [tag.strip() for tag in (t.get("tags") or "").split(",")]
    ]


async def get_category_torrents(category: str) -> list[dict]:
    """列出某分类下的全部种子(不筛选完成状态),用于下载详情页展示。"""
    return await _fetch_torrents_info({"category": category})


async def get_torrent_files(torrent_hash: str) -> list[dict]:
    """列出某个种子内的文件明细(名字/大小/进度),给下载详情页点开某行展开文件列表用。"""
    resp = await _request("get", "/api/v2/torrents/files", params={"hash": torrent_hash})
    resp.raise_for_status()
    return resp.json()


async def get_torrents_by_save_path(save_path: str, category: str = "anime-hub") -> list[dict]:
    """列出当前save_path等于指定路径的全部种子(不筛选完成状态)。

    暂存目录是按(下载暂存根,番名,bgm_id)算出来的共享路径,同一部番的多个种子
    (还在下载中的、RSS陆续投递进来的)可能同时占着同一个save_path——services/
    organize.py清理空暂存目录前,要靠这个函数确认"是不是真的一个种子都不剩了",
    不能只看已经处理完这一个种子的状态。用normcase+normpath归一化路径再比较,
    跟schemas.py::SettingsUpdate.validate_roots_distinct是同一套比较标准
    (大小写/末尾斜杠不敏感)。
    """
    torrents = await _fetch_torrents_info({"category": category})
    target = os.path.normcase(os.path.normpath(save_path))
    return [
        t for t in torrents
        if os.path.normcase(os.path.normpath(t.get("save_path") or "")) == target
    ]


async def get_torrents_by_hashes(hashes: list[str]) -> list[dict]:
    """按hash批量查询种子当前信息(主要用来读tags,判断删除时要不要带上文件)。"""
    if not hashes:
        return []
    return await _fetch_torrents_info({"hashes": "|".join(hashes)})


async def get_torrent_files(torrent_hash: str) -> list[dict]:
    """查询某个种子内部的文件清单(每个文件的相对路径、大小等)。"""
    resp = await _request("get", "/api/v2/torrents/files", params={"hash": torrent_hash})
    resp.raise_for_status()
    return resp.json()


async def rename_torrent_file(torrent_hash: str, old_path: str, new_path: str) -> None:
    """
    重命名/挪动种子内部某个文件。new_path是相对种子保存位置的相对路径,
    可以带子目录(如"Season 01\\xxx.mkv"),qBittorrent会自动在保存位置下建好对应的文件夹。
    """
    resp = await _request(
        "post",
        "/api/v2/torrents/renameFile",
        data={"hash": torrent_hash, "oldPath": old_path, "newPath": new_path},
    )
    resp.raise_for_status()


async def set_torrent_location(torrent_hash: str, location: str) -> None:
    """
    设置整个种子的保存位置(是种子级别的,不能按文件分别设置)。
    我们的策略是把它设成"这部番的公共根目录",再靠renameFile的相对路径
    去实现Season/Other这些子目录分类。
    """
    resp = await _request(
        "post", "/api/v2/torrents/setLocation", data={"hashes": torrent_hash, "location": location}
    )
    resp.raise_for_status()


async def add_torrent_tags(torrent_hash: str, tags: str) -> None:
    """给种子打标签,用于标记"整理已完成",避免下次轮询重复处理。"""
    resp = await _request(
        "post", "/api/v2/torrents/addTags", data={"hashes": torrent_hash, "tags": tags}
    )
    resp.raise_for_status()

async def recheck_torrent(torrent_hash: str) -> None:
    """重新校验种子磁盘文件,配合resume用于从error/missingFiles状态恢复
    (比如网络共享暂时断开又恢复后,qBittorrent以为文件丢了)。"""
    resp = await _request("post", "/api/v2/torrents/recheck", data={"hashes": torrent_hash})
    resp.raise_for_status()

async def resume_torrent(torrent_hash: str) -> None:
    """恢复/继续一个种子。"""
    resp = await _request("post", "/api/v2/torrents/resume", data={"hashes": torrent_hash})
    resp.raise_for_status()

async def delete_torrent(torrent_hash: str, delete_files: bool = True) -> None:
    """删除一个种子,delete_files=True时连同它已下载的文件一起删除磁盘空间。"""
    resp = await _request(
        "post",
        "/api/v2/torrents/delete",
        data={"hashes": torrent_hash, "deleteFiles": str(delete_files).lower()},
    )
    resp.raise_for_status()

async def test_connection() -> bool:
    """测试能否正常登录,给一个健康检查接口用。不走缓存,每次都真正尝试登录一次,
    确保反映的是当下的真实连接状态。"""
    try:
        await _login()
        return True
    except Exception:
        return False
