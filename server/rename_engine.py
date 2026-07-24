import re
import anitopy

# 关键词定义
SUBTITLE_EXTS = {"ass", "srt", "ssa", "vtt", "sup"}
VIDEO_EXTS = {"mkv", "mp4", "ts", "avi", "flv", "mov", "wmv", "m2ts"}
MOVIE_MARKERS = ["剧场版", "劇場版", "movie", "gekijouban"]
OVA_MARKERS = ["ova", "oad", "特典", "特别篇", "番外篇", "sp", "总集篇", "回顾篇", ".5", "激活解说"]
EXTRA_MARKERS = ["op", "ed", "ncop", "nced", "opening", "ending", "pv", "预告",
                  "menu", "cm", "sample", "logo", "credit", "trailer", "teaser",
                  "interview", "spot", "bonus"]
# EXTRA_MARKERS里的短标记(op/ed/pv/cm等)朴素子串匹配容易误伤普通单词内部的字母组合
# (比如"Poppin'Dream"含有"op"),改用单词边界的正则;数字后缀0~2位是为了兼容
# "OP1"/"ED2"这类多首插曲编号的字幕组命名习惯。
_EXTRA_PATTERN = re.compile(
    r"(?<![a-z0-9])(op|ed|ncop|nced|opening|ending|pv|menu|cm|sample|logo|credit|"
    r"trailer|teaser|interview|spot|bonus)\d{0,2}(?![a-z0-9])|预告",
    re.IGNORECASE,
)

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
    if _EXTRA_PATTERN.search(lowered):
        return "extra"
    if any(marker in lowered for marker in MOVIE_MARKERS):
        return "movie"
    if any(marker in lowered for marker in OVA_MARKERS):
        return "ova"
    return "tv"


def resolve_folder_bucket(media_type: str, bgm_id: int | None, season_ordinal: str | None) -> str:
    """
    根据media_type(classify_media_type的结果)+season_ordinal+bgm_id,算出这一部
    内容该落到哪个顶层桶:剧场版/OVA/Season 00/Season {ordinal}。preview_rename_file()
    的改名路径、以及services/common.py家族缓存表写入时的"落地目录"展示字段,
    都调用这一个函数,保证两处判断永远是同一套逻辑,不会出现"缓存表说进A桶、
    实际文件却进了B桶"的错位。

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
        return "Season 00"
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
    if not ep_match and generic_fallback:
        # 两侧都不能贴着字母,否则"10bit"/"1080p"这类分辨率/编码后缀里的数字
        # 会被误当成集数抓取(比如没有真实集数的OVA/PV文件会被错误猜出集数)。
        ep_match = re.search(r'(?<![a-zA-Z\d])(\d{1,3})(?![a-zA-Z\d])', search_text)
    return ep_match.group(1) if ep_match else "??"


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


# 集数偏移量自动加减目前还没做全(大合集横跨多季、追更绝对编号延续等情况都还没处理好),
# 先关闭这个功能,只信任字幕组/合集包自己的集数编号,哪怕编号重复或者跳跃也不管——
# 我们只负责判断"这是第几季",不负责修正季内具体第几集。
# 想恢复实验,把这个开关改成True即可,不用再改调用方代码。
ENABLE_EPISODE_OFFSET = False


def preview_rename_file(
    anime_title: str, file_name: str, torrent_title: str, library_root: str,
    bgm_id: int | None = None, season_hint: str | None = None, episode_offset: int = 0,
    season_ordinal: str | None = None, platform: str | None = None,
):
    """
    按种子内部单个文件计算目标路径,是合集场景的核心入口。
    file_name: 种子内的文件名(不含目录),用于判断集数/OP-ED/文件类型——
               合集场景下这层信息比种子整体标题更可靠。
    torrent_title: 种子整体标题,用于兜底提供季度/字幕组/分辨率这类"整个种子共享"的信息
                   (很多合集内部文件名只是"01.mkv"这种,季度/字幕组信息只在种子标题里出现一次)。
    season_ordinal: 调用方(organize.py)用bangumi_client.resolve_tv_season_ordinal()
                   算出来的"这是系列里第几个真正的TV季"("01"/"02"/...),不看种子标题文本,
                   只信Bangumi关联图谱本身——有值时直接采用,不再走下面的文本正则猜测。
    platform: 这个bgm_id在Bangumi的官方类型(TV/OVA/剧场版/...),传给classify_media_type
                   做权威分流,比种子标题关键词猜测可靠。

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
    file_ext = file_name.rsplit(".", 1)[-1] if "." in file_name else "mkv"

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
            raw_episode = int(episode_number)
            # 只有原始集数"看起来还没偏移过"(<=偏移量)才加偏移;
            # 如果字幕组自己发布时已经用了绝对集数(原始数字比偏移量还大),
            # 说明这份文件本身已经是正确的绝对编号,不需要再加一次,
            # 否则会变成"偏移量+已经偏移过的数字"两次叠加导致集数错乱。
            if ENABLE_EPISODE_OFFSET and episode_offset and raw_episode <= episode_offset:
                 raw_episode += episode_offset
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
    folder_bucket = resolve_folder_bucket(media_type, bgm_id, season_ordinal)

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
    elif folder_bucket == "Season 00":
        folder_path = f"{anime_root}\\{folder_bucket}"
        # 同上:Season 00桶装的是"够不上真季"的旁支正片/短篇特典,不是TV正片的
        # 某一季,同样不套SxxExx编号,但同样存在多集撞名覆盖的风险,处理方式跟OVA一致。
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