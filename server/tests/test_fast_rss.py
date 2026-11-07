import unittest
from unittest.mock import AsyncMock, patch
import asyncio
from types import SimpleNamespace

from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import create_engine
from sqlalchemy import text, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

import models
from schemas import DownloadRequest, RssSubscriptionUpdate
from sources.base import ResourceItem
from sources.registry import get_source
from services.fast_rss import analyse_candidates, episode_number, submit_candidates, SINGLE_RELEASE
from services.rss_poller import poll_subscription, _poll_subscription_items
from routers.download_submit import execute_download
from routers.rss_engine import _update_subscription, _delete_subscription


def resource(group, episode, when="2026-11-06T10:00:00Z", digest="a"):
    return ResourceItem(source="dmhy", title=f"[{group}] Test Anime - {episode} [1080p]",
                        magnet=f"magnet:?xt=urn:btih:{digest * 40}", created_at=when,
                        guid=f"{group}-{episode}")


class FastRssTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        models.Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.rule = models.SubscriptionRule(anime_title="Test Anime", keyword="Test Anime", source="dmhy",
                                            bgm_id=123, main_bgm_id=123, fast_mode=True,
                                            release_type=SINGLE_RELEASE)
        self.db.add(self.rule)
        self.db.add(models.AnimeFamilyCache(source_bgm_id=123, bgm_id=123, name="Test Anime", eps=12,
                                            season_ordinal="01"))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_earliest_per_episode_ignores_feed_order(self):
        items = [resource("B", "01", "2026-11-06T11:00:00Z"), resource("A", "01"), resource("B", "02")]
        self.assertEqual([x["status"] for x in analyse_candidates(self.db, 123, items)],
                         ["duplicate", "candidate", "candidate"])

    def test_special_collection_and_half_episode_are_not_candidates(self):
        for title in ["Test Anime - 01-12", "Test Anime - 12.5", "Test Anime SP01", "Test Anime"]:
            self.assertIsNone(episode_number(self.db, 123, title), title)

    def test_missing_season_metadata_never_reserves_raw_absolute_episode(self):
        self.assertIsNone(episode_number(self.db, 999, "Test Anime - 71"))

    def test_absolute_episode_and_season_are_normalized(self):
        row = self.db.query(models.AnimeFamilyCache).one()
        row.source_bgm_id, row.season_ordinal = 1, "02"
        self.db.commit()
        with patch("services.fast_rss._episode_offset", return_value=12):
            self.assertEqual(episode_number(self.db, 123, "Test Anime - 13"), 1)
            self.assertIsNone(episode_number(self.db, 123, "Test Anime S01E01"))

    async def test_submitted_episode_is_not_downloaded_from_later_group(self):
        with patch("services.fast_rss.qbittorrent_client.get_torrents_by_hashes", new=AsyncMock(return_value=[])), \
             patch("services.fast_rss.qbittorrent_client.add_torrent", new=AsyncMock()) as add:
            await submit_candidates(self.db, self.rule, [resource("A", "01")], "staging")
            await submit_candidates(self.db, self.rule, [resource("B", "01", digest="b")], "staging")
            self.assertEqual(add.await_count, 1)
        self.assertEqual(self.db.query(models.FastRssEpisode).count(), 1)
        self.assertEqual(self.db.query(models.RssMatchedItem).one().download_status, "added")

    async def test_timeout_keeps_winner_and_confirms_before_retry(self):
        with patch("services.fast_rss.qbittorrent_client.get_torrents_by_hashes", new=AsyncMock(return_value=[])), \
             patch("services.fast_rss.qbittorrent_client.add_torrent", new=AsyncMock(side_effect=TimeoutError("timeout"))):
            await submit_candidates(self.db, self.rule, [resource("A", "01")], "staging")
        self.assertFalse(self.db.query(models.FastRssEpisode).one().submitted)
        with patch("services.fast_rss.qbittorrent_client.get_torrents_by_hashes", new=AsyncMock(return_value=[{"hash": "a" * 40}])), \
             patch("services.fast_rss.qbittorrent_client.add_torrent", new=AsyncMock()) as add:
            await submit_candidates(self.db, self.rule, [resource("B", "01", digest="b")], "staging")
            add.assert_not_awaited()
        claim = self.db.query(models.FastRssEpisode).one()
        self.assertTrue(claim.submitted)
        self.assertIn("[A]", claim.title)

    async def test_qb_rejection_is_not_marked_submitted(self):
        with patch("services.fast_rss.qbittorrent_client.get_torrents_by_hashes", new=AsyncMock(return_value=[])), \
             patch("services.fast_rss.qbittorrent_client.add_torrent", new=AsyncMock(return_value="Fails.")):
            await submit_candidates(self.db, self.rule, [resource("A", "01")], "staging")
        self.assertFalse(self.db.query(models.FastRssEpisode).one().submitted)

    def test_database_rejects_second_fast_rule_even_when_paused(self):
        self.rule.enabled = False
        self.db.commit()
        self.db.add(models.SubscriptionRule(anime_title="Test", keyword="Test", source="nyaa", bgm_id=123, fast_mode=True))
        with self.assertRaises(IntegrityError):
            self.db.commit()
        self.db.rollback()

    async def test_concurrent_refresh_only_submits_one_candidate(self):
        adapter = SimpleNamespace(poll=AsyncMock(return_value=[resource("A", "01"), resource("B", "01", digest="b")]))
        with patch("services.rss_poller.get_source", return_value=adapter), \
             patch("services.rss_poller.upsert_anime_folder"), \
             patch("services.rss_season_complete.refresh_season_complete", new=AsyncMock()), \
             patch("services.fast_rss.qbittorrent_client.get_torrents_by_hashes", new=AsyncMock(return_value=[])), \
             patch("services.fast_rss.qbittorrent_client.add_torrent", new=AsyncMock()) as add:
            await asyncio.gather(poll_subscription(self.db, self.rule, "staging"),
                                 poll_subscription(self.db, self.rule, "staging"))
            self.assertEqual(add.await_count, 1)

    async def test_normal_poller_ignores_fast_episode_history(self):
        self.db.add(models.FastRssEpisode(subscription_id=self.rule.id, episode=1, title="old", magnet="old", guid="old", submitted=True))
        normal = models.SubscriptionRule(anime_title="Test Anime", keyword="Test Anime", source="dmhy", bgm_id=123,
                                          main_bgm_id=123, fast_mode=False)
        self.db.add(normal)
        self.db.commit()
        adapter = SimpleNamespace(poll=AsyncMock(return_value=[resource("B", "01", digest="b")]))
        with patch("services.rss_poller.get_source", return_value=adapter), \
             patch("services.rss_poller.upsert_anime_folder"), \
             patch("services.rss_poller.qbittorrent_client.add_torrent", new=AsyncMock()) as add:
            await _poll_subscription_items(self.db, normal, "staging")
            add.assert_awaited_once()

    def test_other_download_history_does_not_block_fast_candidates(self):
        self.db.add(models.RssMatchedItem(subscription_id=999, guid="old", title=resource("A", "01").title, download_status="added"))
        self.db.commit()
        self.assertEqual(analyse_candidates(self.db, 123, [resource("A", "01")], self.rule.id)[0]["status"], "candidate")

    async def test_normal_subscription_can_coexist_and_fast_cannot_be_recreated(self):
        with patch("routers.download_submit.resolve_series_identity", new=AsyncMock(return_value=("Test Anime", 123, None))), \
             patch("routers.download_submit.clear_group_override"):
            request = DownloadRequest(anime_title="Test Anime", keyword="Test Anime", source="dmhy", bgm_id=123,
                                      subscribe=True, fansub_name="A", items=[])
            await execute_download(request, BackgroundTasks(), self.db)
            self.assertEqual(self.db.query(models.SubscriptionRule).count(), 2)
            request.fast_mode, request.fansub_name, request.release_type = True, None, SINGLE_RELEASE
            with self.assertRaises(HTTPException) as caught:
                await execute_download(request, BackgroundTasks(), self.db)
            self.assertEqual(caught.exception.status_code, 409)

    def test_edit_preserves_history_and_enforces_fixed_filters(self):
        self.db.add(models.FastRssEpisode(subscription_id=self.rule.id, episode=1, title="old", magnet="old", guid="old", submitted=True))
        self.db.commit()
        request = RssSubscriptionUpdate(keyword="Test Anime", quality="1080p", release_type=SINGLE_RELEASE)
        _update_subscription(self.rule.id, request, BackgroundTasks(), self.db)
        self.assertTrue(self.db.query(models.FastRssEpisode).one().submitted)
        request.fansub_name = "A"
        with self.assertRaises(HTTPException):
            _update_subscription(self.rule.id, request, BackgroundTasks(), self.db)

    async def test_normal_rss_can_create_and_edit_collection_subscription(self):
        request = DownloadRequest(anime_title="Test Anime", keyword="Test Anime", source="dmhy", bgm_id=123,
                                  subscribe=True, release_type="合集/全集(完结)", items=[])
        with patch("routers.download_submit.resolve_series_identity", new=AsyncMock(return_value=("Test Anime", 123, None))), \
             patch("routers.download_submit.clear_group_override"):
            await execute_download(request, BackgroundTasks(), self.db)
        normal = self.db.query(models.SubscriptionRule).filter_by(fast_mode=False).one()
        self.assertEqual(normal.release_type, "合集/全集(完结)")
        _update_subscription(normal.id, RssSubscriptionUpdate(keyword="Test Anime", release_type=None), BackgroundTasks(), self.db)
        self.assertIsNone(normal.release_type)
        _update_subscription(normal.id, RssSubscriptionUpdate(keyword="Test Anime", release_type="合集/全集(完结)"),
                             BackgroundTasks(), self.db)
        self.assertEqual(normal.release_type, "合集/全集(完结)")

    def test_delete_resets_subscription_history_and_slot(self):
        self.db.add(models.FastRssEpisode(subscription_id=self.rule.id, episode=1, title="old", magnet="old", guid="old", submitted=True))
        self.db.commit()
        _delete_subscription(self.rule.id, self.db)
        self.assertEqual(self.db.query(models.FastRssEpisode).count(), 0)
        self.db.add(models.SubscriptionRule(anime_title="Test", keyword="Test", source="dmhy", bgm_id=123, fast_mode=True))
        self.db.commit()

    def test_feed_publication_time_is_available_for_both_xml_sources(self):
        xml = b'''<rss xmlns:nyaa="https://nyaa.si/xmlns/nyaa"><channel><item>
        <title>Test Anime - 01</title><guid>one</guid><pubDate>Fri, 06 Nov 2026 10:00:00 +0000</pubDate>
        <enclosure url="magnet:?xt=urn:btih:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"/>
        <nyaa:infoHash>aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa</nyaa:infoHash></item></channel></rss>'''
        for name in ["dmhy", "nyaa"]:
            self.assertIsNotNone(get_source(name).parse_feed(xml)[0].created_at)

    def test_existing_database_is_upgraded_without_changing_normal_rules(self):
        import db_migrate
        legacy = create_engine("sqlite:///:memory:")
        try:
            with legacy.begin() as conn:
                conn.execute(text("CREATE TABLE subscription_rule (id INTEGER PRIMARY KEY, bgm_id INTEGER, anime_title VARCHAR, keyword VARCHAR, source VARCHAR)"))
                conn.execute(text("INSERT INTO subscription_rule VALUES (1, 123, 'Test', 'Test', 'dmhy')"))
            with patch.object(db_migrate, "engine", legacy), patch.object(db_migrate, "_refresh_stale_caches"):
                db_migrate.upgrade_db()
                db_migrate.upgrade_db()
            with legacy.connect() as conn:
                self.assertEqual(conn.execute(text("SELECT fast_mode FROM subscription_rule")).scalar(), 0)
            self.assertIn("fast_rss_episode", inspect(legacy).get_table_names())
            self.assertIn("uq_fast_rss_bgm", {x["name"] for x in inspect(legacy).get_indexes("subscription_rule")})
        finally:
            legacy.dispose()
