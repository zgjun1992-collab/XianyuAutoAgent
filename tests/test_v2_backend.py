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
        self.assertIn("工作日午餐可用", product["raw_text"])
        self.assertIn("周末不可用", product["raw_text"])
        self.assertIn("适用门店营业时间内可用", product["raw_text"])
        self.assertEqual("https://img.alicdn.com/cover.jpg", product["thumbnail_url"])
        self.assertIn("https://img.alicdn.com/detail.jpg", product["image_urls"])
        self.assertIn("工作日午餐可用", self.summary_calls[0])
        self.assertNotIn("image_url", self.summary_calls[0])

    @unittest.skipUnless(os.name == "nt", "Windows named mutex only")
    def test_only_one_live_service_can_own_the_same_data_directory(self):
        other = BackendState(self.temp.name)
        self.state._acquire_service_mutex()
        try:
            with self.assertRaisesRegex(ValueError, "已有客服实例"):
                other._acquire_service_mutex()
        finally:
            self.state._release_service_mutex()
        other._acquire_service_mutex()
        other._release_service_mutex()

    @patch("v2_backend.XianyuApis", FakeXianyuApis)
    def test_resync_never_overwrites_manual_knowledge(self):
        self.state.sync_products()
        self.state.store.save_v2_product("30003", "人工标题", "人工修改后的最高优先级资料")
        self.state.sync_products()
        product = self.state.store.get_v2_product("30003")
        self.assertEqual("人工修改后的最高优先级资料", product["raw_text"])
        self.assertEqual(1, product["manual_edited"])

    def test_sync_without_mtop_token_shows_login_message_before_api_call(self):
        self.state.configure({"cookie": "unb=seller-1"})
        with self.assertRaisesRegex(ValueError, "闲鱼登录状态已失效"):
            self.state.sync_products()

    def test_new_cookie_is_applied_to_running_service_and_reconnects(self):
        class Live:
            def __init__(self):
                self.updated = []

            def update_cookie(self, value):
                self.updated.append(value)
                return True

        self.state.live = Live()
        self.state.service_status = "starting"
        self.state.configure({"cookie": "unb=seller-1; _m_h5_tk=new-token_456"})
        self.assertEqual(
            ["unb=seller-1; _m_h5_tk=new-token_456"],
            self.state.live.updated,
        )
        self.assertEqual("reconnecting", self.state.service_status)

    def test_non_auth_cookie_change_does_not_interrupt_running_service(self):
        class Live:
            def __init__(self):
                self.updated = []

            def update_cookie(self, value):
                self.updated.append(value)

        self.state.live = Live()
        self.state.service_status = "connected"
        self.state.configure({
            "cookie": "unb=seller-1; _m_h5_tk=testtoken_123; tracking=changed",
        })
        self.assertEqual([], self.state.live.updated)
        self.assertEqual("connected", self.state.service_status)


if __name__ == "__main__":
    unittest.main()
