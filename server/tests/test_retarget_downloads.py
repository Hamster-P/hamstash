"""改归属后，进行中的下载要改记新的系列根。"""
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
import rename_engine
from services.staging import retarget_season_downloads, upsert_anime_folder


class RetargetDownloadsTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        models.Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_only_this_season_moves_to_the_new_root(self):
        upsert_anime_folder(
            self.db, r"D:\download\旧系列 [bgm-1]", "旧系列", 1, 22, True,
        )
        upsert_anime_folder(
            self.db, r"D:\download\旧系列 [bgm-1]\other", "旧系列", 1, 21, True,
        )
        self.db.add(models.SubscriptionRule(
            anime_title="旧系列", bgm_id=22, main_bgm_id=1, keyword="第三季", source="dmhy",
        ))
        self.db.add(models.SubscriptionRule(
            anime_title="别的番", bgm_id=99, main_bgm_id=99, keyword="别的", source="dmhy",
        ))
        self.db.commit()

        retarget_season_downloads(self.db, 22, 22, "第三季")

        moved = (
            self.db.query(models.AnimeFolder)
            .filter(models.AnimeFolder.season_bgm_id == 22)
            .one()
        )
        self.assertEqual(moved.main_bgm_id, 22)
        self.assertEqual(moved.anime_title, "第三季")
        self.assertEqual(moved.staging_folder, r"D:\download\旧系列 [bgm-1]")
        self.assertEqual(
            rename_engine.build_anime_folder_name(moved.anime_title, moved.main_bgm_id),
            "第三季 [bgm-22]",
        )
        stayed = (
            self.db.query(models.AnimeFolder)
            .filter(models.AnimeFolder.season_bgm_id == 21)
            .one()
        )
        self.assertEqual(stayed.main_bgm_id, 1)
        rule = self.db.query(models.SubscriptionRule).filter_by(bgm_id=22).one()
        self.assertEqual(rule.main_bgm_id, 22)
        self.assertEqual(rule.anime_title, "第三季")
        other = self.db.query(models.SubscriptionRule).filter_by(bgm_id=99).one()
        self.assertEqual(other.main_bgm_id, 99)
        self.assertEqual(other.anime_title, "别的番")


if __name__ == "__main__":
    unittest.main()
