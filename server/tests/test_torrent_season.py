"""同一暂存文件夹里的后一季，不能把前一季整理成自己的季号。"""
import asyncio
import base64
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
import rename_engine
from services.bgm_series_cache import resolve_tv_season_ordinal_cached
from services.library_repair import _bgm_id_by_torrent_hash
from services.organize import _preview_files_for_organize
from services.staging import resolve_torrent_season_bgm_id

SEASON1 = "a368d558232fd93a23d03d862ccfa33736662680"
SEASON2 = "955a17f5b275a94ddb19032d481e75220ddeacd0"
ROOT = 357962
SEASON1_BGM = 357962
SEASON2_BGM = 486054


def _b32(hex_hash: str) -> str:
    return base64.b32encode(bytes.fromhex(hex_hash)).decode().rstrip("=")


class TorrentSeasonTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        models.Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.db.add(models.AnimeFolder(
            staging_folder=r"D:\download\魔都精兵的奴隶 [bgm-357962]",
            anime_title="魔都精兵的奴隶",
            main_bgm_id=ROOT,
            season_bgm_id=SEASON2_BGM,
            auto_rename=True,
        ))
        for bgm_id, ordinal, name, date in (
            (SEASON1_BGM, "01", "魔都精兵的奴隶", "2024-01-04"),
            (SEASON2_BGM, "02", "魔都精兵的奴隶 第二季", "2026-01-08"),
        ):
            self.db.add(models.AnimeFamilyCache(
                source_bgm_id=ROOT,
                bgm_id=bgm_id,
                name=name,
                date=date,
                platform="TV",
                eps=12,
                total_episodes=12,
                season_ordinal=ordinal,
            ))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_manual_download_keeps_its_own_season_after_a_later_submit(self):
        self.db.add(models.DownloadTask(
            anime_title="魔都精兵的奴隶",
            original_title="[H-Enc] Mato Seihei no Slave (BDRip 1080p HEVC FLAC)",
            magnet=f"magnet:?xt=urn:btih:{SEASON1}",
            status="已推送",
            bgm_id=SEASON1_BGM,
            info_hash=SEASON1,
        ))
        self.db.commit()

        season_id = resolve_torrent_season_bgm_id(self.db, SEASON1, fallback=SEASON2_BGM)
        self.assertEqual(season_id, SEASON1_BGM)
        ordinal = asyncio.run(
            resolve_tv_season_ordinal_cached(self.db, season_id, ROOT)
        )
        self.assertEqual(ordinal, "01")

        preview = rename_engine.preview_rename_file(
            anime_title="魔都精兵的奴隶",
            file_name="Mato Seihei no Slave - 01.mkv",
            torrent_title="[H-Enc] Mato Seihei no Slave (BDRip 1080p HEVC FLAC)",
            library_root=r"D:\lib",
            bgm_id=ROOT,
            season_hint="魔都精兵的奴隶",
            season_ordinal=ordinal,
            platform="TV",
            episode_offset=0,
            season_total_eps=12,
        )
        self.assertEqual(
            preview["target_filename"],
            "魔都精兵的奴隶 - S01E01 [H-Enc][1080p].mkv",
        )
        self.assertIn("Season 01", preview["target_relative_path"])

    def test_latest_manual_submit_wins_when_the_same_hash_was_sent_twice(self):
        self.db.add(models.DownloadTask(
            anime_title="魔都精兵的奴隶", original_title="first",
            magnet=f"magnet:?xt=urn:btih:{SEASON1}", status="已推送",
            bgm_id=SEASON2_BGM, info_hash=SEASON1,
        ))
        self.db.add(models.DownloadTask(
            anime_title="魔都精兵的奴隶", original_title="second",
            magnet=f"magnet:?xt=urn:btih:{SEASON1}", status="已推送",
            bgm_id=SEASON1_BGM, info_hash=SEASON1,
        ))
        self.db.commit()
        self.assertEqual(
            resolve_torrent_season_bgm_id(self.db, SEASON1, fallback=SEASON2_BGM),
            SEASON1_BGM,
        )

    def test_old_task_without_a_recorded_season_uses_the_folder(self):
        self.db.add(models.DownloadTask(
            anime_title="魔都精兵的奴隶", original_title="old",
            magnet=f"magnet:?xt=urn:btih:{SEASON1}", status="已推送",
        ))
        self.db.commit()
        self.assertEqual(
            resolve_torrent_season_bgm_id(self.db, SEASON1, fallback=SEASON2_BGM),
            SEASON2_BGM,
        )

    def test_rss_rule_supplies_the_season(self):
        rule = models.SubscriptionRule(
            anime_title="魔都精兵的奴隶 第二季", bgm_id=SEASON2_BGM,
            main_bgm_id=ROOT, keyword="第二季", source="nyaa",
        )
        self.db.add(rule)
        self.db.commit()
        self.db.add(models.RssMatchedItem(
            subscription_id=rule.id, guid="guid-2", info_hash=SEASON2,
            title="[H-Enc] Mato Seihei no Slave 2", magnet=f"magnet:?xt=urn:btih:{SEASON2}",
            download_status="added",
        ))
        self.db.commit()
        self.assertEqual(
            resolve_torrent_season_bgm_id(self.db, SEASON2.upper(), fallback=SEASON1_BGM),
            SEASON2_BGM,
        )
        self.assertEqual(_bgm_id_by_torrent_hash(self.db, SEASON2), SEASON2_BGM)

    def test_base32_magnet_without_info_hash_still_matches_the_rss_rule(self):
        rule = models.SubscriptionRule(
            anime_title="魔都精兵的奴隶", bgm_id=SEASON1_BGM,
            main_bgm_id=ROOT, keyword="奴隶", source="animegarden",
        )
        self.db.add(rule)
        self.db.commit()
        self.db.add(models.RssMatchedItem(
            subscription_id=rule.id, guid="guid-b32", info_hash=None,
            title="[H-Enc] Mato Seihei no Slave",
            magnet=f"magnet:?xt=urn:btih:{_b32(SEASON1)}&dn=slave",
            download_status="added",
        ))
        self.db.commit()
        self.assertEqual(
            resolve_torrent_season_bgm_id(self.db, SEASON1, fallback=SEASON2_BGM),
            SEASON1_BGM,
        )

    def test_manual_record_wins_over_an_rss_rule_for_the_same_hash(self):
        rule = models.SubscriptionRule(
            anime_title="魔都精兵的奴隶 第二季", bgm_id=SEASON2_BGM,
            main_bgm_id=ROOT, keyword="第二季", source="nyaa",
        )
        self.db.add(rule)
        self.db.commit()
        self.db.add(models.RssMatchedItem(
            subscription_id=rule.id, guid="guid-same", info_hash=SEASON1,
            title="slave", magnet=f"magnet:?xt=urn:btih:{SEASON1}",
            download_status="added",
        ))
        self.db.add(models.DownloadTask(
            anime_title="魔都精兵的奴隶", original_title="manual",
            magnet=f"magnet:?xt=urn:btih:{SEASON1}", status="已推送",
            bgm_id=SEASON1_BGM, info_hash=SEASON1,
        ))
        self.db.commit()
        self.assertEqual(
            resolve_torrent_season_bgm_id(self.db, SEASON1, fallback=SEASON2_BGM),
            SEASON1_BGM,
        )

    def test_filename_season_still_overrides_the_torrent_season(self):
        folder = self.db.query(models.AnimeFolder).one()
        plans = _preview_files_for_organize(
            self.db,
            folder,
            r"D:\lib",
            {
                "episode_offset": 0,
                "season_total_eps": 12,
                "season_hint": "魔都精兵的奴隶",
                "platform": "TV",
                "season_ordinal": "01",
            },
            {"hash": SEASON1, "name": "[H-Enc] Mato Seihei no Slave S1+S2"},
            ["Mato Seihei no Slave - S02E03.mkv"],
        )
        self.assertEqual(len(plans), 1)
        self.assertIn("Season 02", plans[0]["preview"]["target_relative_path"])
        self.assertIn("S02E03", plans[0]["preview"]["target_filename"])


if __name__ == "__main__":
    unittest.main()
