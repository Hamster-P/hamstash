"""本季是否下完：要 1..N 每一集都在，不是出现了第 N 集就算。"""
import unittest

from services.rss_season_complete import (
    LONG_RUNNING_MIN_EPS,
    episode_index,
    season_is_complete,
    torrent_hash_of,
    usable_episode_count,
)


class SeasonCompleteTests(unittest.TestCase):
    def test_needs_every_episode(self):
        self.assertFalse(season_is_complete({12}, 12))
        self.assertFalse(season_is_complete(set(range(1, 12)), 12))
        self.assertTrue(season_is_complete(set(range(1, 13)), 12))

    def test_unknown_or_long_running_has_no_count(self):
        self.assertIsNone(usable_episode_count(None))
        self.assertIsNone(usable_episode_count(0))
        self.assertIsNone(usable_episode_count(LONG_RUNNING_MIN_EPS))
        self.assertEqual(usable_episode_count(12), 12)
        self.assertEqual(usable_episode_count(24), 24)

    def test_half_episode_does_not_fill_an_integer_slot(self):
        name = "[Group] Title - 12.5 [1080p].mkv"
        self.assertIsNone(episode_index(name, 12, 0))

    def test_absolute_number_maps_into_this_season(self):
        name = "[Group] Title - 25 [1080p].mkv"
        self.assertEqual(episode_index(name, 12, 24), 1)
        self.assertIsNone(episode_index(name, 12, 0))

    def test_hash_comes_from_magnet_when_info_hash_missing(self):
        magnet = "magnet:?xt=urn:btih:AABBCCDDEEFF00112233445566778899AABBCCDD&dn=x"
        self.assertEqual(
            torrent_hash_of(None, magnet),
            "aabbccddeeff00112233445566778899aabbccdd",
        )
        self.assertIsNone(torrent_hash_of(None, "magnet:?xt=urn:btih:not-a-hash"))

    def test_base32_btih_decodes_to_hex(self):
        # 动漫花园常见的 32 位 Base32，对应 20 字节十六进制哈希。
        magnet = "magnet:?xt=urn:btih:CGTUM6MX3YEPRGKHRD7GDDLOV6WX7TBR&dn="
        self.assertEqual(
            torrent_hash_of(None, magnet),
            "11a7467997de08f8994788fe618d6eafad7fcc31",
        )


if __name__ == "__main__":
    unittest.main()
