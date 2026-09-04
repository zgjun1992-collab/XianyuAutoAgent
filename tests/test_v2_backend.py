import os
import tempfile
import unittest
from unittest.mock import patch

from v2_backend import BackendState


class FakeXianyuApis:
    def __init__(self, interactive=False):
        from requests import Session

        self.interactive = interactive
        self.session = Session()

    def get_all_user_items(self, user_id):
        if user_id != "seller-1":
            raise AssertionError("seller scope was not read from the login cookie")
        return [
            {
                "cardData": {
                    "id": "30003",
                    "itemStatus": 0,
                    "title": "小江溪125元代金券",
                    "priceInfo": {"price": "89"},
                    "picInfo": {"picUrl": "//img.alicdn.com/cover.jpg"},
                    "detailParams": {"itemId": "30003"},
                }
            },
            {"cardData": {"id": "offline-item", "itemStatus": 1}},
        ]

    def get_item_info(self, item_id):
        return {
            "data": {
                "itemDO": {
                    "title": "小江溪125元代金券",
                    "desc": "工作日午餐可用，周末不可用。",
                    "soldPrice": "89",
                    "quantity": 8,
                    "images": ["https://img.alicdn.com/detail.jpg"],
                }
            }
        }


class BackendSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = BackendState(self.temp.name)
        self.state.configure({
            "cookie": "unb=seller-1; _m_h5_tk=testtoken_123",
            "api_key": "test-key",
            "model": "qwen-plus",
        })
        self.summary_calls = []

        def summarize(source_text):
            self.summary_calls.append(source_text)
            return "AI生成的初始知识", {"summary": "AI生成的初始知识", "time_rules": []}

        self.state._generate_summary = summarize

    def tearDown(self):
        self.temp.cleanup()

    @patch("v2_backend.XianyuApis", FakeXianyuApis)
    def test_syncs_onsale_text_without_sending_images_to_ai(self):
        result = self.state.sync_products()
        self.assertEqual(1, result["synced"])
        self.assertEqual(1, result["total"])
        product = self.state.store.get_v2_product("30003")
        self.assertTrue(product["raw_text"].startswith("AI生成的初始知识"))
        self.assertIn("适用门店营业时间内可用", product["raw_text"])
        self.assertEqual("https://img.alicdn.com/cover.jpg", product["thumbnail_url"])
        self.assertIn("https://img.alicdn.com/detail.jpg", product["image_urls"])
        self.assertIn("工作日午餐可用", self.summary_calls[0])
        self.assertNotIn("image_url", self.summary_calls[0])

    @patch("v2_backend.XianyuApis", FakeXianyuApis)
    def test_resync_never_overwrites_manual_knowledge(self):
        self.state.sync_products()
        self.state.store.save_v2_product("30003", "人工标题", "人工修改后的最高优先级资料")
        self.state.sync_products()
        product = self.state.store.get_v2_product("30003")
        self.assertEqual("人工修改后的最高优先级资料", product["raw_text"])
        self.assertEqual(1, product["manual_edited"])


if __name__ == "__main__":
    unittest.main()
