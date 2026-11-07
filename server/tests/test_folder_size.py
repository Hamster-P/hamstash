import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.folder_size import measure_folder


class FolderSizeTests(unittest.TestCase):
    def test_cache_survives_reload_and_skips_unchanged_file_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Season").mkdir()
            (root / "Season" / "episode.mkv").write_bytes(b"video")
            (root / "cover.jpg").write_bytes(b"art")
            size, cache = measure_folder(root)
            self.assertEqual(size, 8)
            with patch("services.folder_size.os.scandir", side_effect=AssertionError("cache miss")):
                self.assertEqual(measure_folder(root, cache), (size, cache))

    def test_nested_add_and_remove_only_rescan_changed_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = root / "Season" / "Extras"
            nested.mkdir(parents=True)
            video = nested / "episode.mkv"
            video.write_bytes(b"video")
            _, cache = measure_folder(root)
            old_stamp = nested.stat().st_mtime_ns
            (nested / "subtitle.ass").write_bytes(b"sub")
            os.utime(nested, ns=(old_stamp + 1000000000, old_stamp + 1000000000))
            with patch("services.folder_size.os.scandir", wraps=os.scandir) as scan:
                size, cache = measure_folder(root, cache)
                self.assertEqual(size, 8)
                self.assertEqual(scan.call_count, 1)
                self.assertEqual(scan.call_args.args[0], nested)
            video.unlink()
            os.utime(nested, ns=(old_stamp + 2000000000, old_stamp + 2000000000))
            self.assertEqual(measure_folder(root, cache)[0], 3)

    def test_cache_is_scoped_to_library_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first, second = root / "first", root / "second"
            first.mkdir()
            second.mkdir()
            (first / "video").write_bytes(b"123")
            _, cache = measure_folder(first)
            self.assertEqual(measure_folder(second, cache)[0], 0)
