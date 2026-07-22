"""
共通工具函数与常量。

被多个路由模块(download / rss / settings)以及后台整理任务复用,
避免每个页面各自的路由文件里重复实现同一套小工具。
"""
import re

from sqlalchemy.orm import Session

import rename_engine
from models import AnimeFolder, AppSetting, RenamedFile

RSS_FOLDER = "anime-hub"  # 我们在qBittorrent的RSS订阅目录树里统一挂在这个文件夹下
ORGANIZE_TAG = "hub-organized"  # 打上这个标签代表后台整理任务已经处理过这个种子


def get_setting(db: Session, key: str, default: str) -> str:
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    return row.value if row and row.value else default


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
        row.target_full_path = target
        row.error = error
        row.release_version = release_version
    else:
        row = RenamedFile(
            torrent_hash=torrent_hash,
            original_path=original_path,
            status=status, 
            target_full_path=target, 
            error=error,
            release_version=release_version,
        )
        db.add(row)
    db.commit()

async def resolve_series_identity(bgm_id: int | None, fallback_title: str):
    """
    返回 (folder_title, main_bgm_id, season_hint_text):
    - folder_title/main_bgm_id: 系列最早一部的名字/ID,决定文件夹归属,
      不管提交时是第几季,同系列都落进同一个文件夹。
    - season_hint_text: 这一季自己在Bangumi的官方名字,专门给季度文字判断用
      (根条目的名字通常不带"第几季"这种信息)。
    """
    import bangumi_client  # 延迟导入,避免模块加载顺序问题

    if not bgm_id:
        return fallback_title, None, fallback_title

    try:
        season_detail = await bangumi_client.get_subject_detail(bgm_id)
        season_title = season_detail.get("name_cn") or season_detail.get("name") or fallback_title
    except Exception:
        season_title = fallback_title

    try:
        main_id = await bangumi_client.resolve_root_subject_id(bgm_id)
        main_detail = await bangumi_client.get_subject_detail(main_id)
        folder_title = main_detail.get("name_cn") or main_detail.get("name") or season_title
    except Exception:
        main_id, folder_title = bgm_id, season_title

    return folder_title, main_id, season_title

def sanitize_path_segment(text: str) -> str:
    """去掉qBittorrent RSS路径分隔符\\和/以及首尾空白,避免层级错乱。"""
    return re.sub(r"[\\/]+", "_", text).strip() or "未命名"

def get_current_version_at_target(db: Session, target_full_path: str):
    """
    查某个媒体库目标路径,当前已经落地的是第几版、以及是哪个种子占着这个位置。
    返回 (version, occupying_torrent_hash, occupying_torrent_file_count):
    occupying_torrent_file_count是"占着这个位置的种子,总共有多少个文件已经标记done"——
    等于1才说明它是单集种子,可以安全整体删除;大于1说明是合集,不能整体删除。
    """
    row = (
        db.query(RenamedFile)
        .filter(RenamedFile.target_full_path == target_full_path, RenamedFile.status == "done")
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
