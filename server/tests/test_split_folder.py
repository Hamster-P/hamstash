"""标题里的 / 不能把番剧文件夹拆成两层。"""
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
import rename_engine
from routers.library import repair_split_bgm_folders


class BuildAnimeFolderNameTests(unittest.TestCase):
    def test_slash_colon_and_backslash_become_fullwidth(self):
        self.assertEqual(
            rename_engine.build_anime_folder_name("乱马1/2", 489820),
            "乱马1／2 [bgm-489820]",
        )
        self.assertEqual(
            rename_engine.build_anime_folder_name("Re:Zero", 1),
            "Re：Zero [bgm-1]",
        )
        self.assertEqual(
            rename_engine.build_anime_folder_name("a\\b", 2),
            "a＼b [bgm-2]",
        )

    def test_plain_title_stays(self):
        self.assertEqual(
            rename_engine.build_anime_folder_name("第三季", 22),
            "第三季 [bgm-22]",
        )

    def test_other_illegal_characters_still_become_underscore(self):
        self.assertEqual(
            rename_engine.build_anime_folder_name("a*b", 3),
            "a_b [bgm-3]",
        )


class RepairSplitFolderTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        models.Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.tmp.cleanup()

    def test_split_ranma_folder_is_joined_and_bound(self):
        leaf = self.root / "乱马1" / "2 [bgm-489820]"
        leaf.mkdir(parents=True)
        (leaf / "ep.mkv").write_bytes(b"x")
        self.db.add(models.LocalMedia(folder_name="乱马1"))
        self.db.add(models.PlaybackRecord(folder_name="乱马1", filename="ep.mkv"))
        self.db.add(models.RenamedFile(
            torrent_hash="hash",
            original_path="ep.mkv",
            status="done",
            target_relative_path="乱马1\\2 [bgm-489820]\\ep.mkv",
        ))
        self.db.add(models.RenamedFile(
            torrent_hash="other",
            original_path="x.mkv",
            status="done",
            target_relative_path="别的番 [bgm-2]\\Season 01\\x.mkv",
        ))
        self.db.add(models.StandaloneMedia(
            library_folder="乱马1",
            rel_path="乱马1/2 [bgm-489820]/ep.mkv",
            filename="ep.mkv",
            bgm_id=489820,
            source="download",
        ))
        self.db.commit()

        recovered = repair_split_bgm_folders(self.root, self.db)
        self.db.commit()

        new_name = "乱马1／2 [bgm-489820]"
        self.assertEqual(recovered, [489820])
        self.assertTrue((self.root / new_name / "ep.mkv").is_file())
        self.assertFalse((self.root / "乱马1").exists())
        media = self.db.query(models.LocalMedia).one()
        self.assertEqual(media.folder_name, new_name)
        self.assertEqual(media.bgm_id, 489820)
        watched = self.db.query(models.PlaybackRecord).one()
        self.assertEqual(watched.folder_name, new_name)
        renamed = self.db.query(models.RenamedFile).filter_by(torrent_hash="hash").one()
        self.assertEqual(renamed.target_relative_path, f"{new_name}\\ep.mkv")
        other = self.db.query(models.RenamedFile).filter_by(torrent_hash="other").one()
        self.assertEqual(other.target_relative_path, "别的番 [bgm-2]\\Season 01\\x.mkv")
        standalone = self.db.query(models.StandaloneMedia).one()
        self.assertEqual(standalone.library_folder, new_name)
        self.assertEqual(standalone.rel_path, f"{new_name}/ep.mkv")

        again = repair_split_bgm_folders(self.root, self.db)
        self.assertEqual(again, [])
        self.assertTrue((self.root / new_name / "ep.mkv").is_file())

    def test_deeper_chain_joins_every_segment(self):
        leaf = self.root / "A" / "B" / "C [bgm-7]"
        leaf.mkdir(parents=True)
        (leaf / "f.mkv").write_bytes(b"x")

        recovered = repair_split_bgm_folders(self.root, self.db)

        self.assertEqual(recovered, [7])
        self.assertTrue((self.root / "A／B／C [bgm-7]" / "f.mkv").is_file())
        self.assertFalse((self.root / "A").exists())

    def test_season_directory_is_not_a_split(self):
        season = self.root / "葬送的芙莉莲" / "Season 01"
        season.mkdir(parents=True)
        (season / "ep.mkv").write_bytes(b"x")
        self.db.add(models.LocalMedia(folder_name="葬送的芙莉莲"))
        self.db.commit()

        recovered = repair_split_bgm_folders(self.root, self.db)
        self.db.commit()

        self.assertEqual(recovered, [])
        self.assertTrue((season / "ep.mkv").is_file())
        media = self.db.query(models.LocalMedia).one()
        self.assertEqual(media.folder_name, "葬送的芙莉莲")
        self.assertIsNone(media.bgm_id)

    def test_two_children_are_left_alone(self):
        parent = self.root / "乱马1"
        (parent / "2 [bgm-489820]").mkdir(parents=True)
        (parent / "extra").mkdir()

        self.assertEqual(repair_split_bgm_folders(self.root, self.db), [])
        self.assertTrue((parent / "2 [bgm-489820]").is_dir())

    def test_file_in_parent_blocks_the_merge(self):
        parent = self.root / "乱马1"
        (parent / "2 [bgm-489820]").mkdir(parents=True)
        (parent / "note.txt").write_text("keep", encoding="utf-8")

        self.assertEqual(repair_split_bgm_folders(self.root, self.db), [])
        self.assertTrue((parent / "note.txt").is_file())

    def test_existing_target_is_not_overwritten(self):
        leaf = self.root / "乱马1" / "2 [bgm-489820]"
        leaf.mkdir(parents=True)
        (leaf / "ep.mkv").write_bytes(b"new")
        dest = self.root / "乱马1／2 [bgm-489820]"
        dest.mkdir()
        (dest / "old.mkv").write_bytes(b"old")

        self.assertEqual(repair_split_bgm_folders(self.root, self.db), [])
        self.assertTrue((leaf / "ep.mkv").is_file())
        self.assertTrue((dest / "old.mkv").is_file())
        self.assertFalse((dest / "ep.mkv").exists())


if __name__ == "__main__":
    unittest.main()
