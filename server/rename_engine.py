import re
import anitopy

# 关键词定义
SUBTITLE_EXTS = {"ass", "srt", "ssa", "vtt", "sup"}
VIDEO_EXTS = {"mkv", "mp4", "ts", "avi", "flv", "mov", "wmv", "m2ts", "m2t", "webm", "rmvb", "m4v"}
MOVIE_MARKERS = ["剧场版", "劇場版", "movie", "gekijouban"]
# 柯南/哆啦A梦这类长篇剧场版系列,字幕组常用"[M28]"这种方括号包住的数字编号
# 标记"剧场版第28部",不含"movie"/"剧场版"这类文本关键词——要求方括号包住,
# 避免跟普通单词里偶然出现的"m+数字"片段(比如某些编码标签)混淆。
_MOVIE_NUMBER_TAG = re.compile(r"\[m\d{1,3}\]", re.IGNORECASE)
OVA_MARKERS = ["ova", "oad", "特典", "特别篇", "番外篇", "sp", "总集篇", "回顾篇", ".5", "激活解说"]
EXTRA_MARKERS = ["op", "ed", "ncop", "nced", "opening", "ending", "pv", "preview", "预告",
                  "menu", "cm", "sample", "logo", "credit", "trailer", "teaser",
                  "interview", "spot", "bonus", "tokuten"]
# EXTRA_MARKERS里的短标记(op/ed/pv/cm等)朴素子串匹配容易误伤普通单词内部的字母组合
# (比如"Poppin'Dream"含有"op"),改用单词边界的正则;数字后缀0~2位是为了兼容
# "OP1"/"ED2"这类多首插曲编号的字幕组命名习惯。
# tokuten是"特典"的罗马字写法——BD花絮盘经常只在文件名里写罗马字,中文"特典"
# 关键词(见OVA_MARKERS)只出现在种子内的父目录名里,传到这里的裸文件名根本看不到,
# 漏了这个词会导致特典视频被当成正片走S03Exx编号,详见rename_engine相关bug记录。
# preview:VCB/Nekomoe这类合集包固定用"[Preview01]..[PreviewNN]"放每集的下集预告片,
# 漏了它会把30秒预告片当成正片改成SxxEyy(实测『想要成为影之实力者!』合集包)。
_EXTRA_PATTERN = re.compile(
    r"(?<![a-z0-9])(op|ed|ncop|nced|opening|ending|pv|preview|menu|cm|sample|logo|credit|"
    r"trailer|teaser|interview|spot|bonus|tokuten)\d{0,2}(?![a-z0-9])|预告",
    re.IGNORECASE,
)
# 合集包里"[SP01]..[SPNN]"这种"方括号包裹+序号"的基本都是特典短片(菜单/CM/预告集锦),
# 不是独立OVA条目。单独的"SP"(无方括号,如"某番 SP.mkv")仍走OVA_MARKERS的OVA分支不变。
_BRACKET_SP_TAG = re.compile(r"\[sp\d+\]", re.IGNORECASE)

def classify_media_type(torrent_title: str, platform: str | None = None) -> str:
    """
    platform是Bangumi官方给这个条目标注的类型(TV/OVA/剧场版/WEB/其他),拿得到时
    优先信它——种子标题里的关键词是字幕组自己写的,不一定靠谱(比如短篇正片
    不会主动在标题里写"OVA"/"剧场版"这类词,靠关键词猜会漏判)。拿不到platform
    (比如没匹配上bgm_id的老番剧)时,退回现在的关键词猜测兜底。

    platform明确是剧场版/OVA时直接权威判定、排在EXTRA_MARKERS关键词猜测之前——
    一个Bangumi官方标注的独立剧场版/OVA条目不可能同时是随季打包的OP/ED花絮文件,
    两者互斥,不需要再靠关键词去猜。
    """
    if platform == "剧场版":
        return "movie"
    if platform == "OVA":
        return "ova"
    lowered = torrent_title.lower()
    if _EXTRA_PATTERN.search(lowered) or _BRACKET_SP_TAG.search(lowered):
        return "extra"
    if _MOVIE_NUMBER_TAG.search(lowered) or any(marker in lowered for marker in MOVIE_MARKERS):
        return "movie"
    if any(marker in lowered for marker in OVA_MARKERS):
        return "ova"
    return "tv"


# 番剧文件夹下"这是个季度目录"的判定。routers/library.py的scan_local_folder_structure
# 扫描物理目录时也用这一个,不能两边各写一份——work_title_bucket()要靠它避开
# 会被误认成季度目录的作品名(比如"SEED DESTINY"开头的S+字母就会命中)。
SEASON_DIR_PATTERN = re.compile(r"^(Season\s*|S|Specials|SP|第\s*)(\d+|[a-zA-Z]+)", re.IGNORECASE)

# 顶层桶里语义固定、不能被作品名目录占用的名字。
RESERVED_BUCKET_NAMES = {"剧场版", "劇場版", "OVA", "Other", "Specials/Others"}


def work_title_bucket(work_title: str | None, family_title: str | None) -> str | None:
    """算"这一部作品自己的目录名",拿不到返回None(调用方应退回Season 00)。

    用于**算不出season_ordinal的TV条目**:它们过去一律堆进Season 00,而播放器
    (Jellyfin/Plex)把Season 00当Specials——机动战士Z高达这种50集正片被当成特典。
    改成每部一个以作品名命名的目录,季号算不算得出来就不再影响目录结构。

    作品名以家族标题开头时去掉这段前缀:家族名已经是上一级目录了,再重复一遍没有
    信息量,去掉之后正好是用户要的"略称"——
        机动战士高达0083 星尘的回忆  ->  0083 星尘的回忆
        机动战士高达 雷霆宙域        ->  雷霆宙域
        机动战士Z高达               ->  机动战士Z高达(前缀对不上,保留全名)
    (查过Bangumi的infobox:"别名"字段里放的是罗马音和日文原名,没有可用的简称,
     去前缀是唯一不联网、不引入新数据的做法。)

    三道护栏,任何一条不满足就退回作品全名;全名也不可用时返回None:
    1. 去掉前缀后剩不到2个字符(比如家族标题几乎等于作品名),太短没法辨认;
    2. 结果命中SEASON_DIR_PATTERN——"SEED DESTINY"这类S开头的名字会被目录扫描器
       误认成季度目录,不能用;
    3. 结果撞上RESERVED_BUCKET_NAMES里语义固定的桶名。
    """
    full = (work_title or "").strip()
    if not full:
        return None

    candidates = []
    family = (family_title or "").strip()
    if family and full.startswith(family):
        trimmed = full[len(family):].strip(" 　-–—:：·")
        if len(trimmed) >= 2:
            candidates.append(trimmed)
    candidates.append(full)

    for name in candidates:
        safe = _sanitize_filename_segment(name)
        if safe in RESERVED_BUCKET_NAMES or SEASON_DIR_PATTERN.match(safe):
            continue
        return safe
    return None


def resolve_folder_bucket(
    media_type: str, bgm_id: int | None, season_ordinal: str | None,
    work_title: str | None = None, family_title: str | None = None,
) -> str:
    """
    根据media_type(classify_media_type的结果)+season_ordinal+bgm_id,算出这一部
    内容该落到哪个顶层桶:剧场版/OVA/作品名目录/Season {ordinal}。preview_rename_file()
    的改名路径、以及services/common.py家族缓存表写入时的"落地目录"展示字段,
    都调用这一个函数,保证两处判断永远是同一套逻辑,不会出现"缓存表说进A桶、
    实际文件却进了B桶"的错位。

    work_title/family_title只在"有bgm_id但算不出season_ordinal"这一条分支上用得到,
    交给work_title_bucket()拼目录名;拿不到作品名(历史数据、没匹配上Bangumi)时
    退回原来的"Season 00",老库的行为不变。

    不处理"extra"(OP/ED/PV这类)——那是种子内单个文件级别的判断,不是一个
    Bangumi条目本身该归哪里的问题,调用方自己处理,不经过这个函数。

    bgm_id为None(完全没匹配上Bangumi,没有任何结构信息)时的返回值只是个占位——
    preview_rename_file在这种情况下会走独立的文本正则兜底路径,不使用这个返回值。
    """
    if media_type == "movie":
        return "剧场版"
    if media_type == "ova":
        return "OVA"
    if media_type == "tv" and bgm_id is not None and season_ordinal is None:
        return work_title_bucket(work_title, family_title) or "Season 00"
    if season_ordinal is not None:
        return f"Season {season_ordinal}"
    return "Season 01"


_ILLEGAL_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]+')


def _sanitize_filename_segment(text: str) -> str:
    """去掉Windows文件名非法字符。不能直接复用services/common.py的
    sanitize_path_segment——那边反过来import了这个模块,会形成循环import。"""
    return _ILLEGAL_FILENAME_CHARS.sub("_", text).strip() or "未命名"

def build_anime_folder_name(anime_title: str, bgm_id: int | None) -> str:
    """
    媒体库里番剧文件夹的命名规则:优先带上显性的bgm_id作为唯一标识,
    避免"无职转生"和"無職轉生"这类同番不同译名各占一个文件夹的情况。
    TMDB接入是阶段二工作,先用bgm_id顶上。
    """
    return f"{anime_title} [bgm-{bgm_id}]" if bgm_id else anime_title


_ZH_SEASON_NUM_MAP = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    "１": 1, "２": 2, "３": 3, "４": 4, "５": 5, "６": 6, "７": 7, "８": 8, "９": 9, "０": 0,
}


def _resolve_season_str(parsed_season, search_text: str) -> str:
    """
    根据anitopy解析出的季度(可能为空)+一段兜底搜索文本,算出两位数的季度字符串。
    search_text用于在没有结构化季度信息时,靠"第X季"/"SXX"这类文本正则兜底提取。
    """
    season_number = parsed_season
    if not season_number:
        zh_match = re.search(r'第\s*([0-9一二三四五六七八九十１２３４５６７８９０]+)\s*季', search_text)
        if zh_match:
            val = zh_match.group(1).strip()
            season_number = _ZH_SEASON_NUM_MAP.get(val, val)
        else:
            s_match = re.search(r'[sS](\d+)', search_text)
            season_number = s_match.group(1) if s_match else "1"
    try:
        return f"{int(season_number):02d}"
    except (ValueError, TypeError):
        return "01"


def _episode_fallback_str(search_text: str, generic_fallback: bool = False) -> str:
    """
    结构化集数缺失时,靠正则从文本里兜底提取集数。
    generic_fallback控制是否在专用的".5话"正则都没命中时,再退一步尝试提取任意1-3位数字
    ——种子整体标题场景关掉这个兜底(标题里的数字噪音更多),单文件场景打开。
    """
    ep_match = re.search(r'(?:第|E|\[)(\d+\.5)(?:话|集|\])?', search_text, re.IGNORECASE)
    if ep_match:
        return ep_match.group(1)
    if generic_fallback:
        # 两侧都不能贴着字母,否则"10bit"/"1080p"这类分辨率/编码后缀里的数字
        # 会被误当成集数抓取(比如没有真实集数的OVA/PV文件会被错误猜出集数)。
        generic_match = re.search(r'(?<![a-zA-Z\d])(\d{1,3})(?![a-zA-Z\d])', search_text)
        if generic_match:
            # 补零跟结构化解析路径(_extract_release_version等处的:02d)保持一致——
            # 不补零的话,这里抓到的裸数字(比如误从标题季号"3"里抓出来的)跟同一季
            # 正常补零的"03"只差一位,视觉上像是"同一集重复"了,详见相关bug记录。
            return f"{int(generic_match.group(1)):02d}"
    return "??"


def _extract_episode_str(episode_number, search_text: str, generic_fallback: bool = False) -> str:
    """根据anitopy解析出的集数(可能为空/列表)+一段兜底搜索文本,算出集数字符串。"""
    if isinstance(episode_number, list):
        episode_number = episode_number[0] if episode_number else None
    if not episode_number:
        return _episode_fallback_str(search_text, generic_fallback)
    try:
        return f"{int(episode_number):02d}"
    except (ValueError, TypeError):
        return str(episode_number)


def _build_meta_suffix(fansub: str, resolution: str) -> str:
    """拼出" [字幕组][分辨率]"后缀,任意一项缺失时自动去掉对应的空方括号。"""
    if not (fansub or resolution):
        return ""
    return f" [{fansub}][{resolution}]".replace("[][", "[").replace("][]", "]")

# 🔄 恢复成你原有的 3 个参数签名，确保后端其他文件调用时不崩溃
def preview_rename(anime_title: str, torrent_title: str, library_root: str):
    """
    终极通用重命名预览引擎 (向下兼容版)
    """
    parsed = anitopy.parse(torrent_title) or {}
    media_type = classify_media_type(torrent_title)
    file_ext = parsed.get("file_extension", "mkv")

    # 根据传入的 library_root 自动切分 TV 和 独立电影库
    tv_root = library_root

    anime_folder_name = anime_title

    # 2. 提取集数 (针对 12.5 话做特殊正则兼容)
    episode_str = _extract_episode_str(parsed.get("episode_number"), torrent_title)

    # 3. 提取保留的元数据（字幕组、分辨率），用于松鼠党洗版肉眼查看
    fansub = parsed.get("release_group", "")
    resolution = parsed.get("video_resolution", "")
    meta_suffix = _build_meta_suffix(fansub, resolution)

    # 4. 根据不同特例走不同的分流策略
    
    # --- 特例 2 (方案A)：剧场版，直接切入独立的电影媒体库 ---
    if media_type == "movie":
        folder_path = f"{tv_root}\\{anime_folder_name}\\剧场版"
        filename = f"{anime_title}{meta_suffix}.{file_ext}"
        
    # --- 特例 3：OP/ED/花絮，切入 TV 目录下的 Other 文件夹 ---
    elif media_type == "extra":
        folder_path = f"{tv_root}\\{anime_folder_name}\\Other"
        clean_title = torrent_title.split(']')[-1].strip() if ']' in torrent_title else torrent_title
        filename = f"{clean_title}.{file_ext}"
        
    # --- 特例 1 (方案A)：OVA、12.5话，强制划分到 Season 00 ---
    elif media_type == "ova":
        folder_path = f"{tv_root}\\{anime_folder_name}\\Season 00"
        filename = f"{anime_title} - S00E{episode_str}{meta_suffix}.{file_ext}"
        
    # --- 常规 TV 正片逻辑 ---
    else:
        season_str = _resolve_season_str(parsed.get("anime_season"), torrent_title)
        folder_path = f"{tv_root}\\{anime_folder_name}\\Season {season_str}"
        filename = f"{anime_title} - S{season_str}E{episode_str}{meta_suffix}.{file_ext}"

    # 规范化 Windows 路径
    folder_path = folder_path.replace("/", "\\")
    full_path = f"{folder_path}\\{filename}"

    return {
        "original_title": torrent_title,
        "media_type": media_type,
        "parsed_episode": episode_str,
        "parsed_resolution": resolution,
        "parsed_fansub": fansub,
        "target_folder": folder_path,
        "target_filename": filename,
        "target_full_path": full_path
    }




def find_sibling_subtitles(video_path: str, all_paths: list[str]) -> list[str]:
    """
    找到跟某个视频文件"文件名主干"一致、同目录的字幕文件。
    例如 01.mkv 会匹配上 01.chs.ass / 01.cht.srt,但不会匹配 02.ass。
    """
    video_dir = video_path.rsplit("/", 1)[0] if "/" in video_path else ""
    video_name = video_path.rsplit("/", 1)[-1]
    video_stem = video_name.rsplit(".", 1)[0]
    matches = []
    for p in all_paths:
        if p == video_path:
            continue
        p_dir = p.rsplit("/", 1)[0] if "/" in p else ""
        p_name = p.rsplit("/", 1)[-1]
        ext = p_name.rsplit(".", 1)[-1].lower() if "." in p_name else ""
        if ext in SUBTITLE_EXTS and p_dir == video_dir and p_name.startswith(video_stem):
            matches.append(p)
    return matches


def parse_file_season(file_name: str) -> str | None:
    """从单个文件名里解析出它自己属于第几季,返回两位数序号字符串("00"/"01"/"02"/...)或 None。
    给"多季混合合集包"用:S1 文件名通常不带季度标记(→None,走文件夹级季度),
    S2 文件名带 "S2"/"第二季"/"2nd Season" 之类(→"02"),SP/特典常写 "S00"(→"00")。
    只信 anitopy 的结构化解析,不做额外文本猜测。"""
    parsed = anitopy.parse(file_name) or {}
    season = parsed.get("anime_season")
    if isinstance(season, list):
        season = season[0] if season else None
    if season is None:
        return None
    try:
        n = int(str(season).strip())
    except (ValueError, TypeError):
        return None
    return f"{n:02d}" if n >= 0 else None


def _normalize_absolute_episode(
    raw_episode: int, episode_offset: int, season_total_eps: int | None
) -> int:
    """把"跨季连续的绝对编号"换算成"这一季内部的第几集"。

    有些字幕组从第一季结尾接着往下数,不在新一季重新从1开始(实测咒术回战
    第二季发的就是25~47,而这一季自己只有23集,正确结果是01~23)。判定信号只有
    一个、而且是通用的:**解析出来的原始数字超过了这一季自己的总集数**——
    超了就说明这个数字不可能是季内编号,只能是绝对编号,减掉前序各季的累计集数
    (episode_offset)就是真实的季内集数。

    没超上限的一律原样保留,不做任何猜测(实测无职转生第二季E20、第三季E06都是
    正常的季内编号,不能动)。episode_offset/season_total_eps任一为0或缺失时
    (Bangumi没有集数数据、或者这是第一季没有前序)同样原样保留——宁可漏改,
    也不能靠猜把本来正确的编号改坏。
    """
    if episode_offset <= 0 or not season_total_eps or season_total_eps <= 0:
        return raw_episode
    if raw_episode <= season_total_eps:
        return raw_episode
    candidate = raw_episode - episode_offset
    return candidate if candidate >= 1 else raw_episode


def preview_rename_file(
    anime_title: str, file_name: str, torrent_title: str, library_root: str,
    bgm_id: int | None = None, season_hint: str | None = None, episode_offset: int = 0,
    season_ordinal: str | None = None, platform: str | None = None,
    season_total_eps: int | None = None,
):
    """
    按种子内部单个文件计算目标路径,是合集场景的核心入口。
    file_name: 种子内的文件名(不含目录),用于判断集数/OP-ED/文件类型——
               合集场景下这层信息比种子整体标题更可靠。
    torrent_title: 种子整体标题,用于兜底提供季度/字幕组/分辨率这类"整个种子共享"的信息
                   (很多合集内部文件名只是"01.mkv"这种,季度/字幕组信息只在种子标题里出现一次)。
    season_hint: 这一部作品**自己的**标题(不是家族共用标题)。对剧场版/OVA/Season 00
                   这三类内容是**必需参数**:它们的文件名主干就是这个标题,缺了只能退回
                   anime_title,会抹掉副标题并让同家族的多部作品撞成同一个文件名。
                   拿不到真实作品名的调用方应该放弃改名,不要传None硬算——可以看
                   返回值里的work_title_from_hint自查。
    season_ordinal: 调用方(organize.py)用bangumi_client.resolve_tv_season_ordinal()
                   算出来的"这是系列里第几个真正的TV季"("01"/"02"/...),不看种子标题文本,
                   只信Bangumi关联图谱本身——有值时直接采用,不再走下面的文本正则猜测。
    platform: 这个bgm_id在Bangumi的官方类型(TV/OVA/剧场版/...),传给classify_media_type
                   做权威分流,比种子标题关键词猜测可靠。
    episode_offset / season_total_eps: 这一季之前各季的累计集数 / 这一季自己的总集数,
                   由调用方从services/bgm_series_cache.py::build_season_episode_table()
                   取得,用于把字幕组的跨季绝对编号换算成季内编号,详见
                   _normalize_absolute_episode()。

    返回值里的relative_path是"相对anime_root"的路径,配合qBittorrent的API约束:
    一个种子的setLocation只能设一个根位置,Season/Other子目录分类要靠renameFile的
    相对路径实现。剧场版是唯一例外(有独立的Movies根目录,不共享anime_root),
    这种情况relative_path返回None,调用方应该跳过这个文件、保留原位不动。
    """
    parsed = anitopy.parse(file_name) or {}
    torrent_parsed = anitopy.parse(torrent_title) or {}
    # 只按这个文件自己的文件名判类型,不掺种子整体标题——合集种子的标题经常会写
    # "01-11TV全集+OVA"这类描述"整个种子包含哪些内容"的话,如果拿去跟每个文件名
    # 拼在一起判断,会把标题里提到的"OVA"关键词误传染给种子里所有的TV正片文件,
    # 导致正片也被错误分类进OVA分支(这正是多集OVA+正传混合种子整理错乱的根因)。
    # platform(Bangumi官方类型)不受这个影响,已经在classify_media_type内部优先判断。
    media_type = classify_media_type(file_name, platform)
    # 下载前的预览(routers/download_submit.py::preview_rename_endpoint)拿不到种子
    # 内部真实文件名,只能拿整个种子标题当file_name占位——标题里常见"AV1 OPUS 2.0"
    # 这种声道标注,朴素取"最后一个.后面的内容"会把"2.0"的"0"误判成扩展名。
    # 真实文件名(下载完成后organize.py那边传进来的)本来就已经是VIDEO_EXTS过滤过的
    # 视频文件,取出来的后缀必然在这个集合里;不在集合里就说明这次拿到的根本不是
    # 真实文件名,退回"mkv"占位,不把标题里的噪音当成扩展名用。
    candidate_ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    file_ext = candidate_ext if candidate_ext in VIDEO_EXTS else "mkv"

    tv_root = library_root

    anime_folder_name = build_anime_folder_name(anime_title, bgm_id)
    anime_root = f"{tv_root}\\{anime_folder_name}"

    episode_number = parsed.get("episode_number") or torrent_parsed.get("episode_number")
    if isinstance(episode_number, list):
        episode_number = episode_number[0] if episode_number else None
    if not episode_number:
        episode_str = _episode_fallback_str(file_name, generic_fallback=True)
    else:
        try:
            raw_episode = _normalize_absolute_episode(
                int(episode_number), episode_offset, season_total_eps
            )
            episode_str = f"{raw_episode:02d}"
        except (ValueError, TypeError):
            episode_str = str(episode_number)

    fansub = parsed.get("release_group") or torrent_parsed.get("release_group", "")
    resolution = parsed.get("video_resolution") or torrent_parsed.get("video_resolution", "")
    meta_suffix = _build_meta_suffix(fansub, resolution)

    anime_root = f"{tv_root}\\{anime_folder_name}"

    # OVA(Bangumi platform=="OVA"或标题关键词命中)单独进OVA文件夹,不跟Season 00
    # 混在一起——这是用户看完柯南完整落地表格后提的组织偏好,柯南这类长篇一个家族
    # 里往往同时有一堆正牌OVA(MAGIC FILE系列之类)和"够不上真季、又不是OVA"的短篇
    # TV特典,分开放肉眼更好分辨。
    #
    # 走Season 00桶的情况:已知bgm_id(有Bangumi结构信息)但season_ordinal算不出来
    # ——说明这一部不在"真季名单"里(比如集数很短的旁支正片、剧场版剪成TV重播的版本),
    # 又不是OVA/剧场版,不能硬撞Season 01,也不该混进OVA文件夹。
    # season_hint就是"这一部作品自己的标题",anime_title是家族共用标题——算不出
    # season_ordinal的TV条目靠这两个拼出以作品名命名的目录(见work_title_bucket)。
    folder_bucket = resolve_folder_bucket(
        media_type, bgm_id, season_ordinal,
        work_title=season_hint, family_title=anime_title,
    )

    # 下面movie/ova/Season 00三个分支都拿"这一部作品自己的标题"当文件名主干,
    # season_hint缺席时只能退回家族共用的anime_title——那会把副标题整段抹掉
    # (『机动战士高达 闪光的哈萨维』→『机动战士高达』),而且同一家族的多部作品
    # 会全部撞成同一个文件名。所以对这三个分支来说season_hint是必需参数,不是
    # 可选提示。返回值里的work_title_from_hint就是给调用方自查用的:为False说明
    # 这个文件名是拿家族标题猜的、不可信,重算类的调用方(services/library_repair.py)
    # 应该放弃这条建议而不是照着改名。
    work_title_from_hint = bool(season_hint)

    if media_type == "movie":
        folder_path = f"{anime_root}\\{folder_bucket}"
        # 用"这一部作品自己的标题"而不是家族共用的anime_title——同一个系列可能
        # 有好几部剧场版(比如青春猪头少年现实里有4部),全用anime_title拼文件名
        # 会全部撞成同一个文件名,后落地的覆盖先落地的。
        work_title = _sanitize_filename_segment(season_hint or anime_title)
        filename = f"{work_title}{meta_suffix}.{file_ext}"
    elif media_type == "extra":
        folder_path = f"{anime_root}\\Other"
        clean_title = file_name.rsplit(".", 1)[0]
        filename = f"{clean_title}.{file_ext}"
    elif media_type == "ova":
        folder_path = f"{anime_root}\\{folder_bucket}"
        # 只有真正的TV正片季才有"第几季第几集"这个概念——OVA跟剧场版一样,
        # 是独立的一部作品,不套SxxExx编号,直接用作品自己的标题+字幕组/分辨率,
        # 更贴合Jellyfin/Plex这类刮削器对"独立特典/OVA条目"的识别方式(标准做法是
        # 按作品名单独归类,不是塞进某一季的集数序列里)。多集OVA(同一个Bangumi
        # 条目内部有好几集)如果同字幕组/分辨率发布,只用work_title拼文件名会
        # 全部撞成同一个文件名,后落地的覆盖先落地的——有解析出真实集数时补一个
        # "- Exx"消歧;真的只有一集、解析不出集数时保持原样干净命名,不硬拼"E??"。
        work_title = _sanitize_filename_segment(season_hint or anime_title)
        if episode_str != "??":
            filename = f"{work_title} - E{episode_str}{meta_suffix}.{file_ext}"
        else:
            filename = f"{work_title}{meta_suffix}.{file_ext}"
    elif bgm_id is not None and season_ordinal is None:
        # 有Bangumi结构信息但算不出季号的TV条目。桶名由resolve_folder_bucket给出:
        # 拿得到作品名就是以作品名命名的目录,拿不到才退回"Season 00"——判定条件写成
        # 跟resolve_folder_bucket里那条分支同一个表达式,两边不会因为桶名变了就错位。
        folder_path = f"{anime_root}\\{folder_bucket}"
        # 同上:这类是"够不上真季"的旁支正片/短篇特典,不是TV正片的某一季,
        # 同样不套SxxExx编号,但同样存在多集撞名覆盖的风险,处理方式跟OVA一致。
        # 文件名用**完整**作品名(不是目录用的略称),跟剧场版/OVA桶的做法一致:
        # 文件被单独拷出去时仍然自带完整信息。
        work_title = _sanitize_filename_segment(season_hint or anime_title)
        if episode_str != "??":
            filename = f"{work_title} - E{episode_str}{meta_suffix}.{file_ext}"
        else:
            filename = f"{work_title}{meta_suffix}.{file_ext}"
    else:
        if season_ordinal is not None:
            season_str = season_ordinal
            folder_path = f"{anime_root}\\{folder_bucket}"
        else:
            # 没有season_ordinal可用(bgm_id全程未知,没有任何Bangumi结构信息)——
            # 完整保留原来的文本正则猜测,不引入新行为,也不经过resolve_folder_bucket。
            season_search_text = f"{season_hint or anime_title} {torrent_title}"
            season_str = _resolve_season_str(torrent_parsed.get("anime_season"), season_search_text)
            folder_path = f"{anime_root}\\Season {season_str}"
        filename = f"{anime_title} - S{season_str}E{episode_str}{meta_suffix}.{file_ext}"

    folder_path = folder_path.replace("/", "\\")
    full_path = f"{folder_path}\\{filename}"

    # relative_path是相对anime_root的部分,用于renameFile的newPath参数
    relative_path = full_path[len(anime_root):].lstrip("\\") if full_path.startswith(anime_root) else None
    # target_relative_path是相对library_root(tv_root)的部分,跟relative_path语义不同——
    # 这个是给RenamedFile持久化用的,不受library_root搬到别的盘/目录影响(见models.py说明)。
    target_relative_path = full_path[len(tv_root):].lstrip("\\") if full_path.startswith(tv_root) else None

    return {
        "original_file_name": file_name,
        "media_type": media_type,
        "work_title_from_hint": work_title_from_hint,
        "parsed_episode": episode_str,
        "release_version": _extract_release_version(parsed, file_name),
        "anime_root": anime_root,
        "relative_path": relative_path,  # None代表剧场版这种不共享根目录的特殊情况
        "target_folder": folder_path,
        "target_filename": filename,
        "target_full_path": full_path,
        "target_relative_path": target_relative_path,
    }

def _extract_release_version(parsed: dict, file_name: str) -> int:
    version = parsed.get("release_version")
    if version:
        try:
            return int(str(version).lstrip("vV"))
        except (ValueError, TypeError):
            pass
    v_match = re.search(r'[vV](\d+)(?:\D|$)', file_name)
    return int(v_match.group(1)) if v_match else 1