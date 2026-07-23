import re

from pydantic import BaseModel, field_validator
from typing import Optional
from datetime import datetime


def validate_proxy_url_value(value: str) -> str:
    """
    格式不对的代理地址(比如漏了http://前缀)传给httpx.AsyncClient(proxy=...)会
    抛出httpx.InvalidURL——这个异常不是httpx.HTTPError的子类,不会被各处client
    构造代码里"except httpx.HTTPError"捕获,会变成裸的500错误,还看不出跟代理有关。
    在保存(SettingsUpdate)和测试(ProxyTestRequest)两个入口都校验,比事后到处防御更可靠。
    """
    value = value.strip()
    if not value:
        return value
    if not re.match(r"^(https?|socks5)://", value, re.IGNORECASE):
        raise ValueError("代理地址需要以 http://、https:// 或 socks5:// 开头")
    return value

class AnimeCreate(BaseModel):
    title: str
    title_original: Optional[str] = None
    summary: Optional[str] = None
    cover_url: Optional[str] = None
    air_date: Optional[str] = None
    total_eps: Optional[int] = None


class AnimeResponse(AnimeCreate):
    id: int
    status: str

    class Config:
        from_attributes = True

class RenamePreviewRequest(BaseModel):
    anime_title: str  # 兜底用:拿不到bgm_id对应的官方名时,使用这个
    bgm_id: Optional[int] = None  # 有值时,优先用Bangumi官方中文名做文件夹/文件名
    titles: list[str]


class PrefetchRenameCacheRequest(BaseModel):
    """下载页一打开(带着bgm_id)就调用,让家族改名规则在后台提前算好,
    不用等用户真正点预览才现算。"""
    anime_title: str
    bgm_id: Optional[int] = None


class DownloadItem(BaseModel):
    title: str
    magnet: str
    fansub_name: Optional[str] = None


class DownloadRequest(BaseModel):
    anime_title: str
    bgm_id: Optional[int] = None
    keyword: str
    source: str  # "dmhy"/"animegarden"/"nyaa",画面强制单选,不再提供"不限"/自动切换
    fansub_name: Optional[str] = None
    quality: Optional[str] = None
    subtitle: Optional[str] = None
    format: Optional[str] = None
    release_type: Optional[str] = None
    subscribe: bool = False
    auto_rename: bool = True
    items: list[DownloadItem]

class SettingsUpdate(BaseModel):
    download_root: str
    library_root: str
    potplayer_path: str
    rename_poll_interval_seconds: int = 300
    player_mode: str = "external"  # external / builtin
    default_source: str = "dmhy"  # 下载页默认选中的数据源: dmhy / animegarden / nyaa
    proxy_url: str = ""  # 访问外部动漫资源站时用的代理地址,留空=直连

    @field_validator("proxy_url")
    @classmethod
    def validate_proxy_url(cls, value: str) -> str:
        return validate_proxy_url_value(value)


class ProxyTestRequest(BaseModel):
    """设置页"测试代理"按钮:测的是输入框里当前的值,不需要先保存。
    留空代表没有手填配置,后端会回落到探测出来的系统代理(跟实际运行时的行为一致)。
    """
    proxy_url: str = ""

    @field_validator("proxy_url")
    @classmethod
    def validate_proxy_url(cls, value: str) -> str:
        return validate_proxy_url_value(value)

class RssSubscriptionResponse(BaseModel):
    """RSS订阅一览页用的单条记录。"""

    id: int
    anime_title: str
    bgm_id: Optional[int] = None
    keyword: str
    source: str
    fansub_name: Optional[str] = None
    quality: Optional[str] = None
    subtitle: Optional[str] = None
    format: Optional[str] = None
    release_type: Optional[str] = None
    auto_rename: bool
    enabled: bool
    rss_url: Optional[str] = None
    last_error: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class RssToggleRequest(BaseModel):
    enabled: bool


class BatchDeleteTasksRequest(BaseModel):
    hashes: list[str]


class QbitTestRequest(BaseModel):
    host: str
    port: int
    username: str
    password: str


class QbitApplyRequest(QbitTestRequest):
    apply_recommended: bool = True