"""
后台常驻循环:定期扫描qBittorrent里anime-hub分类下已完成、还没整理过的种子,
按AnimeFolder表记录的番名/bgm_id/auto_rename,把文件从暂存区搬进媒体库
(需要拆分改名的话,顺便逐文件重命名)。

这是唯一一个不属于任何具体页面、纯后台运行的模块,
所以单独放在services里,而不是挂在某个routers文件下。
"""
import asyncio
import os

import bangumi_client

from sqlalchemy.orm import Session

import config_store
import qbittorrent_client
import rename_engine
from database import SessionLocal
from models import AnimeFamilyCache, AnimeFolder, LocalMedia, RenamedFile
from services.bgm_series_cache import build_season_episode_table, resolve_tv_season_ordinal_cached
from services.common import get_setting
from services.staging import (
    ORGANIZE_TAG,
    UNKNOWN_TAG,
    find_recorded_version_at_target,
    get_current_version_at_target,
    has_done_record_at_target,
    upsert_renamed_file,
    upsert_standalone_media,
)


def _norm_path(path: str) -> str:
    """路径比较统一走这一层归一化(大小写/分隔符/末尾斜杠不敏感),
    跟qbittorrent_client.get_torrents_by_save_path是同一套标准。"""
    return os.path.normcase(os.path.normpath(path)) if path else ""


def _same_relative_path(a: str, b: str) -> bool:
    """比较两个种子内部相对路径是不是同一个。qBittorrent返回的路径用正斜杠,
    rename_engine算出来的用反斜杠,统一后再比;Windows文件名大小写不敏感。"""
    if not a or not b:
        return False
    return a.replace("\\", "/").casefold() == b.replace("\\", "/").casefold()


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
    # 预检:整理第一步就要连本地 qBittorrent(下面 get_completed_torrents),qB 没起时
    # 直接给出明确提示并跳过本轮,而不是让它在下面抛出隐晦的 "All connection attempts failed"。
    # qB 连接不走代理,这里探不通就是 qB 本身没启动/端口不对,与代理无关。
    if not await qbittorrent_client.test_connection():
        print("[ORGANIZE] qBittorrent 未连接,跳过本轮整理")
        return
    db = SessionLocal()
    try:
        torrents = await qbittorrent_client.get_completed_torrents(
            "anime-hub", ORGANIZE_TAG, UNKNOWN_TAG
        )
        for torrent in torrents:
            try:
                await _organize_single_torrent(db, torrent)
            except Exception as e:
                # 回滚会话:上面任何一步的数据库错误都会让Session进入需要rollback
                # 的状态,不清掉的话本轮后面所有种子的查询都会连带失败。
                db.rollback()
                print(f"[ORGANIZE] 处理种子失败 hash={torrent.get('hash')}: {e}")
    finally:
        db.close()


def _invalidate_episode_count(db: Session, folder_name: str) -> None:
    """新文件整理进这个文件夹了,"未看集数"角标缓存的集数不再准。不在这里精确±算——
    版本冲突/替换/跳过这些语义混在一起,从外面猜"这次到底净增了几个文件"容易算错;
    直接置空,交给下次GET /library/scan的后台补课,或用户下次打开这部番详情页时
    顺手重新扫一遍(见routers/library.py)。"""
    media = db.query(LocalMedia).filter(LocalMedia.folder_name == folder_name).first()
    if media and media.episode_file_count is not None:
        media.episode_file_count = None
        db.commit()


def _anime_target_root(library_root: str, folder: AnimeFolder) -> str:
    """这部番在媒体库里的根目录。整理时setLocation搬到这里,
    Season/Other子目录再靠renameFile的相对路径实现。"""
    anime_folder_name = rename_engine.build_anime_folder_name(folder.anime_title, folder.main_bgm_id)
    return f"{library_root.rstrip(chr(92)).rstrip('/')}\\{anime_folder_name}"


def _match_folder_by_save_path(db: Session, library_root: str, save_path: str) -> tuple[AnimeFolder | None, bool]:
    """按种子当前的save_path反查它属于哪条AnimeFolder记录。
    返回(folder, already_at_target):already_at_target为True代表这个种子已经被
    搬到媒体库了,本轮不用再setLocation。

    为什么不能只按staging_folder匹配:setLocation一旦生效,种子的save_path就永久
    变成了媒体库的target_root,再也匹配不上staging_folder。凡是"已经搬完家、但那
    一轮没走到打hub-organized标签"的种子(搬家慢导致等待超时、或者中途进程被杀),
    下一轮就会反查不到记录、被判成未知目录,永远不会被改名,而且不会自愈——
    这里补上按target_root的二次反查,把这类种子重新认领回来接着整理。
    """
    target = _norm_path(save_path)
    if not target:
        return None, False

    folders = db.query(AnimeFolder).all()
    # 暂存目录优先:正常情况(还没搬家)应该命中这一条,语义最准。
    for folder in folders:
        if _norm_path(folder.staging_folder) == target:
            return folder, False
    for folder in folders:
        if _norm_path(_anime_target_root(library_root, folder)) == target:
            return folder, True
    return None, False


async def _resolve_organize_context(db: Session, torrent: dict) -> dict | None:
    """校验这个种子有没有对应的AnimeFolder记录、读库目录设置、拼出目标根路径——
    "整理到底往哪放"的最基础信息,不管是否需要改名内部文件都要用到。
    没有对应记录("未知暂存目录")时返回None,调用方负责打hub-unknown标签。
    """
    library_root = get_setting(db, "library_root", config_store.DEFAULTS["library_root"])
    folder, already_at_target = _match_folder_by_save_path(
        db, library_root, torrent.get("save_path", "")
    )
    if not folder:
        return None

    return {
        "folder": folder,
        "library_root": library_root,
        "target_root": _anime_target_root(library_root, folder),
        "already_at_target": already_at_target,
    }


async def _resolve_season_context(db: Session, folder: AnimeFolder) -> dict:
    """算这部番这一季的集数偏移量/季度文字提示/平台/季度序号,对种子内所有文件
    都一样,只需算一次复用。特意不放进_resolve_organize_context里一起算——只有
    确定要改名内部文件(auto_rename开着)时才用得到,"只搬家不改名"的种子
    (BD原盘/合集光盘)在_organize_single_torrent里会提前return,不会走到这里,
    省一轮不必要的Bangumi请求。
    """
    season_hint = folder.anime_title
    platform = None
    if folder.season_bgm_id:
        try:
            season_detail = await bangumi_client.get_subject_detail(folder.season_bgm_id)
            season_hint = season_detail.get("name_cn") or season_detail.get("name") or folder.anime_title
            platform = season_detail.get("platform")
        except Exception as e:
            print(f"[ORGANIZE] 查询季度提示失败,按番名继续: {e}")

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

    # 这一季自己的总集数 + 前序各季累计集数:用来把字幕组的跨季绝对编号换算回季内编号
    # (实测咒术回战第二季发的是25~47,而这一季自己只有23集,正确结果是01~23),
    # 详见rename_engine._normalize_absolute_episode。resolve_tv_season_ordinal_cached
    # 上面已经把这个家族写进AnimeFamilyCache了,这里直接读表,不额外发网络请求。
    episode_offset = 0
    season_total_eps = None
    if season_ordinal and folder.main_bgm_id:
        season_info = build_season_episode_table(db, folder.main_bgm_id).get(season_ordinal)
        if season_info:
            episode_offset = season_info["episode_offset"]
            season_total_eps = season_info["eps"]

    return {
        "episode_offset": episode_offset,
        "season_total_eps": season_total_eps,
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
            season_total_eps=season_context["season_total_eps"],
            season_ordinal=season_context["season_ordinal"],
            platform=season_context["platform"],
        )
        # 上面按original_path的判重在"这个文件已经被我们自己改过名"之后会失效
        # (qB返回的是改名后的新路径,done记录里存的是改名前的原始路径),
        # 再按目标位置判一次,避免把自己的产物当成新文件重复处理一遍。
        if has_done_record_at_target(db, torrent_hash, preview["target_relative_path"]):
            continue
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


def _all_plans_superseded(db: Session, torrent_hash: str, plans: list[dict]) -> bool:
    """种子内每个待改名文件在媒体库目标位置都已被"等于或更高版本"占着时返回True——
    这种整个种子都追不上现状,不该再搬进媒体库(典型场景:字幕组发了v2、v2先下完
    并整理入库,原始v1才姗姗下完)。只要有任意一个文件是 proceed / delete_then_proceed
    / skip_collection,就返回False,交回_apply_organize_plan逐文件处理。

    纯决策,复用_resolve_version_conflict,不另写版本比较。在_move_to_library之前调用,
    避免低版本种子的原始文件被setLocation搬进番剧库根目录、判跳过后又没人清理。
    """
    for item in plans:
        preview = item["preview"]
        if not preview["relative_path"]:      # 剧场版等不共享anime_root,交给下游判断
            return False
        if item.get("collision_with"):        # 同种子内目标撞车,交给下游按failed处理
            return False
        decision = _resolve_version_conflict(
            db, torrent_hash, preview["target_relative_path"], preview["release_version"]
        )
        if decision["action"] != "skip":
            return False
    return True


def _resolve_standalone_bgm_id(
    db: Session, main_bgm_id: int | None, season_bgm_id: int | None,
    media_type: str, original_file_name: str,
) -> int | None:
    """剧场版/OVA登记进"剧场版模式"时该用哪个bgm_id当封面/标题/简介来源——不能
    直接信season_bgm_id:那是这次提交下载时选的条目(常见场景是RSS订阅追更TV
    正片季度,season_bgm_id是TV正片自己的bgm_id),不是这一部具体剧场版/OVA自己
    在Bangumi上独立的条目,直接拿去查AnimeCatalog会显示成"系列家族根"的封面/简介,
    不是这一部剧场版自己的(实测案例:哆啦A梦剧场版被自动登记后显示的是TV正片
    的封面)。

    但"不能直接信"不等于"永远不信":如果season_bgm_id本身就是一个跟这次media_type
    对得上的独立条目(剧场版文件配剧场版条目/OVA文件配OVA条目),那它就是用户下载
    时亲手选的这一部,是最强的信号,直接采信、不再猜。文件名那边(season_hint)本来
    就一直在用它,这样卡片分组和文件名保证走同一个答案,不会各说各话——
    实测案例:『机动战士高达 闪光的哈萨维 喀耳刻的魔女』下载时选的就是喀耳刻的
    魔女(243430),文件名也确实是对的,却因为下面的子串匹配猜成了『闪光的哈萨维』
    (243429),在剧场版页面上跟前作并成了同一张卡。
    上面docstring担心的RSS场景不受影响:那时season_bgm_id是TV正片条目,platform
    对不上,这一层不会命中,照常往下走猜测。

    season_bgm_id指望不上时(典型就是RSS追更),再从AnimeFamilyCache里按platform
    筛出这部番家族下的剧场版/OVA成员——resolve_tv_season_ordinal_cached在这之前
    已经把整个家族缓存写好了,这里基本不需要额外发网络请求。只有一个候选时直接用;
    多个候选时拿原始文件名(种子内部的真实文件名,通常带着这一部作品自己的标题)
    反过来做子串匹配,确定是家族里的哪一部。一个候选都匹配不上(或者压根没有
    main_bgm_id可查)就退回season_bgm_id——好歹还有个"同系列"的封面兜底,比完全
    没有强,用户也可以在剧场版模式页面里手动"重新分组"纠正。
    """
    platform = "剧场版" if media_type == "movie" else "OVA"

    if main_bgm_id and season_bgm_id:
        picked = (
            db.query(AnimeFamilyCache)
            .filter(
                AnimeFamilyCache.source_bgm_id == main_bgm_id,
                AnimeFamilyCache.bgm_id == season_bgm_id,
                AnimeFamilyCache.platform == platform,
            )
            .first()
        )
        if picked:
            return season_bgm_id

    if not main_bgm_id:
        return season_bgm_id

    candidates = (
        db.query(AnimeFamilyCache)
        .filter(AnimeFamilyCache.source_bgm_id == main_bgm_id, AnimeFamilyCache.platform == platform)
        .all()
    )
    if not candidates:
        return season_bgm_id
    if len(candidates) == 1:
        return candidates[0].bgm_id

    # 命中的候选里取标题最长的那个,而不是第一个命中的。系列剧场版的标题天然
    # 前缀嵌套(『闪光的哈萨维』/『闪光的哈萨维 喀耳刻的魔女』/『闪光的哈萨维 第3部』),
    # 一个带完整副标题的文件名会同时包含前作的短标题,"第一个命中就返回"必然被
    # 笼统的那个抢走;标题越长越具体,取最长的才是真正对应的那一部。
    lowered = original_file_name.lower()
    matched = [
        c for c in candidates
        if (c.name or "").strip() and (c.name or "").strip().lower() in lowered
    ]
    if matched:
        return max(matched, key=lambda c: len((c.name or "").strip())).bgm_id

    return season_bgm_id


async def _apply_organize_plan(
    db: Session, torrent_hash: str, all_paths: list[str], plans: list[dict],
    library_folder: str = "", source_bgm_id: int | None = None, main_bgm_id: int | None = None,
) -> None:
    """执行_preview_files_for_organize算好的改名预览:逐个处理版本冲突判定、
    实际调用qBittorrent renameFile/deleteTorrent、字幕跟随改名,并把每个文件的
    结果写回RenamedFile表。library_folder/source_bgm_id/main_bgm_id 用于把剧场版/OVA
    文件登记进"剧场版模式"列表——source_bgm_id是下载时选的条目(folder.season_bgm_id),
    main_bgm_id是家族根(folder.main_bgm_id),两个一起交给_resolve_standalone_bgm_id
    去判断该用哪个bgm_id当这张卡的封面来源,不是无条件直接用source_bgm_id。"""
    for item in plans:
        video_path = item["video_path"]
        preview = item["preview"]

        # parsed_episode=="??"只对正片(TV季度SxxExx编号)才算真失败——rename_engine
        # 的剧场版/OVA/特典(extra)分支本来就不靠集数拼文件名,"没解析出集数"对它们是
        # 正常情况,不是错误,不该被这里拦下来跳过(实测案例:哆啦A梦剧场版,anitopy
        # 从文件名里根本找不到集数,media_type正确识别成movie,却被这条检查当失败
        # 处理,文件搬到了番剧根目录,但没改名、没登记进"剧场版模式"列表)。
        episode_required = preview["media_type"] not in ("movie", "ova", "extra")
        if not preview["relative_path"] or (episode_required and preview["parsed_episode"] == "??"):
            upsert_renamed_file(
                db, torrent_hash, video_path, status="failed",
                error="无法解析集数,已跳过、原样保留",
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

        this_version = preview["release_version"]

        # 文件已经在目标位置上了:上一轮renameFile已经成功,只是还没来得及写库
        # (进程被杀/数据库报错)。不用再改一次名,补上done记录就行。
        # 版本号取"已记录过的"和"这次算出来的"里更大的那个——改名后的文件名只保留
        # 字幕组和分辨率,v2这类版本标记已经没了,重新解析必然退化成v1,
        # 而改名前写的renaming占位记录里存着真实版本号。
        if _same_relative_path(video_path, preview["relative_path"]):
            recorded_version = find_recorded_version_at_target(
                db, torrent_hash, preview["target_relative_path"]
            )
            upsert_renamed_file(
                db, torrent_hash, video_path, status="done",
                target=preview["target_relative_path"],
                release_version=max(recorded_version, this_version),
            )
            print(f"[ORGANIZE] 文件已在目标位置,补记完成状态: hash={torrent_hash} file={video_path}")
            continue

        # 每次移动前,先查一下这个目标位置当前落地的是第几版——
        # 不管v1/v2是同一个种子里出现,还是分两次RSS下载分别抓到,
        # 都用同一套判断:版本不比现有的高,就跳过,不覆盖已经更好的版本。
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

        # 改名之前先落一条占位记录,把真实版本号存下来。改名成功、写done之前
        # 如果进程被杀,文件名里的v2标记就永久丢了(目标文件名只保留字幕组+分辨率),
        # 靠这条记录下一轮才能把版本号还原回去,不会磁盘上是v2、库里记成v1。
        upsert_renamed_file(
            db, torrent_hash, video_path, status="renaming",
            target=preview["target_relative_path"], release_version=this_version,
        )
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
            # 剧场版/OVA:登记进"剧场版模式"列表,封面尽量用这一部自己的bgm_id
            # (不是无条件用下载时选的条目,见_resolve_standalone_bgm_id说明)。
            if preview.get("media_type") in ("movie", "ova"):
                resolved_bgm_id = _resolve_standalone_bgm_id(
                    db, main_bgm_id, source_bgm_id,
                    preview["media_type"], preview["original_file_name"],
                )
                upsert_standalone_media(
                    db, library_folder, preview["target_relative_path"],
                    resolved_bgm_id, preview["media_type"],
                )
        except Exception as e:
            upsert_renamed_file(
                db, torrent_hash, video_path, status="failed",
                error=str(e), release_version=this_version,
            )
            print(f"[ORGANIZE] 改名失败: hash={torrent_hash} file={video_path} error={e}")


async def _maybe_cleanup_staging_folder(staging_folder_path: str) -> None:
    """这个种子搬完之后,顺手检查一下暂存目录是不是已经没有任何种子还占着了。

    暂存目录是按(下载暂存根,番名,bgm_id)算出来的共享路径(见services/staging.py::
    staging_folder)——不管是单次下载还是RSS轮询,同一部番的所有种子都用这同一个
    save_path,同一部番可能还有别的种子在这个目录里没搬完(还在下载中的、RSS
    后续陆续投递进来的),不能这个种子一搬完就直接删。必须先确认qBittorrent里
    这个save_path下已经一个种子都不剩(不限完成状态——还在下载中的也算数),
    并且物理目录本身也确实是空的,两者都满足才删;用rmdir而不是rmtree,
    一旦判断有误(目录其实非空)就让它报错而不是把东西删掉。
    """
    if not staging_folder_path:
        return
    try:
        remaining = await qbittorrent_client.get_torrents_by_save_path(staging_folder_path)
    except Exception as e:
        print(f"[ORGANIZE] 查询暂存目录占用情况失败,跳过清理: {staging_folder_path}: {e}")
        return
    if remaining:
        return
    try:
        if os.path.isdir(staging_folder_path) and not os.listdir(staging_folder_path):
            os.rmdir(staging_folder_path)
            print(f"[ORGANIZE] 暂存目录已清空,已删除: {staging_folder_path}")
    except OSError as e:
        print(f"[ORGANIZE] 删除空暂存目录失败: {staging_folder_path}: {e}")


async def _wait_for_location_change(
    torrent_hash: str, expected_location: str, timeout: float = 30.0
) -> bool:
    """setLocation对qBittorrent来说是异步操作(尤其涉及大文件/跨盘搬家时),API调用
    本身返回成功不代表文件已经落地新位置——这里轮询确认save_path真的变成了目标
    位置,确认不了就不能继续往下走改名/打标签,否则会出现"标了完成、文件其实还在
    老地方"这种永久卡住的假阳性(实测案例:『你们先走我断后』S01E06,种子被
    renameFile改好名、打上hub-organized标签,但save_path始终还是暂存目录)。

    等待用退避式而不是固定5秒:BD原盘/合集这类几十GB的跨盘搬家远不止5秒,
    等待窗口太短会让绝大多数大种子每轮都超时。真的超时也不再是死局——
    _match_folder_by_save_path能按target_root把已经搬完的种子重新认领回来。
    """
    target = _norm_path(expected_location)
    delay = 1.0
    waited = 0.0
    last_seen = ""
    while True:
        torrents = await qbittorrent_client.get_torrents_by_hashes([torrent_hash])
        if torrents:
            last_seen = torrents[0].get("save_path", "")
            if _norm_path(last_seen) == target:
                return True
        if waited >= timeout:
            print(
                f"[ORGANIZE] 等待搬家生效超时({timeout:.0f}s): hash={torrent_hash} "
                f"期望={expected_location} 当前={last_seen}"
            )
            return False
        await asyncio.sleep(delay)
        waited += delay
        delay = min(delay * 1.5, 5.0)


async def _move_to_library(torrent_hash: str, target_root: str, already_at_target: bool) -> bool:
    """把种子搬到媒体库根目录并确认落地。已经在目标位置上(上一轮搬完但没走完
    后续步骤)时直接返回成功,不重复发setLocation。"""
    if already_at_target:
        return True
    try:
        await qbittorrent_client.set_torrent_location(torrent_hash, target_root)
    except Exception as e:
        print(f"[ORGANIZE] 搬家失败 hash={torrent_hash}: {e}")
        return False
    return await _wait_for_location_change(torrent_hash, target_root)


async def _finish_torrent(torrent_hash: str, staging_folder_path: str) -> None:
    """收尾:先清理暂存目录,再打hub-organized标签。

    顺序不能反——打完标签的种子下一轮就被get_completed_torrents过滤掉了,
    要是崩在"打完标签、还没清理"之间,那个空暂存目录再也不会有人来收。
    _maybe_cleanup_staging_folder内部本身有"还有种子占着就不删"的保护,
    提前调用是安全的。
    """
    await _maybe_cleanup_staging_folder(staging_folder_path)
    await qbittorrent_client.add_torrent_tags(torrent_hash, ORGANIZE_TAG)


async def _organize_single_torrent(db: Session, torrent: dict) -> None:
    torrent_hash = torrent["hash"]

    context = await _resolve_organize_context(db, torrent)
    if context is None:
        print(f"[ORGANIZE] 未知暂存目录,跳过整理: {torrent.get('save_path', '')}")
        await qbittorrent_client.add_torrent_tags(torrent_hash, UNKNOWN_TAG)
        return

    folder = context["folder"]
    target_root = context["target_root"]
    # 暂存目录取AnimeFolder记录里的,不取种子当前的save_path——种子可能已经搬到
    # 媒体库了(already_at_target),那时候save_path指的是媒体库目录,
    # 拿去给_maybe_cleanup_staging_folder会去删媒体库文件夹。
    staging_folder_path = folder.staging_folder

    # 用户关闭了自动改名(比如BD原盘/合集光盘):只搬家,不碰内部文件结构
    if not folder.auto_rename:
        if not await _move_to_library(torrent_hash, target_root, context["already_at_target"]):
            print(f"[ORGANIZE] 搬家未确认生效(未改名模式),留到下一轮重试: hash={torrent_hash}")
            return
        _invalidate_episode_count(db, os.path.basename(target_root.replace("\\", "/").rstrip("/")))
        await _finish_torrent(torrent_hash, staging_folder_path)
        return

    try:
        files = await qbittorrent_client.get_torrent_files(torrent_hash)
    except Exception as e:
        print(f"[ORGANIZE] 获取文件列表失败 hash={torrent_hash}: {e}")
        return

    all_paths = [f["name"] for f in files]
    video_paths = [p for p in all_paths if p.rsplit(".", 1)[-1].lower() in rename_engine.VIDEO_EXTS]

    # 改名预览是纯计算 + Bangumi 查询,不依赖种子是否已搬库——特意放在 _move_to_library
    # 之前算好,好让下面的整体版本预检能在"文件还没进媒体库"时就拦下追不上现状的种子。
    # 集数偏移量、季度提示文本、季度序号对种子内所有文件都一样,算一次复用即可。
    season_context = await _resolve_season_context(db, folder)
    plans = _preview_files_for_organize(
        db, folder, context["library_root"], season_context, torrent, video_paths
    )
    # 库文件夹名 = 目标根目录的最后一段(剧场版/OVA 登记进"剧场版模式"时要用)。
    library_folder = os.path.basename(target_root.replace("\\", "/").rstrip("/"))

    # 整体版本预检:这个种子里每个待处理文件在媒体库目标位置都已被等于/更高版本占着
    # (典型:字幕组发了v2,v2先下完并整理入库,原始v1才姗姗下完)。此时不搬库——否则
    # v1的原始文件会被setLocation搬进番剧库根目录,随后判跳过、打标签,永久遗留污染媒体库。
    if plans and not context["already_at_target"] and _all_plans_superseded(db, torrent_hash, plans):
        for item in plans:
            upsert_renamed_file(
                db, torrent_hash, item["video_path"], status="skipped",
                error="目标位置已有等于或更高版本,未搬入媒体库",
                release_version=item["preview"]["release_version"],
            )
        print(f"[ORGANIZE] 种子整体版本落后于媒体库现状,不搬库、直接结束: hash={torrent_hash}")
        await _finish_torrent(torrent_hash, staging_folder_path)
        return

    # setLocation是种子级别的,只能设一个根位置——先统一挪到这部番的媒体库根目录,
    # 具体Season/Other子目录分类,靠下面逐个文件调用renameFile的相对路径实现。
    # 注意:解析失败的文件也会被这一步带过来,只是留在根目录、文件名不变,
    # 不会被强行塞进错误的Season子路径,方便事后人工核查。
    if not await _move_to_library(torrent_hash, target_root, context["already_at_target"]):
        print(f"[ORGANIZE] 搬家未确认生效,本轮不继续处理,留到下一轮重试: hash={torrent_hash}")
        return  # 不改名、不打标签。下一轮就算搬家已经完成、save_path变成了媒体库目录,
                # _match_folder_by_save_path也能按target_root反查回这条AnimeFolder记录,
                # 接着往下整理,不会卡死在"未知暂存目录"

    await _apply_organize_plan(
        db, torrent_hash, all_paths, plans, library_folder, folder.season_bgm_id, folder.main_bgm_id
    )
    _invalidate_episode_count(db, library_folder)

    # 不管有没有文件失败,都打标签结束这一轮——失败文件的状态已经记进RenamedFile表,
    # 不会无限重试刷日志;后续要重跑,可以手动清掉这个标签(或者以后加个"重试"按钮)。
    await _finish_torrent(torrent_hash, staging_folder_path)
