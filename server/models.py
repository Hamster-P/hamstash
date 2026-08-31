from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey
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
    main_bgm_id = Column(Integer, nullable=True)  # 创建订阅时解析出的系列根ID,后续轮询直接复用,不重新查Bangumi关联关系
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
    last_polled_at = Column(DateTime(timezone=True), nullable=True)  # 上次尝试轮询这条订阅的时间(不管成不成功),给一览页展示


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
    target_full_path = Column(String, nullable=True)  # 已弃用,历史遗留列,新代码不再写入/读取(见target_relative_path)
    target_relative_path = Column(String, nullable=True)  # 相对于library_root的路径,不含盘符前缀——
    # library_root搬到别的盘/目录时(迁移场景database.py::_resolve_db_path会自动把DB文件也搬过去)
    # 现读现拼library_root+这个字段就能拿到当前有效的绝对路径,不会像target_full_path那样
    # 因为焊死了旧盘符导致版本冲突判断(get_current_version_at_target)对不上历史记录。
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
    # 作为这部番封面用的家族成员bgm_id:NULL=尚未按默认策略解析,回退到上面的bgm_id自身的图。
    # 手动"选择图片"或后台默认策略(最新TV季/第一季)都写这一列。
    cover_bgm_id = Column(Integer, nullable=True)
    # 用户是否手动选过封面:True时不被默认策略自动覆盖(策略变更只清cover_is_custom=False的行)。
    cover_is_custom = Column(Boolean, default=False, nullable=False)
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
    # 集数判断优先用eps,为0/空时才退回total_episodes(见services/bgm_series_cache.py::
    # _member_eps)——实测total_episodes会把特典/SP也算进去、比实际正片偏大(咒术回战
    # 第一季eps=24但total_episodes=25、实际正片就是24集),拿它算跨季集数偏移量会算错。
    # 反过来eps对连载中的番经常是0,所以两个字段都得留着互相兜底。
    eps = Column(Integer, nullable=True)  # Bangumi原始eps字段,连载中的番这里通常是0
    total_episodes = Column(Integer, nullable=True)  # eps为0时的兜底集数
    season_ordinal = Column(String, nullable=True)  # "01"/"02"/...,None代表不是真季候选
    folder_bucket = Column(String, nullable=True)  # 顶层桶,仅供人工查表时直接可读,不参与改名逻辑判断
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), server_default=func.now())


class RelatedAnimeCache(Base):
    """
    影视库详情页"补番"一览(GET /bangumi/related)的整份响应缓存,按家族根 bgm_id 存。

    anime_family_cache 只缓存了"家族成员结构 + 季度序号",没有封面/评分这些展示字段,
    所以补番接口每次都要对家族每个成员发一轮 relations(核实有没有新续作) + 一轮
    subject 详情(拿封面/评分),共 2N 次经代理的 bgm.tv 往返,大系列(柯南 60+ 成员)
    要十几秒。这张表把算好的整份结果(含封面/评分)落库,TTL 内直接返回、零联网。

    失效:超 TTL(见 routers/search.py::RELATED_CACHE_TTL) / 家族被 _persist_family_map
    重写 / 季度算法版本变更 / 修复媒体库清缓存——后三者由对应流程主动删行,详见
    services/bgm_series_cache.py 与 services/library_repair.py。
    """
    __tablename__ = "related_anime_cache"

    id = Column(Integer, primary_key=True, index=True)
    root_bgm_id = Column(Integer, unique=True, nullable=False, index=True)  # 家族根,即 AnimeFamilyCache.source_bgm_id
    payload = Column(Text, nullable=False)  # json.dumps(results 列表),结构同 /bangumi/search 的条目
    checked_at = Column(DateTime(timezone=True), server_default=func.now())


class RssMatchedItem(Base):
    """
    RSS 引擎(services/rss_poller.py)每一轮轮询处理过的文章记录。
    双重作用:1) 去重——同一篇文章(按guid)不会被重复判断/重复下载;
    2) 给订阅一览页的"命中记录"展示提供数据源。
    """
    __tablename__ = "rss_matched_item"

    id = Column(Integer, primary_key=True, index=True)
    subscription_id = Column(Integer, ForeignKey("subscription_rule.id"), nullable=False, index=True)
    guid = Column(String, nullable=False, index=True)  # RSS文章的guid/详情页链接,同一订阅内天然唯一
    info_hash = Column(String, nullable=True)  # 种子info hash,nyaa需要靠这个自己拼磁力链接
    title = Column(String, nullable=False)
    magnet = Column(String, nullable=True)
    download_status = Column(String, nullable=False)  # added / failed / skipped
    error = Column(String, nullable=True)
    matched_at = Column(DateTime(timezone=True), server_default=func.now())


class StandaloneMedia(Base):
    """要在影视库"剧场版模式"里单独成卡展示的剧场版/OVA 文件登记表。
    表驱动、逐文件一行——不扫盘、不从路径自动判断谁是剧场版。两条写入路径:
    1) 用户在系列详情分集里手动追加(source="manual");
    2) 下载整理时识别到 movie/OVA 的文件自动追加(source="download",bgm_id 取下载时选的条目)。
    展示时前端按 bgm_id 分组成卡(一部剧场版1集/OVA多集),点进迷你详情列各集。

    rel_path 一律存正斜杠、与 routers/library.py::scan_local_folder_structure 的 to_rel 同格式
    (自动加时 RenamedFile.target_relative_path 是反斜杠,写表前要转正斜杠),否则卡片会跟
    磁盘/系列分集对不上、一直 missing。
    """
    __tablename__ = "standalone_media"

    id = Column(Integer, primary_key=True, index=True)
    library_folder = Column(String, nullable=False, index=True)  # 文件所属库文件夹名(LocalMedia.folder_name)
    rel_path = Column(String, unique=True, nullable=False, index=True)  # 相对library_root的正斜杠路径,去重键
    filename = Column(String, nullable=False)
    bgm_id = Column(Integer, nullable=False, index=True)  # 作为封面/标题/简介的剧场版/OVA条目,也是分组键
    media_type = Column(String, nullable=True)  # "movie" / "ova"
    source = Column(String, nullable=False, default="manual")  # "manual" / "download"
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AnimeOriginCache(Base):
    """
    bgm_id -> 是否日本产地的持久化缓存,产地数据来自Bangumi条目的meta_tags字段
    (里面会标"日本"/"中国"/"韩国"/"美国"这类产地标签)。追更页的日历接口本身不带
    meta_tags,只能对每个条目额外查一次详情才能拿到,但产地信息基本终身不变
    (不会出现"这部番今天是日本产、明天变成别的产地"的情况),所以查过一次的
    bgm_id直接读这张表就行,不用每次都重新请求——跟AnimeFamilyCache是同一种
    "低变动率数据没必要重复查"的设计。
    """
    __tablename__ = "anime_origin_cache"

    bgm_id = Column(Integer, primary_key=True)
    is_japanese = Column(Boolean, nullable=False)
    meta_tags = Column(String, nullable=True)  # 逗号分隔的原始tags,留着排查/以后可能要用其他产地判断
    cached_at = Column(DateTime(timezone=True), server_default=func.now())


class MediaGroupOverride(Base):
    """用户对"这部作品归到哪个家族"的手动覆盖。

    Bangumi关联图谱算出来的家族归属不总是符合使用习惯:高达UC家族46个成员会全部
    塞进同一个文件夹,而0083/∀高达/独角兽这些"同世界观但各自独立的新作"被当成
    同一部的不同季管理并不合理。这张表让自动判定成为"默认值"而不是"唯一答案":
    root_bgm_id指向这一部该归到的家族根,root_bgm_id == bgm_id 表示"独立成一部",
    不并进任何家族、在媒体库里自己一个顶层文件夹、自己一张卡。

    没有对应行 = 完全沿用现有自动判定,所以历史库升级后行为逐字节不变;
    表本身由database.Base.metadata.create_all自动建出(见db_migrate.py),不需要写迁移。

    刻意独立成表、不塞进AnimeFamilyCache:那张表是纯缓存,
    services/library_repair.py::reset_season_cache每次"修复媒体库"扫描都会整表清空,
    用户的手动决定放进去会被静默抹掉。
    """
    __tablename__ = "media_group_override"

    bgm_id = Column(Integer, primary_key=True)  # 被覆盖归属的那个具体条目
    root_bgm_id = Column(Integer, nullable=False, index=True)  # 归到哪个家族根;==bgm_id即独立成一部
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
