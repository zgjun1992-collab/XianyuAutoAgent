import base64
import json
import unittest
from unittest.mock import AsyncMock

from main import XianyuLive


class _Store:
    def __init__(self):
        self.audit_updates = []
        self.reply_scopes = []

    @staticmethod
    def order_payment_notice(item_id):
        return (
            f"【商品信息】\n半秋山100元代金券：售价66.8元，发100元券1张\n"
            "【发货提醒】\n付款后会自动发货。\n"
            "【退换货政策】\n非卡券质量问题的退款需扣除5%手续费。"
        )

    def update_audit(self, audit_id, status, final_reply=""):
        self.audit_updates.append((audit_id, status, final_reply))

    def record_ai_reply(self, scope_id):
        self.reply_scopes.append(scope_id)


class _Context:
    def __init__(self):
        self.messages = []

    def add_message_by_chat(self, *args):
        self.messages.append(args)


class MainWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def make_live(self):
        live = XianyuLive.__new__(XianyuLive)
        live.myid = "seller"
        live.app_store = _Store()
        live.context_manager = _Context()
        live.event_callback = None
        live._order_notice_scopes = set()
        live._buyer_routes = {"buyer-1": ("chat-1", "item-1")}
        live.send_msg = AsyncMock()
        return live

    async def test_waiting_payment_uses_known_buyer_route_and_sends_once(self):
        live = self.make_live()
        event = {"1": "buyer-1@goofish", "3": {"redReminder": "等待买家付款"}}
        self.assertTrue(await live.handle_order_reminder(event, object()))
        live.send_msg.assert_awaited_once()
        args = live.send_msg.await_args.args
        self.assertEqual(("chat-1", "buyer-1"), args[1:3])
        self.assertIn("半秋山100元代金券", args[3])
        self.assertIn("非卡券质量问题的退款", args[3])
        self.assertIn("付款后会自动发货", args[3])
        self.assertEqual("等待买家付款", live._order_routes["seller:chat-1:item-1"]["status"])

        self.assertTrue(await live.handle_order_reminder(event, object()))
        self.assertEqual(1, live.send_msg.await_count)

    async def test_paid_order_status_is_saved_without_sending_payment_notice(self):
        live = self.make_live()
        event = {"1": "buyer-1@goofish", "3": {"redReminder": "等待卖家发货"}}
        self.assertTrue(await live.handle_order_reminder(event, object()))
        live.send_msg.assert_not_awaited()
        self.assertEqual("等待卖家发货", live._order_routes["seller:chat-1:item-1"]["status"])

    async def test_waiting_payment_card_text_is_detected_without_red_reminder(self):
        live = self.make_live()
        event = {"1": "buyer-1@goofish", "3": {"reminderContent": "我已拍下，待付款"}}
        self.assertTrue(await live.handle_order_reminder(event, object()))
        live.send_msg.assert_awaited_once()

    def test_recall_and_paid_amount_helpers(self):
        self.assertTrue(XianyuLive.is_recall_message("对方撤回了一条消息"))
        self.assertFalse(XianyuLive.is_recall_message("正常消息"))
        payload = {"order": {"actualPaidAmount": "67.90"}}
        self.assertEqual("67.90", XianyuLive.extract_actual_paid_amount(payload))

    def test_buyer_reply_sanitizer_removes_internal_and_coverage_terms(self):
        text = XianyuLive.sanitize_buyer_reply("SKU来自数据库，全国通用，所有门店都能用")
        self.assertNotIn("SKU", text.upper())
        self.assertNotIn("数据库", text)
        self.assertNotIn("全国通用", text)
        self.assertNotIn("所有门店都能用", text)

    def test_buyer_reply_sanitizer_keeps_safe_coverage_denial_natural(self):
        text = XianyuLive.sanitize_buyer_reply("当前商品不是全国通用。")
        self.assertEqual("当前商品仅限已配置的可用门店使用。", text)

    def test_buyer_reply_sanitizer_replaces_an_unapproved_promise(self):
        text = XianyuLive.sanitize_buyer_reply("请提供订单号，我帮您补发。")
        self.assertIn("人工核实", text)
        self.assertIn("72小时", text)
        self.assertNotIn("补发", text)

    def test_media_dependent_short_text_detection(self):
        self.assertTrue(XianyuLive.is_media_dependent_text("可以用么"))
        self.assertTrue(XianyuLive.is_media_dependent_text("多少钱"))
        self.assertFalse(XianyuLive.is_media_dependent_text("深圳龙岗万科里可以用吗"))
        self.assertFalse(XianyuLive.is_media_dependent_text("账单390元怎么买"))

    def test_media_markers_are_not_discarded_as_bracket_system_messages(self):
        live = XianyuLive.__new__(XianyuLive)
        self.assertFalse(live.is_bracket_system_message("[图片]"))
        self.assertFalse(live.is_bracket_system_message("[语音]"))
        self.assertTrue(live.is_bracket_system_message("[系统通知]"))

    def test_inbound_media_payload_without_text_is_detected(self):
        live = XianyuLive.__new__(XianyuLive)
        image = {"contentType": 2, "senderUserId": "buyer"}
        voice = {"messageType": "voice", "voiceUrl": "https://example.invalid/a"}
        self.assertEqual("[图片]", live.inbound_media_marker(image))
        self.assertEqual("[语音]", live.inbound_media_marker(voice))
        self.assertTrue(live.is_chat_message({"1": {"10": image}}))

    def test_normal_text_payload_with_thumbnail_is_not_media(self):
        payload = {
            "contentType": 1,
            "reminderContent": "多少钱",
            "itemImageUrl": "https://example.invalid/item.jpg",
        }
        self.assertEqual("", XianyuLive.inbound_media_marker(payload))

    def test_model_number_grounding_blocks_only_unverified_facts(self):
        self.assertEqual(
            "",
            XianyuLive.model_reply_grounding_issue(
                "100元券多少钱", "100元代金券售价54元", [], "100元券售价54元。"
            ),
        )
        reason = XianyuLive.model_reply_grounding_issue(
            "这个多少钱", "100元代金券售价54元", [], "售价55元。"
        )
        self.assertIn("55元", reason)

    def test_grounding_failure_uses_clarification_instead_of_fake_manual_transfer(self):
        price = XianyuLive.grounding_fallback_reply("388多少")
        self.assertIn("具体商品规格", price)
        self.assertNotIn("人工", price)
        store = XianyuLive.grounding_fallback_reply("万达店在哪里")
        self.assertIn("城市和具体店名", store)
        self.assertNotIn("人工", store)

    def test_ordinary_review_does_not_lock_the_whole_conversation(self):
        self.assertFalse(XianyuLive.requires_persistent_manual_takeover(
            {"kind": "model_grounding_guard", "decision": "review"}, "200多少"
        ))
        self.assertFalse(XianyuLive.requires_persistent_manual_takeover(
            {"kind": "stores", "decision": "review"}, "湖州有门店吗"
        ))
        self.assertTrue(XianyuLive.requires_persistent_manual_takeover(
            {"kind": "code_operation_review", "decision": "review"}, "帮我补发券码"
        ))

    def test_disabled_purchase_order_first_reply_silences_only_greetings(self):
        product = {"coupon_type": "purchase_order", "first_reply_enabled": False}
        for message in ("你好", "您好", "在吗？", "hello"):
            self.assertTrue(
                XianyuLive.should_silence_disabled_purchase_order_greeting(product, message)
            )
        self.assertFalse(
            XianyuLive.should_silence_disabled_purchase_order_greeting(product, "怎么付款")
        )
        self.assertFalse(XianyuLive.should_silence_disabled_purchase_order_greeting(
            {"coupon_type": "purchase_order", "first_reply_enabled": True}, "你好"
        ))
        self.assertTrue(XianyuLive.requires_persistent_manual_takeover(
            {"kind": "refund_dispute", "decision": "review"}, "必须马上退款"
        ))

    def test_pure_greeting_classifier_does_not_treat_questions_as_greetings(self):
        self.assertTrue(XianyuLive.should_send_first_reply("你好", {"kind": "greeting"}))
        for message in ("周末能用吗", "南昌万象城能用吗", "两个人多少钱", "怎么付款"):
            with self.subTest(message=message):
                self.assertFalse(XianyuLive.should_send_first_reply(message, {"kind": "stores"}))

    def test_enabled_first_reply_attaches_to_first_normal_question(self):
        for kind in ("coupon_catalog", "stores", "day_use", "price", "greeting"):
            with self.subTest(kind=kind):
                self.assertTrue(XianyuLive.should_attach_first_reply(True, "商品首次回复", kind))
        self.assertFalse(XianyuLive.should_attach_first_reply(False, "商品首次回复", "stores"))
        self.assertFalse(XianyuLive.should_attach_first_reply(True, "", "stores"))
        self.assertFalse(XianyuLive.should_attach_first_reply(True, "商品首次回复", "refund_quality"))

    def test_manual_first_reply_is_not_replaced_by_commitment_filter(self):
        live = self.make_live()
        manual = "请提供订单号，我帮您补发。"
        self.assertEqual(manual, live.prepare_product_first_reply({
            "first_reply_text": manual,
            "first_reply_manual": True,
        }))
        automatic = live.prepare_product_first_reply({
            "first_reply_text": manual,
            "first_reply_manual": False,
        })
        self.assertNotEqual(manual, automatic)

    async def test_manual_purchase_order_first_reply_bypasses_transport_resanitizing(self):
        live = XianyuLive.__new__(XianyuLive)
        live.myid = "seller"
        ws = AsyncMock()
        reply = "买家发桌码，卖家改价后代付账单。"
        await XianyuLive.send_msg(
            live, ws, "chat-1", "buyer-1", reply, sanitize=False
        )
        payload = json.loads(ws.send.await_args.args[0])
        encoded = payload["body"][0]["content"]["custom"]["data"]
        sent = json.loads(base64.b64decode(encoded).decode("utf-8"))["text"]["text"]
        self.assertEqual(reply, sent)
        self.assertNotIn("72小时", sent)

    async def test_normal_transport_reply_still_uses_commitment_filter(self):
        live = XianyuLive.__new__(XianyuLive)
        live.myid = "seller"
        ws = AsyncMock()
        await XianyuLive.send_msg(
            live, ws, "chat-1", "buyer-1", "请提供订单号，我帮您补发。"
        )
        payload = json.loads(ws.send.await_args.args[0])
        encoded = payload["body"][0]["content"]["custom"]["data"]
        sent = json.loads(base64.b64decode(encoded).decode("utf-8"))["text"]["text"]
        self.assertIn("人工核实", sent)
        self.assertIn("72小时", sent)

    def test_ai_and_first_reply_switches_are_independent(self):
        product = {
            "enabled": False,
            "first_reply_enabled": True,
            "first_reply_text": "代买单首次说明",
            "first_reply_manual": True,
        }
        live = self.make_live()
        self.assertEqual("代买单首次说明", live.prepare_product_first_reply(product))
        self.assertTrue(XianyuLive.should_attach_first_reply(
            product["first_reply_enabled"], product["first_reply_text"], "greeting"
        ))
        product["first_reply_enabled"] = False
        self.assertFalse(XianyuLive.should_attach_first_reply(
            product["first_reply_enabled"], product["first_reply_text"], "greeting"
        ))

    def test_manual_takeover_greeting_is_still_classified_as_a_greeting(self):
        # The live workflow uses this narrow gate before its manual-review
        # notice, while retaining takeover for subsequent substantive issues.
        self.assertTrue(XianyuLive.should_send_first_reply("你好"))
        self.assertFalse(XianyuLive.should_send_first_reply("券码核销失败"))

    def test_message_id_is_preferred_for_deduplication(self):
        first = {"1": {"10": {"reminderUrl": "x?messageId=first-1"}}}
        second = {"1": {"10": {"messageId": "second-2"}}}
        self.assertEqual("first-1", XianyuLive.inbound_message_id(
            first, first["1"]["10"], first["1"]["10"]["reminderUrl"]
        ))
        self.assertEqual("second-2", XianyuLive.inbound_message_id(second, second["1"]["10"]))

    async def test_auto_reply_is_recorded_only_after_transport_success(self):
        live = self.make_live()
        await live._send_and_record_auto_reply(
            object(), "chat-1", "buyer-1", "scope-1", "item-1", 7, "已发送内容"
        )
        self.assertEqual([(7, "sent", "已发送内容")], live.app_store.audit_updates)
        self.assertEqual(["scope-1"], live.app_store.reply_scopes)
        self.assertEqual("assistant", live.context_manager.messages[0][3])

    async def test_failed_auto_reply_is_not_added_to_context_or_reply_count(self):
        live = self.make_live()
        live.send_msg.side_effect = ConnectionError("websocket closed")
        with self.assertRaises(ConnectionError):
            await live._send_and_record_auto_reply(
                object(), "chat-1", "buyer-1", "scope-1", "item-1", 8, "未发送内容"
            )
        self.assertEqual([(8, "failed", "未发送内容")], live.app_store.audit_updates)
        self.assertEqual([], live.app_store.reply_scopes)
        self.assertEqual([], live.context_manager.messages)


if __name__ == "__main__":
    unittest.main()
