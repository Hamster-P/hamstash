"""搜索不按产地剔除。追更页的日本限定不在这里。"""
import unittest

from bangumi_client import _filter_search_page


class SearchOriginTests(unittest.TestCase):
    def test_keeps_non_japanese_titles_that_match(self):
        items = [
            {"name": "時光代理人", "name_cn": "时光代理人", "meta_tags": ["中国", "动画"]},
            {"name": "進撃の巨人", "name_cn": "进击的巨人", "meta_tags": ["日本"]},
        ]
        kept = _filter_search_page(items, "时光代理人")
        self.assertEqual([item["name_cn"] for item in kept], ["时光代理人"])

    def test_empty_keyword_keeps_every_origin(self):
        items = [
            {"name_cn": "时光代理人", "meta_tags": ["中国"]},
            {"name_cn": "进击的巨人", "meta_tags": ["日本"]},
        ]
        self.assertEqual(_filter_search_page(items, ""), items)


if __name__ == "__main__":
    unittest.main()
