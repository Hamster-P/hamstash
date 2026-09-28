"""RSS 轮询结束时判断：这一季是不是每一集都下完了。

只看订阅自己推下去的种子，以及这些种子整理落地后的文件。
不看命中记录上的「已下载」——那只表示已经交给 qBittorrent。
"""
import base64
import os
import re

from sqlalchemy import func
from sqlalchemy.orm import Session

import bangumi_client
import qbittorrent_client
import rename_engine
from models import AnimeFamilyCache, RenamedFile, RssMatchedItem, SubscriptionRule

# 普通季大约 12 或 24 集。上百集的是柯南、海贼王这类超长篇，不提示删订阅。
LONG_RUNNING_MIN_EPS = 100
_BTIH_RE = re.compile(r"btih:([a-fA-F0-9]{40}|[A-Za-z2-7]{32})", re.IGNORECASE)


def _btih_to_hex(token: str) -> str | None:
    """磁力里的 btih 有两种：40 位十六进制，或 32 位 Base32。

    动漫花园、动漫花园聚合进来的资源经常是 Base32。
    只认十六进制会把这些种子全部丢掉，整理落地的文件也对不上。
    """
    if re.fullmatch(r"[a-fA-F0-9]{40}", token):
        return token.lower()
    if re.fullmatch(r"[A-Za-z2-7]{32}", token):
        try:
            raw = base64.b32decode(token.upper())
        except Exception:
            return None
        if len(raw) == 20:
            return raw.hex()
    return None


def usable_episode_count(eps: int | None) -> int | None:
    """正片集数。0、空、或达到超长篇门槛时，调用方不显示提示。"""
    if not eps or eps <= 0:
        return None
    if eps >= LONG_RUNNING_MIN_EPS:
        return None
    return eps


def torrent_hash_of(info_hash: str | None, magnet: str | None) -> str | None:
    if info_hash:
        parsed = _btih_to_hex(info_hash)
        if parsed:
            return parsed
    if magnet:
        found = _BTIH_RE.search(magnet)
        if found:
            return _btih_to_hex(found.group(1))
    return None


def episode_index(file_name: str, season_eps: int, episode_offset: int) -> int | None:
    """文件名里的集数，换算成这一季的 1..N。半集、区间、解析不出的都不算。"""
    parsed = rename_engine.parse_file_episode(os.path.basename(file_name))
    if not parsed:
        return None
    try:
        raw = float(parsed)
    except ValueError:
        return None
    if not raw.is_integer():
        return None
    normalized = rename_engine._normalize_absolute_episode(raw, episode_offset, season_eps)
    if not float(normalized).is_integer():
        return None
    number = int(normalized)
    if 1 <= number <= season_eps:
        return number
    return None


def season_is_complete(have: set[int], season_eps: int) -> bool:
    """1 到 N 每一集都在，才算下完。只有第 N 集不算。"""
    return all(number in have for number in range(1, season_eps + 1))


def _is_video(name: str) -> bool:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return ext in rename_engine.VIDEO_EXTS


def _file_finished(progress: float | None) -> bool:
    return (progress or 0) >= 0.999


async def _downloaded_episode_numbers(
    db: Session, rule: SubscriptionRule, season_eps: int, episode_offset: int
) -> set[int]:
    items = (
        db.query(RssMatchedItem)
        .filter(
            RssMatchedItem.subscription_id == rule.id,
            RssMatchedItem.download_status == "added",
        )
        .all()
    )
    hashes = []
    for item in items:
        digest = torrent_hash_of(item.info_hash, item.magnet)
        if digest and digest not in hashes:
            hashes.append(digest)

    found: set[int] = set()
    if hashes:
        try:
            torrents = await qbittorrent_client.get_torrents_by_hashes(hashes)
        except Exception as exc:
            print(f"[RSS引擎] 读取下载明细失败 subscription={rule.id}: {exc}")
            torrents = []
        present = {str(t.get("hash") or "").lower() for t in torrents}
        for digest in hashes:
            if digest not in present:
                continue
            try:
                files = await qbittorrent_client.get_torrent_files(digest)
            except Exception as exc:
                print(f"[RSS引擎] 读取种子文件失败 hash={digest}: {exc}")
                continue
            for entry in files:
                name = entry.get("name") or ""
                if not _is_video(name) or not _file_finished(entry.get("progress")):
                    continue
                number = episode_index(name, season_eps, episode_offset)
                if number is not None:
                    found.add(number)

        renamed = (
            db.query(RenamedFile)
            .filter(func.lower(RenamedFile.torrent_hash).in_(hashes), RenamedFile.status == "done")
            .all()
        )
        for row in renamed:
            for path in (row.target_relative_path, row.original_path):
                if not path or not _is_video(path):
                    continue
                number = episode_index(path, season_eps, episode_offset)
                if number is not None:
                    found.add(number)
    return found


def _episode_offset(db: Session, row: AnimeFamilyCache | None) -> int:
    if row is None or not row.season_ordinal or row.source_bgm_id is None:
        return 0
    from services.bgm_series_cache import build_season_episode_table

    info = build_season_episode_table(db, row.source_bgm_id).get(row.season_ordinal)
    if not info:
        return 0
    return int(info.get("episode_offset") or 0)


async def _season_eps(db: Session, bgm_id: int | None) -> tuple[int | None, int] | None:
    """返回 (集数, 偏移)。集数是 None 表示确定不用提示。

    整个函数返回 None 表示这次没查到，调用方保持上次的判断。
    """
    if not bgm_id:
        return None, 0
    row = db.query(AnimeFamilyCache).filter(AnimeFamilyCache.bgm_id == bgm_id).first()
    # 缓存里正片集数是 0、但总集数已经上百，就是还在连载的超长篇。
    if row and not (row.eps or 0) and (row.total_episodes or 0) >= LONG_RUNNING_MIN_EPS:
        return None, 0
    cached = usable_episode_count(row.eps if row else None)
    offset = _episode_offset(db, row)
    if cached:
        return cached, offset
    try:
        detail = await bangumi_client.get_subject_detail(bgm_id)
    except Exception as exc:
        print(f"[RSS引擎] 读取季度集数失败 bgm_id={bgm_id}: {exc}")
        return None
    return usable_episode_count(detail.get("eps")), offset


async def refresh_season_complete(db: Session, rule: SubscriptionRule) -> None:
    """轮询收尾时算一次。确定没有集数就清掉提示。查询失败则保持上次结果。"""
    looked_up = await _season_eps(db, rule.bgm_id)
    if looked_up is None:
        return
    eps, offset = looked_up
    if eps is None:
        rule.season_complete = False
    else:
        have = await _downloaded_episode_numbers(db, rule, eps, offset)
        rule.season_complete = season_is_complete(have, eps)
    db.commit()
