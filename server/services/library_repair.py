"""
媒体库修复:检测"按当前改名规则重算的结果"与磁盘实际文件之间的落差,以及数据库里
指向已不存在文件/目录的孤儿记录,供设置页"修复媒体库"功能调用。

设计前提(详见server端调研结论,不要在这里再引入"迁移library_root/download_root"
之类的逻辑):
- RenamedFile.target_relative_path / LocalMedia.folder_name 本身就是相对library_root
  存储的,library_root换目录后不需要迁移这两张表,只需要用当前的library_root重新
  跑一遍下面的检查即可自然覆盖。
- 已整理完的文件,原始下载信息(种子标题/原始文件名)大多没有持久化,不能"回放"当初
  的整理输入来重算——改用磁盘上当前的文件名反向重算"如果现在整理应该长什么样",
  这样也顺带覆盖了用户手动拖进库、从未走过整理流程的文件。
"""
import os
import re
from pathlib import Path

from sqlalchemy.orm import Session

import config_store
import models
import rename_engine
from routers.library import VIDEO_EXTENSIONS, get_library_root, scan_local_folder_structure
from services.common import get_setting

# scan_local_folder_structure对没识别出结构的目录统一归进这个兜底桶——不确定这类
# 目录是不是番剧目录本来的组织方式,不参与重算,避免误判乱改。
_SKIPPED_BUCKETS = {"Specials/Others"}


def _normcase(path: Path) -> str:
    """跨平台安全的路径比较key:Windows下大小写不敏感、统一分隔符,POSIX下原样保留
    (docker/linux部署时library_root就在POSIX路径下,大小写本来就敏感)。"""
    return os.path.normcase(str(path))


def _same_relpath(relative_path: str) -> str:
    """只统一分隔符的相对路径比较key(不动大小写,POSIX下大小写是有意义的)。

    本模块对外产出的相对路径统一用正斜杠,但RenamedFile.target_relative_path
    历史数据存的是反斜杠(见services/staging.py::upsert_renamed_file),直接用
    ==比裸字符串永远匹配不上——那正是"物理文件改完名,数据库里的路径却从来
    没跟着更新"的根因。
    """
    return relative_path.replace("\\", "/")


def _to_db_relpath(relative_path: str) -> str:
    """写回RenamedFile.target_relative_path时用的形态:沿用库里既有的反斜杠风格,
    保证这一列不会同时出现两种分隔符(get_current_version_at_target是按精确
    字符串查这一列的,风格混了会查不到、导致版本冲突判断失效)。"""
    return relative_path.replace("/", "\\")


def reset_season_cache(db: Session) -> int:
    """清空AnimeFamilyCache,让接下来的扫描按当前算法重新解析季度关系。返回清掉的行数。

    修复流程不能信任这张表的历史内容:季度归并算法演进过——早期版本给同一季拆播的
    每个cour各分配一个独立的season_ordinal(01/02/03/04/05),现在会把它们合并成
    同一季(01/01/02/02/03)。旧行留着会造成两种后果,而且都会动到用户的文件:
    1) 真正过时的目录(比如实际只有3季却存在Season 05)在旧表里反而是"合法编号",
       检查不出来、修不到;
    2) 每季的集数/前序累计集数全是按cour切的,拿它算跨季绝对编号换算会减错偏移量,
       把本来正确的文件改成错误集数(实测把S02E20改成了S02E09)。

    放在"扫描"入口而不是"应用修复"入口:apply_rename_fixes内部会重跑一次扫描做
    TOCTOU防护,如果等到apply才清缓存,界面上让用户确认的是旧缓存算出的建议、实际
    执行的却是重算后的新结果,等于"确认了A、改成了B",破坏这个功能"逐条人工确认后
    才动文件"的核心保证。
    """
    removed = db.query(models.AnimeFamilyCache).delete(synchronize_session=False)
    db.commit()
    return removed


async def _resolve_season_table(db: Session, main_bgm_id: int) -> dict[str, dict]:
    """拿这部番的"每季集数/偏移量/季度本名"表(见bgm_series_cache.build_season_episode_table),
    它同时也是"当前算法下合法的季度编号集合"的权威来源。缓存未命中(这个bgm_id从没被
    下载/整理流程算过)时现查一次resolve_family_season_map补上。
    """
    import bangumi_family  # 延迟导入,避免循环import

    from services.bgm_series_cache import _persist_family_map, build_season_episode_table

    has_cache = (
        db.query(models.AnimeFamilyCache)
        .filter(models.AnimeFamilyCache.source_bgm_id == main_bgm_id)
        .first()
        is not None
    )
    if not has_cache:
        try:
            family_map = await bangumi_family.resolve_family_season_map(main_bgm_id)
        except Exception:
            family_map = {}
        if family_map:
            _persist_family_map(db, main_bgm_id, family_map)

    return build_season_episode_table(db, main_bgm_id)


def _resolve_effective_season(season_num: str, season_table: dict[str, dict]) -> str | None:
    """把文件夹名上的季度编号,映射成当前算法下真实有效的season_ordinal。

    编号本身合法(在season_table里)就直接采用——比如无职转生磁盘上的Season 02/03、
    咒术回战的Season 02,这些内容本身是对的,不能因为别的原因去动它们。

    编号不合法(比如无职转生磁盘上残留的Season 05,而这个系列现在只有01/02/03)说明
    它来自更早期"按第几个cour顺序编号"的方案(1期→S01, 1期2クール→S02, 2期→S03,
    2期2クール→S04, 3期→S05)。按这个前提反推:取按首播日期排第NN个的那一季,
    用它真实的season_ordinal——无职转生的第5个cour属于第三期,正好得到"03"。
    NN超出总cour数时退回"最新的那一季"兜底。

    这是启发式,但扫描结果要用户逐条勾选确认后才会真正改文件,判错了用户不勾即可。
    """
    if not season_table:
        return None
    if season_num in season_table:
        return season_num

    # 按季度顺序把每一季的cour数展开成一个"第几个cour -> season_ordinal"的序列
    cour_sequence: list[str] = []
    for ordinal in sorted(season_table):
        cour_sequence.extend([ordinal] * max(season_table[ordinal]["cour_count"], 1))

    index = int(season_num) - 1
    if 0 <= index < len(cour_sequence):
        return cour_sequence[index]
    return sorted(season_table)[-1]


def _parse_season_bucket(bucket_name: str) -> str | None:
    """把"Season 05"这类桶名解析成两位数季度序号字符串"05";不是Season NN格式的
    桶名(剧场版/OVA/Other/未识别桶)返回None。"""
    m = re.match(r"^Season\s*0*(\d+)$", bucket_name)
    return f"{int(m.group(1)):02d}" if m else None


def _non_season_args(platform: str | None = None) -> dict:
    """非"Season NN"桶的重算参数:这些桶装的都是剧场版/OVA/花絮/够不上真季的旁支,
    本来就不套SxxExx编号,所以没有季度序号、也不需要集数偏移换算。"""
    return {
        "season_ordinal": None,
        "platform": platform,
        "season_hint": None,
        "episode_offset": 0,
        "season_total_eps": None,
    }


def _bucket_recompute_args(bucket_name: str) -> dict | None:
    """按扫盘桶名决定重算preview_rename_file要用的season_ordinal/platform/season_hint。
    返回None代表这个桶不参与重算(未识别的兜底桶)。只处理不需要season_ordinal存在性
    校验的桶(剧场版/OVA/Other/Season 00);"Season NN"(NN!="00")桶由调用方
    (scan_rename_mismatches)单独处理,不走这个函数。

    电影/OVA桶显式把platform传成"剧场版"/"OVA"——不能指望文件名里还留着
    这类关键词(已经改过名的文件很可能已经不含"movie"/"OVA"这类词了),
    只有靠"这个文件本来就躺在这个桶目录下"这个既成事实反推分类,才能让
    classify_media_type()稳定走到正确分支。

    Other桶反而不强制platform,交给classify_media_type()按文件名内容自己判断
    (包括新加的[M##]剧场版编号识别)——这个桶天然是大杂烩,可能是真花絮,也可能是
    从没被本程序处理过、混进来的正片/电影/OVA,唯一能用的就是文件名内容本身。
    """
    if bucket_name in ("剧场版", "劇場版"):
        return _non_season_args(platform="剧场版")
    if bucket_name == "OVA":
        return _non_season_args(platform="OVA")
    if bucket_name in ("Other", "Season 00"):
        return _non_season_args()
    return None


async def scan_rename_mismatches(db: Session) -> list[dict]:
    """核心检查:遍历每个已绑定bgm_id的LocalMedia文件夹,按当前改名规则重算每个
    视频文件"应该"落在哪,跟磁盘上实际路径不一致的记为一条改名建议。只读,不动
    文件也不写库。
    """
    library_root = get_library_root(db)
    if not library_root.exists():
        return []

    results: list[dict] = []
    medias = db.query(models.LocalMedia).filter(models.LocalMedia.bgm_id.isnot(None)).all()

    for media in medias:
        # 只有物理文件夹名严格符合当前"{标题} [bgm-{id}]"命名约定时才参与重算——
        # preview_rename_file内部会用build_anime_folder_name(anime_title, bgm_id)
        # 重新拼出anime_root,如果拼出来的顶层文件夹名跟磁盘上实际的folder_name不一致
        # (比如通过/library/match手动绑定、物理文件夹名从没改过),会导致提案变成
        # "把整个文件夹搬到一个新顶层目录",这已经超出"单个文件改名"的范畴,不安全,
        # 直接跳过整部番,不生成任何建议。
        folder_match = re.match(rf"^(.*)\s\[bgm-{media.bgm_id}\]$", media.folder_name)
        if not folder_match:
            continue
        anime_title = folder_match.group(1).strip()

        anime_path = library_root / media.folder_name
        structure = scan_local_folder_structure(anime_path, library_root)
        if not structure:
            continue

        season_table: dict[str, dict] | None = None
        proposed_targets_seen: dict[str, str] = {}

        for bucket_name, episodes in structure.items():
            if bucket_name in _SKIPPED_BUCKETS:
                continue

            season_num = _parse_season_bucket(bucket_name)
            if season_num is not None and season_num != "00":
                # "Season NN"桶(NN!="00"):不信任文件夹上的数字,拿这部番真实的季度表
                # 校验/映射一次——懒加载,避免没有任何"真季"内容的番剧也白跑一次
                # Bangumi请求。
                if season_table is None:
                    season_table = await _resolve_season_table(db, media.bgm_id)
                effective = _resolve_effective_season(season_num, season_table)
                if effective is None:
                    continue  # 完全拿不到季度表(网络失败等),这一桶不动
                info = season_table[effective]
                args = {
                    "season_ordinal": effective,
                    "platform": info["platform"],
                    "season_hint": info["name"],
                    "episode_offset": info["episode_offset"],
                    "season_total_eps": info["eps"],
                }
            else:
                args = _bucket_recompute_args(bucket_name)
                if args is None:
                    continue

            for ep in episodes:
                current_full_path = library_root / ep["rel_path"]
                if current_full_path.suffix.lower() not in VIDEO_EXTENSIONS:
                    continue

                preview = rename_engine.preview_rename_file(
                    anime_title=anime_title,
                    file_name=ep["filename"],
                    torrent_title=ep["filename"],
                    library_root=str(library_root),
                    bgm_id=media.bgm_id,
                    season_hint=args["season_hint"],
                    season_ordinal=args["season_ordinal"],
                    platform=args["platform"],
                    episode_offset=args["episode_offset"],
                    season_total_eps=args["season_total_eps"],
                )
                target_full_path = preview.get("target_full_path")
                if not target_full_path or preview.get("parsed_episode") == "??":
                    continue

                proposed_path = Path(target_full_path)
                if _normcase(current_full_path) == _normcase(proposed_path):
                    continue

                current_rel = str(current_full_path.relative_to(library_root)).replace("\\", "/")
                proposed_rel = str(proposed_path.relative_to(library_root)).replace("\\", "/") \
                    if _is_within(proposed_path, library_root) else None
                if proposed_rel is None:
                    continue

                blocked = False
                block_reason = None
                key = _normcase(proposed_path)
                if proposed_path.exists() and _normcase(proposed_path) != _normcase(current_full_path):
                    blocked = True
                    block_reason = "目标位置已存在另一个文件,为避免覆盖不自动处理"
                elif key in proposed_targets_seen:
                    blocked = True
                    block_reason = f"跟同一批次里的另一条改名建议目标冲突({proposed_targets_seen[key]})"
                else:
                    proposed_targets_seen[key] = current_rel

                siblings = rename_engine.find_sibling_subtitles(
                    current_rel, [str(e["rel_path"]) for eps in structure.values() for e in eps]
                )

                results.append({
                    "folder_name": media.folder_name,
                    "current_relative_path": current_rel,
                    "proposed_relative_path": proposed_rel,
                    "sibling_subtitles": siblings,
                    "blocked": blocked,
                    "block_reason": block_reason,
                })

    return results


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def scan_orphaned_records(db: Session) -> dict:
    """孤儿数据检查(只读):RenamedFile的done记录/AnimeFolder的暂存目录,在磁盘上
    是否还能找到对应的文件/目录。LocalMedia的孤儿判断复用routers/library.py::
    scan_and_update_library里已有的"文件夹是否还在磁盘上"逻辑,这里不重复实现,
    调用方(apply阶段)直接调那个函数即可。
    """
    library_root = get_library_root(db)
    orphaned_renamed_files = []
    if library_root.exists():
        done_rows = db.query(models.RenamedFile).filter(models.RenamedFile.status == "done").all()
        for row in done_rows:
            if not row.target_relative_path:
                continue
            # 库里存的是反斜杠风格,POSIX(docker/linux)下Path不会把反斜杠当分隔符,
            # 整段会被当成一个带反斜杠的怪文件名 -> exists()恒为False -> 把好记录
            # 全判成孤儿删掉。这里先统一成当前平台的分隔符再判存在性。
            relative = row.target_relative_path.replace("\\", os.sep).replace("/", os.sep)
            full_path = library_root / relative
            if not full_path.exists():
                orphaned_renamed_files.append({
                    "id": row.id,
                    "torrent_hash": row.torrent_hash,
                    "target_relative_path": row.target_relative_path,
                })

    download_root = Path(get_setting(db, "download_root", config_store.DEFAULTS["download_root"]))
    orphaned_anime_folders = []
    if download_root.exists():
        for row in db.query(models.AnimeFolder).all():
            if not Path(row.staging_folder).exists():
                orphaned_anime_folders.append({"id": row.id, "staging_folder": row.staging_folder})

    return {
        "orphaned_renamed_files": orphaned_renamed_files,
        "orphaned_anime_folders": orphaned_anime_folders,
    }


async def apply_rename_fixes(db: Session, selected_paths: set[str] | None) -> dict:
    """重新执行一次只读扫描(防止扫描和点击应用之间用户手动改动了文件的TOCTOU问题),
    对选中且未被阻塞的条目实际执行文件系统rename/move,成功后如果有对应的RenamedFile
    行(按旧的target_relative_path匹配),一并把它的target_relative_path更新为新值。
    """
    library_root = get_library_root(db)
    mismatches = await scan_rename_mismatches(db)

    # 一次性按"只统一分隔符"的key给RenamedFile建索引,供下面改完名后同步DB路径用
    # (不能直接用==比,原因见_same_relpath)。同一个归一化路径理论上只会有一行,
    # 真撞了以后来的覆盖前面的,跟get_current_version_at_target取最新版的取向一致。
    rows_by_relpath = {
        _same_relpath(row.target_relative_path): row
        for row in db.query(models.RenamedFile)
        .filter(models.RenamedFile.target_relative_path.isnot(None))
        .all()
    }

    succeeded, skipped, failed = [], [], []
    for item in mismatches:
        current_rel = item["current_relative_path"]
        if selected_paths is not None and current_rel not in selected_paths:
            continue
        if item["blocked"]:
            skipped.append({"path": current_rel, "reason": item["block_reason"]})
            continue

        current_full = library_root / current_rel
        proposed_full = library_root / item["proposed_relative_path"]
        try:
            proposed_full.parent.mkdir(parents=True, exist_ok=True)
            current_full.rename(proposed_full)

            video_stem = current_full.stem
            for sub_rel in item["sibling_subtitles"]:
                sub_current = library_root / sub_rel
                if not sub_current.exists():
                    continue
                # 字幕文件名 = 视频文件名主干 + 语言/格式后缀(比如"01.chs.ass"里的
                # ".chs.ass"),把这段后缀原样接到改名后的视频主干上。
                suffix = sub_current.name[len(video_stem):]
                sub_proposed = proposed_full.with_name(proposed_full.stem + suffix)
                sub_current.rename(sub_proposed)

            row = rows_by_relpath.get(_same_relpath(current_rel))
            if row:
                row.target_relative_path = _to_db_relpath(item["proposed_relative_path"])
            db.commit()
            succeeded.append({"from": current_rel, "to": item["proposed_relative_path"]})
        except OSError as e:
            db.rollback()
            failed.append({"path": current_rel, "error": str(e)})

    return {"succeeded": succeeded, "skipped": skipped, "failed": failed}


def apply_orphan_cleanup(db: Session, categories: dict) -> dict:
    """执行孤儿数据清理:RenamedFile/AnimeFolder两张表按分类开关删除对应行。
    LocalMedia的清理直接复用routers/library.py::scan_and_update_library,由调用方
    (settings路由)另外调用,这里不重复。
    """
    orphans = scan_orphaned_records(db)
    result = {"removed_renamed_files": 0, "removed_anime_folders": 0}

    if categories.get("clean_renamed_files"):
        ids = [o["id"] for o in orphans["orphaned_renamed_files"]]
        if ids:
            db.query(models.RenamedFile).filter(models.RenamedFile.id.in_(ids)).delete(
                synchronize_session=False
            )
            result["removed_renamed_files"] = len(ids)

    if categories.get("clean_anime_folders"):
        ids = [o["id"] for o in orphans["orphaned_anime_folders"]]
        if ids:
            db.query(models.AnimeFolder).filter(models.AnimeFolder.id.in_(ids)).delete(
                synchronize_session=False
            )
            result["removed_anime_folders"] = len(ids)

    db.commit()
    return result
