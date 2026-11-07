"""极速订阅的候选选择和订阅内集数占位；不读取普通下载历史。"""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from sqlalchemy.exc import IntegrityError

import models
import rename_engine
import qbittorrent_client
from sources.base import ResourceItem
from services.rss_season_complete import torrent_hash_of, _episode_offset

SINGLE_RELEASE = "单集(追更)"


def episode_number(db, bgm_id: int, title: str) -> int | None:
    if rename_engine.is_collection_title(title) or rename_engine.classify_media_type(title) != "tv":
        return None
    parsed = rename_engine.parse_file_episode(title)
    if not parsed:
        return None
    raw = float(parsed)
    if not raw.is_integer() or raw <= 0:
        return None
    row = db.query(models.AnimeFamilyCache).filter_by(bgm_id=bgm_id).first()
    if row is None:
        # Do not persist raw absolute episode numbers before season metadata is ready.
        return None
    explicit_season = rename_engine.parse_file_season(title)
    if explicit_season and row.season_ordinal:
        if int(explicit_season) != int(row.season_ordinal):
            return None
    raw = rename_engine._normalize_absolute_episode(raw, _episode_offset(db, row), row.eps)
    if raw <= 0 or (row.eps and raw > row.eps):
        return None
    return int(raw)


def publication_time(value: str | None) -> float:
    if value:
        try:
            try:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                dt = parsedate_to_datetime(value)
            return dt.replace(tzinfo=dt.tzinfo or timezone.utc).timestamp()
        except (ValueError, TypeError, OverflowError):
            pass
    return float("inf")


def analyse_candidates(db, bgm_id: int, items: list[ResourceItem], subscription_id=None) -> list[dict]:
    claims = {row.episode: row for row in db.query(models.FastRssEpisode).filter_by(
        subscription_id=subscription_id).all()} if subscription_id else {}
    episodes = [episode_number(db, bgm_id, item.title) for item in items]
    winners = {}
    # Missing publication times use stable feed/search order (first observed candidate).
    for i in sorted(range(len(items)), key=lambda i: (publication_time(items[i].created_at), i)):
        episode = episodes[i]
        if episode is not None and torrent_hash_of(None, items[i].magnet) and episode not in claims:
            winners.setdefault(episode, i)
    return [{
        "episode": episode,
        "status": ("unknown" if episode is None or not torrent_hash_of(None, items[i].magnet) else
                   "submitted" if episode in claims and claims[episode].submitted else
                   "pending" if episode in claims else
                   "candidate" if winners.get(episode) == i else "duplicate"),
    } for i, episode in enumerate(episodes)]


async def submit_candidates(db, rule, items: list[ResourceItem], save_path: str) -> None:
    """先持久化占位再请求 qB；结果不明时只重试同一个磁力，不切换字幕组。"""
    analysis = analyse_candidates(db, rule.bgm_id, items, rule.id)
    for item, result in zip(items, analysis):
        if result["status"] != "candidate":
            continue
        db.add(models.FastRssEpisode(
            subscription_id=rule.id, episode=result["episode"], title=item.title,
            magnet=item.magnet, guid=item.guid or item.magnet, submitted=False,
        ))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()  # Another refresh already reserved this episode.

    pending = db.query(models.FastRssEpisode).filter_by(subscription_id=rule.id, submitted=False).all()
    released = False
    for claim in pending:
        # Editing filters keeps submitted history, but may replace an unsubmitted choice.
        from services.rss_poller import _criteria_from_rule
        from sources.base import matches_criteria
        item = ResourceItem(source=rule.source, title=claim.title, magnet=claim.magnet)
        if not matches_criteria(item, _criteria_from_rule(rule)):
            # Check for an accepted request before releasing an uncertain reservation.
            digest = torrent_hash_of(None, claim.magnet)
            try:
                present = await qbittorrent_client.get_torrents_by_hashes([digest]) if digest else None
            except Exception:
                continue
            if present:
                claim.submitted = True
                db.commit()
            elif present is not None:
                db.delete(claim)
                db.commit()
                released = True
            continue
        try:
            digest = torrent_hash_of(None, claim.magnet)
            if not digest:
                continue
            present = await qbittorrent_client.get_torrents_by_hashes([digest])
            if not present:
                response = await qbittorrent_client.add_torrent(magnet=claim.magnet, save_path=save_path)
                if isinstance(response, str) and response.strip().lower() in {"fails.", "fails"}:
                    raise RuntimeError("qBittorrent 未接受下载任务")
            claim.submitted = True
            record = db.query(models.RssMatchedItem).filter_by(subscription_id=rule.id, guid=claim.guid).first()
            if record is None:
                record = models.RssMatchedItem(subscription_id=rule.id, guid=claim.guid, title=claim.title)
                db.add(record)
            record.info_hash, record.magnet = digest, claim.magnet
            record.download_status, record.error = "added", None
            db.commit()
        except Exception as exc:
            db.rollback()
            rule.last_error = f"第 {claim.episode} 集提交结果待确认，将重试同一资源: {exc}"
            db.commit()
    if released:
        await submit_candidates(db, rule, items, save_path)
    elif not db.query(models.FastRssEpisode).filter_by(subscription_id=rule.id, submitted=False).first():
        rule.last_error = None
        db.commit()
