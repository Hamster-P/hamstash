import os
import re

from pydantic import BaseModel, field_validator, model_validator
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
    # 跟DownloadRequest同名字段保持一致,让预览显示的目标路径跟提交后真正落地的一致。
    merge_to_family: bool = True


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
    # 是否并进这部作品所属的Bangumi家族(高达/柯南这类系列的最早那个条目)。
    # True(默认,也是历史行为)=同系列共用一个媒体库文件夹、按第几季组织;
    # False=这一部独立成一部,在媒体库单独成卡。默认值保证老客户端不带这个字段时
    # 行为完全不变。实际落地靠写一条MediaGroupOverride,见models.py该表的说明。
    merge_to_family: bool = True
    items: list[DownloadItem]

class SettingsUpdate(BaseModel):
    download_root: str
    library_root: str
    potplayer_path: str
    rename_poll_interval_seconds: int = 300
    rss_poll_interval_seconds: int = 1800  # RSS引擎自动轮询间隔,默认30分钟
    player_mode: str = "external"  # external / builtin
    default_source: str = "dmhy"  # 下载页默认选中的数据源: dmhy / animegarden / nyaa
    default_home_view: str = "tracking"  # 软件启动时默认显示的页面: tracking/search/library
    proxy_url: str = ""  # 访问外部动漫资源站时用的代理地址,留空=直连
    # 下载源覆盖配置(JSON 字符串,见 config_store.DEFAULTS['download_sources']),留空=全用默认
    download_sources: str = ""
    # 媒体库默认封面策略: latest_tv / first_season / matched(见 config_store.DEFAULTS)
    library_cover_strategy: str = "latest_tv"

    @field_validator("proxy_url")
    @classmethod
    def validate_proxy_url(cls, value: str) -> str:
        return validate_proxy_url_value(value)

    @field_validator("download_sources")
    @classmethod
    def validate_download_sources(cls, value: str) -> str:
        """必须是合法 JSON 对象(或空串)。存原始字符串,读取时按 source 取覆盖项。
        并且强制:至少要有一个源处于启用状态——全部停用会让下载页/RSS 订阅无源可用。"""
        import json
        if not value.strip():
            return ""  # 空串=全用默认,三个源默认都启用,天然满足"至少一个"
        try:
            data = json.loads(value)
        except (ValueError, TypeError):
            raise ValueError("download_sources 必须是合法的 JSON")
        if not isinstance(data, dict):
            raise ValueError("download_sources 必须是 JSON 对象")
        # 计算生效后的启用集合:注册表里的每个源,覆盖里显式写了 enabled 就用它,否则默认启用
        from sources.registry import all_sources
        any_enabled = False
        for adapter in all_sources():
            entry = data.get(adapter.id)
            enabled = entry.get("enabled", True) if isinstance(entry, dict) else True
            if enabled:
                any_enabled = True
                break
        if not any_enabled:
            raise ValueError("至少需要启用一个下载源")
        return value

    @field_validator("default_home_view")
    @classmethod
    def validate_default_home_view(cls, value: str) -> str:
        allowed = {"tracking", "search", "library"}
        if value not in allowed:
            raise ValueError(f"default_home_view 必须是 {sorted(allowed)} 之一")
        return value

    @model_validator(mode="after")
    def validate_roots_distinct(self):
        """下载暂存目录和媒体库根目录不能是同一个文件夹——整理任务会把下载暂存
        目录当作"待搬运的临时区"扫描处理,两者重合会把媒体库里已经归档好的文件
        当成新下载的东西重新扫描/移动,数据直接错乱。用normcase+normpath归一化
        再比较,不然"D:/Anime"和"d:/anime/"这种大小写/末尾斜杠差异会被误判成不同。
        """
        left = os.path.normcase(os.path.normpath(self.download_root))
        right = os.path.normcase(os.path.normpath(self.library_root))
        if left == right:
            raise ValueError("下载暂存目录和媒体库根目录不能设置成同一个文件夹")
        return self


class ProxyTestRequest(BaseModel):
    """设置页"测试代理"按钮:测的是输入框里当前的值,不需要先保存。
    留空代表没有手填配置,后端会回落到探测出来的系统代理(跟实际运行时的行为一致)。
    """
    proxy_url: str = ""
    # 要探测的下载源 id 列表(设置页当前勾选启用的那些,可能还没保存)。
    # None=没传,后端回落到"当前已启用的源";空列表=一个源都不探(只探 Bangumi 相关)。
    sources: Optional[list[str]] = None

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
    last_polled_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class RssToggleRequest(BaseModel):
    enabled: bool


class RssMatchedItemResponse(BaseModel):
    """订阅一览页"命中记录"列表的单条记录。"""

    id: int
    guid: str
    title: str
    magnet: Optional[str] = None
    download_status: str
    error: Optional[str] = None
    matched_at: datetime

    class Config:
        from_attributes = True


class BatchDeleteTasksRequest(BaseModel):
    hashes: list[str]


class QbitTestRequest(BaseModel):
    host: str
    port: int
    username: str
    password: str


class QbitApplyRequest(QbitTestRequest):
    apply_recommended: bool = True