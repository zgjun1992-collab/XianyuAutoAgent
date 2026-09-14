import os
import tempfile
import unittest

from app_store import (
    AppStore, DEFAULT_POLICIES, LEGACY_AFTERSALE_POLICY_RAW,
    LEGACY_AFTERSALE_POLICY_SUMMARY, LEGACY_GLOBAL_SYSTEM_PROMPT,
    PolicyEngine, find_unauthorized_promises,
)
from XianyuApis import XianyuApis


class AppStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = AppStore(os.path.join(self.tempdir.name, "app.db"))

    def tearDown(self):
        self.tempdir.cleanup()

    def test_local_product_has_explicit_priority(self):
        self.store.save_product("1001", "测试餐券", "有效期：2026-12-31\n售价：85元")
        merged = self.store.build_merged_knowledge("1001", '{"desc":"有效期到2026-10-31"}')
        self.assertIn("本地商品资料（最高优先级）", merged)
        self.assertIn("2026-12-31", merged)
        self.assertIn("闲鱼页面资料（仅补充本地资料缺失项）", merged)

    def test_price_request_is_replaced_with_polite_refusal(self):
        result = PolicyEngine().evaluate("80元可以吗", "可以，我给你降价到80元。")
        self.assertEqual("replace", result.action)
        self.assertIn("暂不支持议价", result.suggested_reply)

    def test_compact_calendar_date_is_not_mistaken_for_bargaining(self):
        for message in ("9.26可以用吗", "9-26能用吗", "9月26日可用吗"):
            with self.subTest(message=message):
                result = PolicyEngine().evaluate(message, "9月26日不能使用。")
                self.assertEqual("allow", result.action)

        bargain = PolicyEngine().evaluate("9.26元可以吗", "不支持议价。")
        self.assertEqual("replace", bargain.action)

    def test_refund_request_requires_review_even_with_safe_draft(self):
        result = PolicyEngine().evaluate("可以马上退款吗", "我先帮你看看订单。")
        self.assertEqual("review", result.action)
        self.assertIn("人工核实", result.suggested_reply)

    def test_generic_refund_question_is_not_blindly_marked_high_risk(self):
        result = PolicyEngine().evaluate("怎么申请退款", "请按已确认的退款流程操作。")
        self.assertEqual("allow", result.action)

    def test_safe_answer_is_allowed(self):
        result = PolicyEngine().evaluate("有效期到什么时候", "本地资料显示有效期到2026年12月31日。")
        self.assertEqual("allow", result.action)

    def test_coupon_word_is_not_mistaken_for_bargaining(self):
        result = PolicyEngine().evaluate("这个优惠券怎么用", "到店扫码核销。")
        self.assertEqual("allow", result.action)

    def test_mixed_denomination_question_is_not_bargaining(self):
        result = PolicyEngine().evaluate("200和300可以一起用吗", "200和300不能混用。")
        self.assertEqual("allow", result.action)

    def test_store_amount_usage_question_is_not_bargaining(self):
        result = PolicyEngine().evaluate(
            "深圳壹方城200可以用吗", "深圳宝安壹方城店可用。",
        )
        self.assertEqual("allow", result.action)

        bargain = PolicyEngine().evaluate("80元可以吗", "不支持议价。")
        self.assertEqual("replace", bargain.action)

    def test_dangerous_bargain_draft_is_blocked(self):
        result = PolicyEngine().evaluate("你好", "全新可小刀。")
        self.assertEqual("review", result.action)
        self.assertNotIn("小刀", result.suggested_reply)

    def test_all_unapproved_commitment_phrases_are_blocked(self):
        phrases = (
            "给你降价", "给您降价", "给你便宜", "给您便宜", "最低价给你",
            "马上退款", "立即退款", "现在退款", "一定退款", "保证退款",
            "赔你", "赔您", "补偿你", "补偿您", "免费送你", "免费送您",
            "保证能用", "肯定能用", "所有门店都能用", "全国门店都能用",
            "保证到账", "马上到账", "立即到账", "延长有效期",
            "给你换码", "给您换码", "全国通用",
        )
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                result = PolicyEngine().evaluate("你好", phrase)
                self.assertEqual("review", result.action)
                self.assertEqual(DEFAULT_POLICIES["manual_review_notice"], result.suggested_reply)

    def test_safe_negation_does_not_become_a_false_promise(self):
        for draft in (
            "不能保证退款，需要人工核实。",
            "当前卡券不是全国通用，仅限已确认门店。",
            "补发需要人工核实后处理。",
        ):
            with self.subTest(draft=draft):
                self.assertEqual([], find_unauthorized_promises(draft))
                self.assertEqual("allow", PolicyEngine().evaluate("你好", draft).action)

    def test_old_builtin_review_notice_migrates_but_custom_notice_is_preserved(self):
        old_builtin = "这个问题需要进行人工审核，已经为您记录并转交人工处理，我们会在72小时内处理。"
        self.store.set_setting("manual_review_notice", old_builtin)
        migrated = AppStore(self.store.db_path)
        self.assertEqual(
            DEFAULT_POLICIES["manual_review_notice"],
            migrated.get_policies()["manual_review_notice"],
        )
        self.store.set_setting("manual_review_notice", "这是人工自定义审核话术")
        reopened = AppStore(self.store.db_path)
        self.assertEqual("这是人工自定义审核话术", reopened.get_policies()["manual_review_notice"])

    def test_old_builtin_global_prompt_migrates_but_custom_prompt_is_preserved(self):
        self.store.set_setting("global_system_prompt", LEGACY_GLOBAL_SYSTEM_PROMPT)
        migrated = AppStore(self.store.db_path)
        self.assertIn("不同平台的卡券不得混用", migrated.get_policies()["global_system_prompt"])
        self.store.set_setting("global_system_prompt", "这是人工自定义约束")
        reopened = AppStore(self.store.db_path)
        self.assertEqual("这是人工自定义约束", reopened.get_policies()["global_system_prompt"])

    def test_old_builtin_aftersale_policy_migrates_but_custom_policy_is_preserved(self):
        self.store.set_setting("aftersale_policy_raw", LEGACY_AFTERSALE_POLICY_RAW)
        self.store.set_setting("aftersale_policy_summary", LEGACY_AFTERSALE_POLICY_SUMMARY)
        migrated = AppStore(self.store.db_path)
        policies = migrated.get_policies()
        self.assertEqual(DEFAULT_POLICIES["aftersale_policy_raw"], policies["aftersale_policy_raw"])
        self.assertEqual(
            DEFAULT_POLICIES["aftersale_policy_summary"], policies["aftersale_policy_summary"]
        )
        self.assertIn("敬请理解", policies["aftersale_policy_summary"])

        self.store.set_setting("aftersale_policy_raw", "这是商家自定义售后原文")
        self.store.set_setting("aftersale_policy_summary", "这是商家自定义售后摘要")
        reopened = AppStore(self.store.db_path)
        self.assertEqual("这是商家自定义售后原文", reopened.get_policies()["aftersale_policy_raw"])
        self.assertEqual("这是商家自定义售后摘要", reopened.get_policies()["aftersale_policy_summary"])

    def test_audit_lifecycle(self):
        audit_id = self.store.create_audit(
            chat_id="chat", user_id="buyer", user_name="买家", item_id="1001",
            user_message="你好", draft_reply="你好", action="allow", reasons=[], status="pending",
            order_id="ORDER-1001", conversation_url="https://www.goofish.com/im",
            order_url="https://www.goofish.com/order/detail?id=ORDER-1001",
        )
        created = self.store.get_audit(audit_id)
        self.assertEqual("pending", created["status"])
        self.assertEqual("ORDER-1001", created["order_id"])
        self.assertIn("/im", created["conversation_url"])
        self.assertIn("ORDER-1001", created["order_url"])
        self.store.update_audit(audit_id, "sent", "您好")
        row = self.store.get_audit(audit_id)
        self.assertEqual("sent", row["status"])
        self.assertEqual("您好", row["final_reply"])

    def test_audit_redacts_order_and_voucher_credentials(self):
        audit_id = self.store.create_audit(
            chat_id="chat", user_id="buyer", user_name="买家", item_id="1001",
            user_message=(
                "3316384347100027780\n"
                "卡号：示例-044175443178-100元代金券\n"
                "密码：https://kms.example.invalid/xgj/private-token"
            ),
            draft_reply="", action="review", reasons=[], status="pending",
        )
        message = self.store.get_audit(audit_id)["user_message"]
        self.assertNotIn("3316384347100027780", message)
        self.assertNotIn("private-token", message)
        self.assertNotIn("044175443178", message)
        self.assertIn("已隐藏", message)

    def test_conversation_reply_count_resets_and_is_scoped(self):
        state = self.store.touch_conversation("seller:chat:item1", "seller", "chat", "buyer", "item1", 24)
        self.assertEqual(0, state["ai_reply_count"])
        self.store.record_ai_reply("seller:chat:item1")
        self.store.mark_first_reply_sent("seller:chat:item1")
        self.assertTrue(self.store.is_first_reply_sent("seller:chat:item1"))
        state = self.store.touch_conversation("seller:chat:item1", "seller", "chat", "buyer", "item1", 24)
        self.assertEqual(1, state["ai_reply_count"])
        self.assertEqual(1, state["first_reply_sent"])
        other = self.store.touch_conversation("seller:chat:item2", "seller", "chat", "buyer", "item2", 24)
        self.assertEqual(0, other["ai_reply_count"])
        self.store.pause_conversation("seller:chat:item1")
        paused = self.store.list_conversations()[0]
        self.assertEqual("limit_reached", paused["state"])
        self.store.reset_conversation("seller:chat:item1")
        reset = next(item for item in self.store.list_conversations() if item["scope_id"].endswith("item1"))
        self.assertEqual(0, reset["ai_reply_count"])
        self.assertEqual(0, reset["first_reply_sent"])
        self.assertFalse(self.store.is_first_reply_sent("seller:chat:item1"))
        self.assertEqual("active", reset["state"])

    def test_manual_conversation_state_is_persistent_and_resumable(self):
        scope_id = "seller:chat:item1"
        self.store.touch_conversation(scope_id, "seller", "chat", "buyer", "item1", 24)
        self.store.pause_conversation(scope_id, "manual")
        reopened = AppStore(self.store.db_path)
        self.assertEqual("manual", reopened.get_conversation_state(scope_id))
        reopened.resume_conversation(scope_id)
        self.assertEqual("active", reopened.get_conversation_state(scope_id))

    def test_manual_state_survives_reply_count_and_inactivity_window(self):
        scope_id = "seller:chat:item1"
        self.store.touch_conversation(scope_id, "seller", "chat", "buyer", "item1", 24)
        self.store.pause_conversation(scope_id, "manual")
        with self.store._connect() as conn:
            conn.execute(
                "UPDATE conversation_state SET last_activity=? WHERE scope_id=?",
                ("2020-01-01T00:00:00", scope_id),
            )
        state = self.store.touch_conversation(
            scope_id, "seller", "chat", "buyer", "item1", 1,
        )
        self.assertFalse(state["reset"])
        self.assertEqual("manual", state["state"])
        self.store.record_ai_reply(scope_id)
        self.assertEqual("manual", self.store.get_conversation_state(scope_id))

    def test_structured_query_context_is_persisted_and_cleared_on_reset(self):
        scope_id = "seller:chat:item1"
        self.store.touch_conversation(scope_id, "seller", "chat", "buyer", "item1", 24)
        context = {
            "price_filters": {
                "intent": "price", "meal_period": "lunch", "people_count": 3,
            }
        }
        self.store.update_query_context(scope_id, context)
        reopened = AppStore(self.store.db_path)
        state = reopened.touch_conversation(
            scope_id, "seller", "chat", "buyer", "item1", 24
        )
        self.assertEqual(context, state["query_context"])
        reopened.reset_conversation(scope_id)
        reset = reopened.touch_conversation(
            scope_id, "seller", "chat", "buyer", "item1", 24
        )
        self.assertEqual({}, reset["query_context"])

    def test_desktop_api_mode_is_non_interactive(self):
        api = XianyuApis(interactive=False)
        self.assertFalse(api.interactive)

    def test_discounted_total_question_is_not_treated_as_bargaining(self):
        decision = PolicyEngine(DEFAULT_POLICIES).evaluate("273优惠完多少", "正常凑单答复")
        self.assertEqual("allow", decision.action)

    def test_real_bargaining_still_uses_price_fallback(self):
        decision = PolicyEngine(DEFAULT_POLICIES).evaluate("还能再优惠一点吗", "")
        self.assertEqual("replace", decision.action)


if __name__ == "__main__":
    unittest.main()
