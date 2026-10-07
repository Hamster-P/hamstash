"""删掉媒体库文件时，指向它的整理记录要一起去掉。"""
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from routers.library import delete_renamed_files_at
from services.staging import upsert_renamed_file


class DeleteRenamedFilesTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        models.Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_episode_delete_removes_only_that_file(self):
        upsert_renamed_file(
            self.db, "hash", "01.mkv", status="done",
            target="番 [bgm-1]\\Season 01\\番 - S01E01.mkv",
        )
        upsert_renamed_file(
            self.db, "hash", "02.mkv", status="done",
            target="番 [bgm-1]/Season 01/番 - S01E02.mkv",
        )
        gone = delete_renamed_files_at(
            self.db, "番 [bgm-1]/Season 01/番 - S01E01.mkv", whole_folder=False,
        )
        self.db.commit()
        self.assertEqual(gone, 1)
        left = self.db.query(models.RenamedFile).all()
        self.assertEqual(len(left), 1)
        self.assertEqual(left[0].original_path, "02.mkv")

    def test_folder_delete_removes_its_files_only(self):
        upsert_renamed_file(
            self.db, "hash", "01.mkv", status="done",
            target="葬送的芙莉莲 [bgm-1]\\Season 01\\S01E01.mkv",
        )
        upsert_renamed_file(
            self.db, "other", "01.mkv", status="done",
            target="葬送的芙莉莲 剧场版 [bgm-2]\\S01E01.mkv",
        )
        upsert_renamed_file(
            self.db, "pending", "03.mkv", status="failed", target=None,
        )
        gone = delete_renamed_files_at(self.db, "葬送的芙莉莲 [bgm-1]", whole_folder=True)
        self.db.commit()
        self.assertEqual(gone, 1)
        left = {row.torrent_hash for row in self.db.query(models.RenamedFile).all()}
        self.assertEqual(left, {"other", "pending"})


if __name__ == "__main__":
    unittest.main()
