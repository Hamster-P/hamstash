from sqlalchemy import Column, Integer, String, DateTime, ForeignKey
from sqlalchemy.sql import func
from database import Base
from sqlalchemy import Boolean
from datetime import datetime

class AnimeCatalog(Base):
    __tablename__ = "anime_catalog"

    id = Column(Integer, primary_key=True, index=True)
    bgm_id = Column(Integer, unique=True, index=True, nullable=True)
    title = Column(String, nullable=False)
    title_original = Column(String, nullable=True)
    summary = Column(String, nullable=True)
    cover_url = Column(String, nullable=True)
    air_date = Column(String, nullable=True)
    total_eps = Column(Integer, nullable=True)
    status = Column(String, default="未标记")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    total_episodes = Column(Integer, default=0, nullable=True)

class SubscriptionRule(Base):
    """RSS订阅规则:某部番+关键词+字幕组组合,长期生效,持续自动下载新种子。"""

    __tablename__ = "subscription_rule"

    id = Column(Integer, primary_key=True, index=True)
    anime_title = Column(String, nullable=False)
    bgm_id = Column(Integer, nullable=True)
    keyword = Column(String, nullable=False)  # 完整检索关键词,后续轮询用它去搜新种子
    source = Column(String, nullable=False, default="dmhy")  # 这条订阅的RSS来自dmhy、animegarden还是nyaa
    fansub_name = Column(String, nullable=True)  # 为空代表不限字幕组
    quality = Column(String, nullable=True)  # 1080p / 720p,为空代表不限
    subtitle = Column(String, nullable=True)  # 简体/繁体/简日双语/繁日双语,为空代表不限
    format = Column(String, nullable=True)  # MKV/MP4,为空代表不限
    release_type = Column(String, nullable=True)  # 单集(追更)/合集全集(完结),为空代表不限
    auto_rename = Column(Boolean, default=True)
    enabled = Column(Boolean, default=True)  # RSS开关:是否应该在qBittorrent里保持激活状态
    rss_url = Column(String, nullable=True)  # 提交给qBittorrent的RSS订阅源地址(dmhy关键词RSS)
    rss_path = Column(String, nullable=True)  # 该RSS在qBittorrent订阅目录树里的路径,删除时需要
    rule_name = Column(String, nullable=True)  # 对应qBittorrent"自动下载规则"的名字
    last_error = Column(String, nullable=True)  # 最近一次激活/关闭qBittorrent RSS时的报错,给一览页展示
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class DownloadTask(Base):
    """每一次实际推送给qBittorrent的下载任务记录。"""

    __tablename__ = "download_task"

    id = Column(Integer, primary_key=True, index=True)
    subscription_id = Column(Integer, nullable=True)  # 为空代表是单次下载,不关联订阅
    anime_title = Column(String, nullable=False)
    original_title = Column(String, nullable=False)  # 种子原始标题
    magnet = Column(String, nullable=False)
    fansub_name = Column(String, nullable=True)
    target_full_path = Column(String, nullable=True)  # 预期改名后的完整路径(预览值)
    status = Column(String, default="已推送")  # 已推送/下载中/已完成/已改名
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class AppSetting(Base):
    """简单的键值对配置表,用于存放可以在设置页里修改的运行时配置。"""

    __tablename__ = "app_setting"

    key = Column(String, primary_key=True)
    value = Column(String, nullable=True)



class AnimeFolder(Base):
    """
    暂存区文件夹 <-> 番名/bgm_id 的对照表。
    手动下载、RSS订阅在往qBittorrent推送任务时,存放位置统一设成
    download_root下这部番专属的暂存子文件夹,同时在这里写一条记录;
    后台轮询任务扫到这个暂存文件夹下载完成后,靠这张表反查该按哪个
    番名/bgm_id把文件整理进library_root。
    """
    __tablename__ = "anime_folder"

    id = Column(Integer, primary_key=True, index=True)
    staging_folder = Column(String, unique=True, nullable=False, index=True)  # download_root下的暂存子目录
    anime_title = Column(String, nullable=False)
    main_bgm_id = Column(Integer, nullable=True)   # 系列根ID,决定文件夹归属
    season_bgm_id = Column(Integer, nullable=True)  # 这一次提交时的季度专属ID,决定季度文字判断+集数偏移量
    auto_rename = Column(Boolean, default=True)  # False时后台整理任务只搬家不改名,原样保留目录结构(应对BD/合集光盘)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class RenamedFile(Base):
    """
    记录种子内部每一个视频文件各自的整理处理状态。
    以文件为最小单位,天然支持"合集里部分文件处理成功、部分失败"——
    失败的文件只会停留在番剧根目录(setLocation是种子级别的,没法让失败文件
    留在暂存区不动),但不会被塞进错误的Season/Other子路径,方便人工事后核查。
    """
    __tablename__ = "renamed_file"

    id = Column(Integer, primary_key=True, index=True)
    torrent_hash = Column(String, nullable=False, index=True)
    original_path = Column(String, nullable=False)
    status = Column(String, default="pending")  # pending/done/failed
    release_version = Column(Integer, default=1)
    target_full_path = Column(String, nullable=True)
    error = Column(String, nullable=True)
    processed_at = Column(DateTime(timezone=True), server_default=func.now())


class LocalMedia(Base):
    """
    本地媒体库扫描关联表。
    用于将 D:\AnimeLibrary 下的实体文件夹，与 Bangumi（AnimeCatalog）建立绑定。
    """
    __tablename__ = "local_media"

    id = Column(Integer, primary_key=True, index=True)
    folder_name = Column(String, unique=True, nullable=False, index=True)  # 比如 "葬送的芙莉莲"
    bgm_id = Column(Integer, ForeignKey("anime_catalog.bgm_id"), nullable=True)  # 关联已有的动漫元数据
    last_scanned_at = Column(DateTime(timezone=True), onupdate=func.now(), server_default=func.now())
    latest_activity_at = Column(DateTime, nullable=True)  # 文件夹自身+各Season子目录mtime的最大值,供列表页"最新更新"排序用

class PlaybackRecord(Base):
    """极其轻量的已播放记录表"""
    __tablename__ = "playback_record"

    id = Column(Integer, primary_key=True, index=True)
    folder_name = Column(String, index=True)  # 动漫文件夹名
    filename = Column(String, index=True)     # 视频文件名，如 S04E01.mkv
    watched_at = Column(DateTime, default=datetime.now)  # 观看时间


class AnimeFamilyCache(Base):
    """
    bangumi_family.resolve_family_season_map()算出来的"某个Bangumi关联家族里,
    每个成员该归到第几季/哪个顶层桶"结果的持久化缓存——这个计算要BFS遍历整个
    关联图谱+批量查详情,柯南这种长篇实测60+个节点、十几秒,但家族结构变动率
    很低(一部番一年也就新增1~2个成员),命中缓存直接读数据库,不用每次整理
    都重新摸一遍Bangumi API。

    按bgm_id(单个具体条目)查询,不是按source_bgm_id(家族根)查询——命中判断是
    "这个具体条目有没有被算过",没算过(可能是全新番,也可能是老系列新出的
    剧场版/新一季)才触发一次全量重算,重算时把整个家族一起写回,不是只写这一条。
    """
    __tablename__ = "anime_family_cache"

    id = Column(Integer, primary_key=True, index=True)
    source_bgm_id = Column(Integer, nullable=False, index=True)  # 家族根,即main_bgm_id
    bgm_id = Column(Integer, unique=True, nullable=False, index=True)  # 家族里的具体某个条目
    name = Column(String, nullable=False)
    date = Column(String, nullable=True)  # 首播日期,Bangumi原始字符串,未定档的可能是空
    platform = Column(String, nullable=True)
    eps = Column(Integer, nullable=True)  # Bangumi原始eps字段,连载中的番这里通常是0
    total_episodes = Column(Integer, nullable=True)  # 集数判断实际用这个(为0则退回eps)
    season_ordinal = Column(String, nullable=True)  # "01"/"02"/...,None代表不是真季候选
    folder_bucket = Column(String, nullable=True)  # 顶层桶,仅供人工查表时直接可读,不参与改名逻辑判断
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), server_default=func.now())
