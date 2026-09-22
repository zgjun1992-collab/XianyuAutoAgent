import json
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

    def test_nested_goofish_sku_records_are_recovered(self):
        payload = {
            "itemDO": {"skuList": []},
            "tradeInfo": {"skuInfo": {"skuList": [{
                "skuId": "sku-200", "priceInCent": 11980,
                "propertyList": [{"valueText": "美团200元代金券"}],
            }] }},
        }
        records = self.state._collect_sku_records(payload)
        self.assertEqual(1, len(records))
        self.assertEqual("sku-200", records[0]["skuId"])

    @patch("v2_backend.XianyuApis", FakeXianyuApis)
    def test_syncs_onsale_text_without_sending_images_to_ai(self):
        result = self.state.sync_products()
        self.assertEqual(1, result["synced"])
        self.assertEqual(1, result["total"])
        product = self.state.store.get_v2_product("30003")
        self.assertTrue(product["ai_summary"])
        self.assertIn("适用门店营业时间内可用", product["ai_summary"])
        self.assertIn("工作日午餐可用", product["raw_text"])
        self.assertIn("周末不可用", product["raw_text"])
        self.assertIn("适用门店营业时间内可用", product["ai_summary"])
        self.assertEqual("https://img.alicdn.com/cover.jpg", product["thumbnail_url"])
        self.assertIn("https://img.alicdn.com/detail.jpg", product["image_urls"])
        self.assertIn("工作日午餐可用", self.summary_calls[0])
        self.assertNotIn("image_url", self.summary_calls[0])

    @patch("v2_backend.XianyuApis", FakeXianyuApis)
    def test_lists_lightweight_onsale_products_for_selective_sync(self):
        options = self.state.list_onsale_products()

        self.assertEqual(1, len(options))
        self.assertEqual("30003", options[0]["item_id"])
        self.assertEqual("小江溪125元代金券", options[0]["title"])
        self.assertFalse(options[0]["imported"])
        self.assertFalse(options[0]["ignored"])
        self.assertEqual([], self.summary_calls)

    @patch("v2_backend.XianyuApis", FakeXianyuApis)
    def test_selective_sync_only_updates_requested_product(self):
        self.state.store.save_v2_product("keep-online", "保留商品", "人工资料")

        result = self.state.sync_products(["30003"])

        self.assertEqual(1, result["synced"])
        self.assertEqual("onsale", self.state.store.get_v2_product("keep-online")["item_status"])
        self.assertIsNotNone(self.state.store.get_v2_product("30003"))

    @patch("v2_backend.XianyuApis", FakeXianyuApis)
    def test_selective_sync_reports_missing_onsale_product(self):
        result = self.state.sync_products(["missing-item"])

        self.assertEqual(0, result["synced"])
        self.assertEqual("missing-item", result["failed"][0]["item_id"])
        self.assertIn("未在当前闲鱼在售商品中找到", result["failed"][0]["error"])

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

    def test_resummarize_locks_real_skus_and_builds_one_profile_per_sku(self):
        platform = json.dumps({
            "title": "测试品牌多规格券",
            "description": "页面旧规则：100券限工作日；200券限周末。页面误写100券售价88元。",
            "sku": [
                {"skuId": "sku-100", "priceInCent": 6900, "quantity": 8,
                 "propertyList": [{"valueText": "美团100元代金券"}]},
                {"skuId": "sku-200", "priceInCent": 13800, "quantity": 5,
                 "propertyList": [{"valueText": "美团200元代金券"}]},
            ],
        }, ensure_ascii=False)
        self.state.store.upsert_synced_product({
            "item_id": "sku-profile", "title": "测试品牌多规格券",
            "platform_summary": platform, "item_status": "onsale",
        })
        self.state.store.save_v2_product(
            "sku-profile", "测试品牌多规格券",
            "人工规则：100券午餐可用、最多2张；200券晚餐可用、最多1张。",
        )
        captured = []

        def summarize(source_text):
            payload = json.loads(source_text)
            captured.append(payload)
            first, second = payload["canonical_skus"]
            return "模型草稿", {
                "summary": "模型草稿",
                "common_rules": {"refund_rule": "未使用可退"},
                "sku_profiles": [
                    {"sku_key": first["sku_key"], "sku_name": "伪造名称", "sale_price": "1", "meal_period": "午餐", "max_stack": "2"},
                    {"sku_key": second["sku_key"], "sku_name": "伪造名称", "sale_price": "2", "meal_period": "晚餐", "max_stack": "1"},
                ],
                "time_rules": [],
            }

        self.state._generate_summary = summarize
        before = self.state.store.get_v2_product("sku-profile")
        result = self.state.summarize_product("sku-profile")
        self.assertEqual("", captured[0]["page_description"])
        self.assertIn("人工规则", captured[0]["effective_knowledge"])
        self.assertEqual(["69", "138"], [row["sale_price"] for row in captured[0]["canonical_skus"]])
        profiles = result["ai_draft_structured"]["sku_profiles"]
        self.assertEqual(["美团100元代金券", "美团200元代金券"], [row["sku_name"] for row in profiles])
        self.assertEqual(["69", "138"], [row["sale_price"] for row in profiles])
        self.assertEqual(["未使用可退", "未使用可退"], [row["refund_rule"] for row in profiles])
        self.assertEqual([["refund_rule"], ["refund_rule"]], [row["inherited_common_fields"] for row in profiles])
        self.assertIn("【逐SKU规则档案】", result["ai_draft_summary"])
        self.assertEqual(before["structured"], result["structured"])
        self.assertEqual(before["raw_text"], result["raw_text"])

    def test_unlabelled_buffet_profile_defaults_to_adult_and_all_times(self):
        structured = {
            "sku_profiles": [{"sku_key": "light-2", "sku_name": "轻享两位"}],
        }
        canonical = [{
            "sku_key": "light-2", "sku_name": "轻享两位", "option_type": "package",
            "sale_price": "438", "face_value": "", "composition": "", "stock": "8",
            "sellable": True, "people_counts_in_sku_name": [2],
        }]

        result = self.state._normalize_summary_structure(structured, canonical)

        profile = result["sku_profiles"][0]
        self.assertEqual("成人", profile["audience"])
        self.assertEqual("适用门店营业时间内可用", profile["applicable_time"])
        self.assertEqual("适用门店营业时间内可用", result["sale_options"][0]["applicable_time"])

    def test_explicit_buffet_audience_and_meal_are_never_replaced_by_defaults(self):
        structured = {
            "sku_profiles": [{
                "sku_key": "child-dinner", "sku_name": "儿童晚餐",
                "audience": "儿童", "meal_period": "晚餐", "applicable_time": "晚餐时段可用",
            }],
        }
        canonical = [{
            "sku_key": "child-dinner", "sku_name": "儿童晚餐", "option_type": "package",
            "sale_price": "88", "face_value": "", "composition": "", "stock": "8",
            "sellable": True, "people_counts_in_sku_name": [1],
        }]

        result = self.state._normalize_summary_structure(structured, canonical)

        profile = result["sku_profiles"][0]
        self.assertEqual("儿童", profile["audience"])
        self.assertEqual("晚餐", profile["meal_period"])
        self.assertEqual("晚餐时段可用", profile["applicable_time"])

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

    def test_verification_event_is_exposed_and_new_challenge_cookie_reconnects(self):
        class Live:
            def __init__(self):
                self.updated = []

            def update_cookie(self, value):
                self.updated.append(value)

        self.state.live = Live()
        self.state.on_live_event({
            "type": "verification_required",
            "message": "闲鱼要求安全验证",
            "url": "https://h5.m.goofish.com/_____tmd_____/punish?token=test",
        })
        state = self.state.service_state()
        self.assertEqual("verification_required", state["status"])
        self.assertTrue(state["verification_required"])
        self.assertIn("punish", state["verification_url"])

        cookie = "unb=seller-1; _m_h5_tk=testtoken_123; x5sec=verified"
        self.state.configure({"cookie": cookie})
        self.assertEqual([cookie], self.state.live.updated)
        self.assertEqual("reconnecting", self.state.service_status)

    def test_promotion_qr_upload_refreshes_first_reply_and_test_preview(self):
        self.state.store.save_v2_product(
            "promo-1", "推广商品", "推广购买链接：https://example.invalid/buy",
            coupon_type="promotion",
        )
        source = os.path.join(self.temp.name, "qr.png")
        with open(source, "wb") as handle:
            handle.write(b"qr-image")
        asset = self.state.save_image_asset({
            "item_id": "promo-1",
            "source_path": source,
            "name": "推广购买二维码",
            "purpose": "promotion_qr",
            "trigger_words": "推广购买二维码",
            "reply_text": "",
            "enabled": True,
        })
        product = self.state.store.get_v2_product("promo-1")
        self.assertIn(f"{{$图片:{asset['id']}}}", product["first_reply_text"])
        preview = self.state.test_reply({"item_id": "promo-1", "message": "下单链接发我"})
        self.assertEqual("allow", preview["action"])
        self.assertEqual(asset["id"], preview["image_asset"]["id"])

        self.state.delete_image_asset(asset["id"])
        product = self.state.store.get_v2_product("promo-1")
        self.assertNotIn(f"{{$图片:{asset['id']}}}", product["first_reply_text"])


if __name__ == "__main__":
    unittest.main()
