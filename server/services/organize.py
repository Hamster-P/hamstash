"""
后台常驻循环:定期扫描qBittorrent里anime-hub分类下已完成、还没整理过的种子,
按AnimeFolder表记录的番名/bgm_id/auto_rename,把文件从暂存区搬进媒体库
(需要拆分改名的话,顺便逐文件重命名)。

这是唯一一个不属于任何具体页面、纯后台运行的模块,
所以单独放在services里,而不是挂在某个routers文件下。
"""
import asyncio
import bangumi_client

from sqlalchemy.orm import Session

import config_store
import qbittorrent_client
import rename_engine
from database import SessionLocal
from models import AnimeFolder, RenamedFile
from services.bgm_series_cache import resolve_tv_season_ordinal_cached
from services.common import get_setting
from services.staging import ORGANIZE_TAG, get_current_version_at_target, upsert_renamed_file


async def organize_loop() -> None:
    """
    轮询间隔从设置读取,每轮都重新读一次,
    在设置页改完点保存不用重启容器就能生效。
    """
    while True:
        try:
            db = SessionLocal()
            try:
                interval = int(
                    get_setting(
                        db,
                        "rename_poll_interval_seconds",
                        config_store.DEFAULTS["rename_poll_interval_seconds"],
                    )
                )
            finally:
                db.close()
        except Exception:
            interval = int(config_store.DEFAULTS["rename_poll_interval_seconds"])

        try:
            await _organize_completed_torrents()
        except Exception as e:
            print(f"[ORGANIZE] 轮询整理任务出错: {e}")

        await asyncio.sleep(interval)


async def _organize_completed_torrents() -> None:
    db = SessionLocal()
    try:
        torrents = await qbittorrent_client.get_completed_torrents("anime-hub", ORGANIZE_TAG)
        for torrent in torrents:
            try:
                await _organize_single_torrent(db, torrent)
            except Exception as e:
                print(f"[ORGANIZE] 处理种子失败 hash={torrent.get('hash')}: {e}")
    finally:
        db.close()


async def _organize_single_torrent(db: Session, torrent: dict) -> None:
    torrent_hash = torrent["hash"]
    staging_folder_path = torrent.get("save_path", "")

    folder = (
        db.query(AnimeFolder)
        .filter(AnimeFolder.staging_folder == staging_folder_path)
        .first()
    )
    if not folder:
        print(f"[ORGANIZE] 未知暂存目录,跳过整理: {staging_folder_path}")
        await qbittorrent_client.add_torrent_tags(torrent_hash, "hub-unknown")
        return

    library_root = get_setting(db, "library_root", config_store.DEFAULTS["library_root"])
    anime_folder_name = rename_engine.build_anime_folder_name(folder.anime_title, folder.main_bgm_id)
    target_root = f"{library_root.rstrip(chr(92)).rstrip('/')}\\{anime_folder_name}"

    # 用户关闭了自动改名(比如BD原盘/合集光盘):只搬家,不碰内部文件结构
    if not folder.auto_rename:
        try:
            await qbittorrent_client.set_torrent_location(torrent_hash, target_root)
        except Exception as e:
            print(f"[ORGANIZE] 搬家失败(未改名模式) hash={torrent_hash}: {e}")
            return
        await qbittorrent_client.add_torrent_tags(torrent_hash, ORGANIZE_TAG)
        return

    try:
        files = await qbittorrent_client.get_torrent_files(torrent_hash)
    except Exception as e:
        print(f"[ORGANIZE] 获取文件列表失败 hash={torrent_hash}: {e}")
        return

    all_paths = [f["name"] for f in files]
    video_paths = [p for p in all_paths if p.rsplit(".", 1)[-1].lower() in rename_engine.VIDEO_EXTS]

    # setLocation是种子级别的,只能设一个根位置——先统一挪到这部番的媒体库根目录,
    # 具体Season/Other子目录分类,靠下面逐个文件调用renameFile的相对路径实现。
    # 注意:解析失败的文件也会被这一步带过来,只是留在根目录、文件名不变,
    # 不会被强行塞进错误的Season子路径,方便事后人工核查。
    try:
        await qbittorrent_client.set_torrent_location(torrent_hash, target_root)
    except Exception as e:
        print(f"[ORGANIZE] 搬家失败 hash={torrent_hash}: {e}")
        return
    # 集数偏移量、季度提示文本、季度序号,对这个种子内所有文件都一样,算一次复用即可,
    # 不用每个文件都重新查一遍Bangumi。
    episode_offset = 0
    season_hint = folder.anime_title
    platform = None
    if folder.season_bgm_id:
        try:
            # 先注释 episode_offset = await bangumi_client.resolve_episode_offset(folder.season_bgm_id)
            season_detail = await bangumi_client.get_subject_detail(folder.season_bgm_id)
            season_hint = season_detail.get("name_cn") or season_detail.get("name") or folder.anime_title
            platform = season_detail.get("platform")
        except Exception as e:
            print(f"[ORGANIZE] 计算集数偏移量/季度提示失败,按0偏移继续: {e}")

    # 季度序号完全不看种子标题文本,只信Bangumi关联图谱本身(platform+集数+首播日期)——
    # 详见bangumi_family.resolve_family_season_map的文档,修的是"旁支正片/剧场版TV重制版
    # 撞车覆盖Season 01"的问题。查过一次的家族会缓存进AnimeFamilyCache表(见
    # resolve_tv_season_ordinal_cached),同一个系列反复下载不用每次都重新爬关联图谱。
    # 查询失败(网络问题/这一部本来就不在真季名单里)时返回None,rename_engine那边会
    # 按platform分流到剧场版/Season 00,不会退回旧的文本猜测逻辑。
    season_ordinal = None
    if folder.season_bgm_id and folder.main_bgm_id:
        try:
            season_ordinal = await resolve_tv_season_ordinal_cached(
                db, folder.season_bgm_id, folder.main_bgm_id
            )
        except Exception as e:
            print(f"[ORGANIZE] 计算季度序号失败,按platform分流继续: {e}")

    for video_path in video_paths:
        existing = (
            db.query(RenamedFile)
            .filter(
                RenamedFile.torrent_hash == torrent_hash,
                RenamedFile.original_path == video_path,
            )
            .first()
        )
        if existing and existing.status == "done":
            continue

        file_name = video_path.rsplit("/", 1)[-1]
        preview = rename_engine.preview_rename_file(
            anime_title=folder.anime_title,
            file_name=file_name,
            torrent_title=torrent.get("name", ""),
            library_root=library_root,
            bgm_id=folder.main_bgm_id,
            season_hint=season_hint,
            episode_offset=episode_offset,
            season_ordinal=season_ordinal,
            platform=platform,
        )

        if not preview["relative_path"] or preview["parsed_episode"] == "??":
            upsert_renamed_file(
                db,
                torrent_hash,
                video_path,
                status="failed",
                error="无法解析集数,或属于不共享媒体库根目录的媒体类型(如剧场版),已跳过、原样保留",
            )
            print(f"[ORGANIZE] 解析失败,跳过: hash={torrent_hash} file={video_path}")
            continue
        # 每次移动前,先查一下这个目标位置当前落地的是第几版——
        # 不管v1/v2是同一个种子里出现,还是分两次RSS下载分别抓到,
        # 都用同一套判断:版本不比现有的高,就跳过,不覆盖已经更好的版本。
        current_version, old_hash, old_torrent_file_count = get_current_version_at_target(
            db, preview["target_full_path"]
        )
        this_version = preview["release_version"]
        if this_version <= current_version:
            upsert_renamed_file(
                db, torrent_hash, video_path, status="skipped",
                error=f"目标位置已有v{current_version},当前文件是v{this_version},未替换",
                release_version=this_version,
            )
            print(
                f"[ORGANIZE] 版本不比现有高,跳过: hash={torrent_hash} "
                f"file={video_path} (v{this_version} <= v{current_version})"
            )
            continue

        if current_version > 0 and old_hash and old_hash != torrent_hash:
            # 目标位置被另一个种子占着(典型场景:RSS前后两次下载,v1/v2是两个独立种子)。
            # qBittorrent没有"只删一个文件、不影响所属种子"的API,只能整个删种子。
            # 只有确认旧种子是"单集种子"(只有这一个文件标记done)才安全删除,
            # 否则可能连累合集里其他还在用的集数,这种情况不自动处理,留给人工判断。
            if old_torrent_file_count <= 1:
                try:
                    await qbittorrent_client.delete_torrent(old_hash, delete_files=True)
                    print(f"[ORGANIZE] 已删除被更高版本取代的旧种子: {old_hash}")
                except Exception as e:
                    upsert_renamed_file(
                        db, torrent_hash, video_path, status="failed",
                        error=f"删除旧版本种子失败,未替换: {e}", release_version=this_version,
                    )
                    print(f"[ORGANIZE] 删除旧种子失败,跳过替换: hash={old_hash} error={e}")
                    continue
            else:
                upsert_renamed_file(
                    db, torrent_hash, video_path, status="skipped",
                    error=f"目标位置被合集种子({old_hash})占用,该种子还有其他集数在用,未自动删除,需人工处理",
                    release_version=this_version,
                )
                print(f"[ORGANIZE] 旧种子是合集,不自动删除,需人工处理: {old_hash}")
                continue
    
        try:
            await qbittorrent_client.rename_torrent_file(
                torrent_hash, video_path, preview["relative_path"]
            )
            for sub_path in rename_engine.find_sibling_subtitles(video_path, all_paths):
                sub_ext = sub_path.rsplit(".", 1)[-1]
                sub_relative = preview["relative_path"].rsplit(".", 1)[0] + f".{sub_ext}"
                try:
                    await qbittorrent_client.rename_torrent_file(torrent_hash, sub_path, sub_relative)
                except Exception as e:
                    print(f"[ORGANIZE] 字幕改名失败 {sub_path}: {e}")
            upsert_renamed_file(
                db, torrent_hash, video_path, status="done",
                target=preview["target_full_path"], release_version=this_version,
             
            )
        except Exception as e:
            upsert_renamed_file(
                db, torrent_hash, video_path, status="failed",
                error=str(e), release_version=this_version,
            )
            print(f"[ORGANIZE] 改名失败: hash={torrent_hash} file={video_path} error={e}")

    # 不管有没有文件失败,都打标签结束这一轮——失败文件的状态已经记进RenamedFile表,
    # 不会无限重试刷日志;后续要重跑,可以手动清掉这个标签(或者以后加个"重试"按钮)。
    await qbittorrent_client.add_torrent_tags(torrent_hash, ORGANIZE_TAG)
