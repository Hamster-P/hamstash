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


async def _resolve_organize_context(db: Session, torrent: dict) -> dict | None:
    """校验这个种子有没有对应的AnimeFolder记录、读库目录设置、拼出目标根路径——
    "整理到底往哪放"的最基础信息,不管是否需要改名内部文件都要用到。
    没有对应记录("未知暂存目录")时返回None,调用方负责打hub-unknown标签。
    """
    staging_folder_path = torrent.get("save_path", "")
    folder = (
        db.query(AnimeFolder)
        .filter(AnimeFolder.staging_folder == staging_folder_path)
        .first()
    )
    if not folder:
        return None

    library_root = get_setting(db, "library_root", config_store.DEFAULTS["library_root"])
    anime_folder_name = rename_engine.build_anime_folder_name(folder.anime_title, folder.main_bgm_id)
    target_root = f"{library_root.rstrip(chr(92)).rstrip('/')}\\{anime_folder_name}"

    return {
        "folder": folder,
        "library_root": library_root,
        "target_root": target_root,
    }


async def _resolve_season_context(db: Session, folder: AnimeFolder) -> dict:
    """算这部番这一季的集数偏移量/季度文字提示/平台/季度序号,对种子内所有文件
    都一样,只需算一次复用。特意不放进_resolve_organize_context里一起算——只有
    确定要改名内部文件(auto_rename开着)时才用得到,"只搬家不改名"的种子
    (BD原盘/合集光盘)在_organize_single_torrent里会提前return,不会走到这里,
    省一轮不必要的Bangumi请求。
    """
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

    return {
        "episode_offset": episode_offset,
        "season_hint": season_hint,
        "platform": platform,
        "season_ordinal": season_ordinal,
    }


def _preview_files_for_organize(
    db: Session,
    folder: AnimeFolder,
    library_root: str,
    season_context: dict,
    torrent: dict,
    video_paths: list[str],
) -> list[dict]:
    """逐个视频文件算改名预览(纯计算,不做任何网络/文件I/O),跳过数据库里已经
    标记done的文件。返回[{"video_path", "preview"}, ...],交给
    _apply_organize_plan实际执行改名。
    """
    torrent_hash = torrent["hash"]
    plans = []
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
            season_hint=season_context["season_hint"],
            episode_offset=season_context["episode_offset"],
            season_ordinal=season_context["season_ordinal"],
            platform=season_context["platform"],
        )
        plans.append({"video_path": video_path, "preview": preview})
    return _guard_target_path_collisions(plans)


def _guard_target_path_collisions(plans: list[dict]) -> list[dict]:
    """同一个种子内如果有两个文件算出了相同的target_full_path(改名规则没考虑到的
    冷门场景),原样改名会导致后处理的文件把先处理的物理覆盖掉、静默丢数据。
    这里做最后一道安全网:按video_path排序保证结果确定性,撞车时只放行第一个,
    其余的直接标记failed、留在原地不动,不参与实际改名——不是修复根因,是防止
    "根因还没覆盖到的场景"再次造成不可逆的文件丢失。
    """
    seen: dict[str, str] = {}
    result = []
    for item in sorted(plans, key=lambda p: p["video_path"]):
        target = item["preview"]["target_full_path"]
        if target in seen:
            item["collision_with"] = seen[target]
        else:
            seen[target] = item["video_path"]
        result.append(item)
    return result


def _resolve_version_conflict(
    db: Session, torrent_hash: str, target_relative_path: str, this_version: int
) -> dict:
    """查目标位置当前落地的是第几版、被哪个种子占着,判断这次改名该继续、跳过,
    还是要先删掉被取代的旧种子——纯决策,不做实际删除/改名I/O,返回一个动作
    描述给_apply_organize_plan执行:
    - {"action": "proceed"}                              正常改名
    - {"action": "skip", "error": ...}                    版本不比现有高
    - {"action": "skip_collection", "error": ...}         旧种子是合集,需人工处理
    - {"action": "delete_then_proceed", "old_hash": ...}  需先删掉被取代的单集旧种子
    """
    current_version, old_hash, old_torrent_file_count = get_current_version_at_target(
        db, target_relative_path
    )

    if this_version <= current_version:
        return {
            "action": "skip",
            "error": f"目标位置已有v{current_version},当前文件是v{this_version},未替换",
        }

    if current_version > 0 and old_hash and old_hash != torrent_hash:
        # 目标位置被另一个种子占着(典型场景:RSS前后两次下载,v1/v2是两个独立种子)。
        # qBittorrent没有"只删一个文件、不影响所属种子"的API,只能整个删种子。
        # 只有确认旧种子是"单集种子"(只有这一个文件标记done)才安全删除,
        # 否则可能连累合集里其他还在用的集数,这种情况不自动处理,留给人工判断。
        if old_torrent_file_count <= 1:
            return {"action": "delete_then_proceed", "old_hash": old_hash}
        return {
            "action": "skip_collection",
            "error": f"目标位置被合集种子({old_hash})占用,该种子还有其他集数在用,未自动删除,需人工处理",
        }

    return {"action": "proceed"}


async def _apply_organize_plan(
    db: Session, torrent_hash: str, all_paths: list[str], plans: list[dict]
) -> None:
    """执行_preview_files_for_organize算好的改名预览:逐个处理版本冲突判定、
    实际调用qBittorrent renameFile/deleteTorrent、字幕跟随改名,并把每个文件的
    结果写回RenamedFile表。"""
    for item in plans:
        video_path = item["video_path"]
        preview = item["preview"]

        if not preview["relative_path"] or preview["parsed_episode"] == "??":
            upsert_renamed_file(
                db, torrent_hash, video_path, status="failed",
                error="无法解析集数,或属于不共享媒体库根目录的媒体类型(如剧场版),已跳过、原样保留",
            )
            print(f"[ORGANIZE] 解析失败,跳过: hash={torrent_hash} file={video_path}")
            continue

        if item.get("collision_with"):
            upsert_renamed_file(
                db, torrent_hash, video_path, status="failed",
                error=f"目标路径与同种子内另一个文件({item['collision_with']})冲突,需人工核查改名规则,已跳过、原样保留",
            )
            print(f"[ORGANIZE] 目标路径撞车,跳过: hash={torrent_hash} file={video_path}")
            continue

        # 每次移动前,先查一下这个目标位置当前落地的是第几版——
        # 不管v1/v2是同一个种子里出现,还是分两次RSS下载分别抓到,
        # 都用同一套判断:版本不比现有的高,就跳过,不覆盖已经更好的版本。
        this_version = preview["release_version"]
        decision = _resolve_version_conflict(db, torrent_hash, preview["target_relative_path"], this_version)

        if decision["action"] == "skip":
            upsert_renamed_file(
                db, torrent_hash, video_path, status="skipped",
                error=decision["error"], release_version=this_version,
            )
            print(
                f"[ORGANIZE] 版本不比现有高,跳过: hash={torrent_hash} "
                f"file={video_path} ({decision['error']})"
            )
            continue

        if decision["action"] == "skip_collection":
            upsert_renamed_file(
                db, torrent_hash, video_path, status="skipped",
                error=decision["error"], release_version=this_version,
            )
            print(f"[ORGANIZE] 旧种子是合集,不自动删除,需人工处理: {decision['old_hash']}")
            continue

        if decision["action"] == "delete_then_proceed":
            old_hash = decision["old_hash"]
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
                target=preview["target_relative_path"], release_version=this_version,
            )
        except Exception as e:
            upsert_renamed_file(
                db, torrent_hash, video_path, status="failed",
                error=str(e), release_version=this_version,
            )
            print(f"[ORGANIZE] 改名失败: hash={torrent_hash} file={video_path} error={e}")


async def _organize_single_torrent(db: Session, torrent: dict) -> None:
    torrent_hash = torrent["hash"]

    context = await _resolve_organize_context(db, torrent)
    if context is None:
        print(f"[ORGANIZE] 未知暂存目录,跳过整理: {torrent.get('save_path', '')}")
        await qbittorrent_client.add_torrent_tags(torrent_hash, "hub-unknown")
        return

    folder = context["folder"]
    target_root = context["target_root"]

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
    season_context = await _resolve_season_context(db, folder)

    plans = _preview_files_for_organize(
        db, folder, context["library_root"], season_context, torrent, video_paths
    )
    await _apply_organize_plan(db, torrent_hash, all_paths, plans)

    # 不管有没有文件失败,都打标签结束这一轮——失败文件的状态已经记进RenamedFile表,
    # 不会无限重试刷日志;后续要重跑,可以手动清掉这个标签(或者以后加个"重试"按钮)。
    await qbittorrent_client.add_torrent_tags(torrent_hash, ORGANIZE_TAG)
