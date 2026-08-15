"""
下载暂存目录记录 + 逐文件整理/改名状态记录。

RSS_FOLDER/ORGANIZE_TAG是qBittorrent这一侧的两个约定常量，AnimeFolder/RenamedFile
两张表则是"提交下载时预先记好要落到哪、后台整理任务实际处理到哪一步"的持久化记录，
organize.py/subscription.py都要用，放在一起管理。
"""
from sqlalchemy.orm import Session

import os

import rename_engine
from models import AnimeFolder, RenamedFile, StandaloneMedia

RSS_FOLDER = "anime-hub"  # 我们在qBittorrent的RSS订阅目录树里统一挂在这个文件夹下
ORGANIZE_TAG = "hub-organized"  # 打上这个标签代表后台整理任务已经处理过这个种子
# 反查不到AnimeFolder记录("这个种子到底是哪部番"无从得知)的种子打这个标签。
# 必须和ORGANIZE_TAG一起进get_completed_torrents的排除名单——否则这类种子每一轮
# 都会被重新捞出来、重新打一次同样的标签,既刷日志又白发qB请求。
UNKNOWN_TAG = "hub-unknown"


def staging_folder(download_root: str, anime_title: str, main_bgm_id: int | None) -> str:
    folder_name = rename_engine.build_anime_folder_name(anime_title, main_bgm_id)
    return f"{download_root.rstrip(chr(92)).rstrip('/')}\\{folder_name}"


def upsert_anime_folder(
    db: Session,
    staging_folder_path: str,
    anime_title: str,
    main_bgm_id: int | None,
    season_bgm_id: int | None,
    auto_rename: bool,
) -> None:
    """
    暂存文件夹 -> 番名/系列根ID/季度专属ID/是否自动改名 的对照,供后台整理任务反查使用。
    main_bgm_id决定文件夹归属(同系列不同季共享同一个值),
    season_bgm_id是这次提交时的季度专属ID,给季度文字判断和集数偏移量计算用。
    同一个暂存文件夹如果被再次提交(同一部番又下载了一次,或者调整了auto_rename开关),
    以最新这次为准更新记录。
    """
    folder = (
        db.query(AnimeFolder)
        .filter(AnimeFolder.staging_folder == staging_folder_path)
        .first()
    )
    if folder:
        folder.anime_title = anime_title
        folder.main_bgm_id = main_bgm_id
        folder.season_bgm_id = season_bgm_id
        folder.auto_rename = auto_rename
    else:
        folder = AnimeFolder(
            staging_folder=staging_folder_path,
            anime_title=anime_title,
            main_bgm_id=main_bgm_id,
            season_bgm_id=season_bgm_id,
            auto_rename=auto_rename,
        )
        db.add(folder)
    db.commit()


def upsert_renamed_file(
    db: Session,
    torrent_hash: str,
    original_path: str,
    status: str,
    target: str | None = None,
    error: str | None = None,
    release_version: int = 1,
) -> None:
    """target是相对library_root的相对路径(不含盘符前缀),不是绝对路径——
    这样library_root搬到别的盘/目录时,历史记录不会因为焊死了旧盘符而失效
    (见models.py::RenamedFile.target_relative_path的说明)。
    """
    row = (
        db.query(RenamedFile)
        .filter(
            RenamedFile.torrent_hash == torrent_hash,
            RenamedFile.original_path == original_path,
        )
        .first()
    )
    if row:
        row.status = status
        row.target_relative_path = target
        row.error = error
        row.release_version = release_version
    else:
        row = RenamedFile(
            torrent_hash=torrent_hash,
            original_path=original_path,
            status=status,
            target_relative_path=target,
            error=error,
            release_version=release_version,
        )
        db.add(row)
    db.commit()


def upsert_standalone_media(
    db: Session,
    library_folder: str,
    target_relative_path: str,
    bgm_id: int | None,
    media_type: str,
) -> None:
    """整理时识别到剧场版/OVA 文件,把它登记进"剧场版模式"列表(按 rel_path upsert)。
    rel_path 统一存正斜杠(target_relative_path 是反斜杠),与影视库扫盘/播放路径对齐。
    bgm_id 取下载时选的条目(folder.season_bgm_id);为空则跳过(没有可展示的封面来源)。"""
    if not bgm_id or not target_relative_path:
        return
    rel_path = target_relative_path.replace("\\", "/")
    row = db.query(StandaloneMedia).filter(StandaloneMedia.rel_path == rel_path).first()
    if row:
        row.library_folder = library_folder
        row.filename = os.path.basename(rel_path)
        row.bgm_id = bgm_id
        row.media_type = media_type
        row.source = "download"
    else:
        db.add(StandaloneMedia(
            library_folder=library_folder,
            rel_path=rel_path,
            filename=os.path.basename(rel_path),
            bgm_id=bgm_id,
            media_type=media_type,
            source="download",
        ))
    db.commit()


def get_current_version_at_target(db: Session, target_relative_path: str):
    """
    查某个媒体库目标路径(相对library_root的相对路径),当前已经落地的是第几版、
    以及是哪个种子占着这个位置。
    返回 (version, occupying_torrent_hash, occupying_torrent_file_count):
    occupying_torrent_file_count是"占着这个位置的种子,总共有多少个文件已经标记done"——
    等于1才说明它是单集种子,可以安全整体删除;大于1说明是合集,不能整体删除。
    """
    row = (
        db.query(RenamedFile)
        .filter(
            RenamedFile.target_relative_path == target_relative_path,
            RenamedFile.status == "done",
        )
        .order_by(RenamedFile.release_version.desc())
        .first()
    )
    if not row:
        return 0, None, 0

    done_count = (
        db.query(RenamedFile)
        .filter(RenamedFile.torrent_hash == row.torrent_hash, RenamedFile.status == "done")
        .count()
    )
    return row.release_version, row.torrent_hash, done_count


def has_done_record_at_target(db: Session, torrent_hash: str, target_relative_path: str) -> bool:
    """这个种子在这个目标位置上是不是已经有done记录了。

    按(torrent_hash, original_path)判重会在"文件已经被我们自己改过名"之后失效:
    qBittorrent返回的是改名后的新路径,而done记录里存的是改名前的原始路径,
    对不上就会被当成新文件重新处理一遍。目标位置是改名前后都不变的稳定身份,
    拿它再判一次,避免重复处理自己的产物。
    """
    if not target_relative_path:
        return False
    return (
        db.query(RenamedFile)
        .filter(
            RenamedFile.torrent_hash == torrent_hash,
            RenamedFile.target_relative_path == target_relative_path,
            RenamedFile.status == "done",
        )
        .first()
    ) is not None


def find_recorded_version_at_target(db: Session, torrent_hash: str, target_relative_path: str) -> int:
    """查这个种子在某个目标位置上已经记过的最高版本号(不限status,查不到返回0)。

    专门给"文件已经在目标位置上、但这一轮是重新解析出来的"场景兜底:改名后的
    文件名只保留字幕组和分辨率,不保留v2这类版本标记,重新解析必然退化成v1。
    上一轮改名前写的renaming占位记录里存着真实版本号,这里把它取回来,
    避免磁盘上是v2、数据库却记成v1。
    """
    if not target_relative_path:
        return 0
    row = (
        db.query(RenamedFile)
        .filter(
            RenamedFile.torrent_hash == torrent_hash,
            RenamedFile.target_relative_path == target_relative_path,
        )
        .order_by(RenamedFile.release_version.desc())
        .first()
    )
    return row.release_version if row and row.release_version else 0
