"""RSS 的单集限制与手动搜索/改名预览使用同一标题判断。"""
import unittest

from rename_engine import is_collection_title, is_single_episode_title
from sources.base import ResourceItem, SearchCriteria, matches_criteria
from services.rss_poller import _criteria_from_rule
from models import SubscriptionRule


class ReleaseFilterTests(unittest.TestCase):
    def test_collection_spellings_are_rejected_by_single_filter(self):
        for suffix in ["01-12", "01～12", "01~12", "E01-E12", "01—12", "第01至12集",
                       "01+02", "01&02", "01_12", "01話-12話", "Batch", "PACK", "Complete", "BD BOX", "全集", "全12話", "S1+S2"]:
            title = f"[Group] Test Anime [{suffix}] [1080p]"
            with self.subTest(suffix=suffix):
                self.assertTrue(is_collection_title(title))
                item = ResourceItem(source="dmhy", title=title)
                self.assertFalse(matches_criteria(item, SearchCriteria(release_type="单集(追更)")))
                self.assertTrue(matches_criteria(item, SearchCriteria(release_type="合集/全集(完结)")))

    def test_final_single_episode_and_noise_are_not_collections(self):
        for title in ["[Group] Test Anime - 12 [FIN] [1080p]", "[Group] Test Anime - 12 END",
                      "[Group] Finland - 05 [1080p]", "[Group] Backpacker - 05",
                      "[Group] Test Anime - 05 [x264-10bit] [85.5 MB]",
                      "[Group] Test Anime - 05 [2026-11-06]", "[Group] Test Anime S01E05",
                      "[Group] 动画 第05话 [1080p]", "[Group] Test Anime - 05 [720-1080p]"]:
            with self.subTest(title=title):
                self.assertFalse(is_collection_title(title))
                self.assertTrue(is_single_episode_title(title))

    def test_unknown_title_is_not_assumed_to_be_single(self):
        for title in ["[Group] Test Anime [1080p]", "Test Anime [2026-11-06] [85.5 MB]"]:
            with self.subTest(title=title):
                self.assertFalse(is_single_episode_title(title))
                self.assertFalse(matches_criteria(ResourceItem(source="nyaa", title=title),
                                                  SearchCriteria(release_type="单集(追更)")))

    def test_normal_rules_respect_release_choice_and_fast_rules_stay_single(self):
        for mode in [False, True]:
            for release in [None, "不限", "合集/全集(完结)"]:
                rule = SubscriptionRule(keyword="Test Anime", fast_mode=mode, release_type=release)
                criteria = _criteria_from_rule(rule)
                self.assertEqual(criteria.release_type, "单集(追更)" if mode else release)
                self.assertEqual(matches_criteria(ResourceItem(source="dmhy", title="Test Anime [01-12]"), criteria), not mode)

    def test_unrestricted_manual_search_still_accepts_collections(self):
        self.assertTrue(matches_criteria(ResourceItem(source="dmhy", title="Test Anime [01-12]"), SearchCriteria()))
