import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from XianyuAgent import BaseAgent, IntentRouter, XianyuReplyBot


class ExplodingClassifier:
    def generate(self, **_kwargs):
        raise AssertionError("极速路由不应调用分类模型")


class CapturingCompletions:
    def __init__(self):
        self.request = None

    def create(self, **kwargs):
        self.request = kwargs
        message = SimpleNamespace(content="好的")
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class SemanticCompletions(CapturingCompletions):
    def create(self, **kwargs):
        self.request = kwargs
        content = (
            '{"questions":['
            '{"intent":"store","evidence":"济南可以用吗",'
            '"slots":{"store_query":"济南"},"uses_context":false,"confidence":0.96},'
            '{"intent":"purchase","evidence":"300的可以直接拍吗",'
            '"slots":{"sku_amount":"300"},"uses_context":false,"confidence":0.93}'
            '],"needs_clarification":false}'
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )


class FastReplyTests(unittest.TestCase):
    def test_unknown_intent_goes_directly_to_default_agent(self):
        router = IntentRouter(ExplodingClassifier())
        self.assertEqual("default", router.detect("这个怎么使用", "", ""))

    def test_history_keeps_only_recent_messages(self):
        bot = object.__new__(XianyuReplyBot)
        context = [
            {"role": "user", "content": f"问题{i}"}
            for i in range(12)
        ]
        with patch.dict(os.environ, {"AI_CONTEXT_MESSAGES": "4", "AI_CONTEXT_CHARS": "3500"}):
            history = bot.format_history(context)
        self.assertNotIn("问题7", history)
        self.assertIn("问题8", history)
        self.assertIn("问题11", history)

    def test_completion_uses_short_output_and_timeout(self):
        completions = CapturingCompletions()
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        agent = BaseAgent(client, "简短回答", lambda value: value)
        with patch.dict(
            os.environ,
            {"MODEL_NAME": "qwen-plus", "AI_MAX_TOKENS": "160", "AI_REPLY_TIMEOUT": "15"},
        ):
            reply = agent.generate("怎么用", "商品资料", "")
        self.assertEqual("好的", reply)
        self.assertEqual(160, completions.request["max_tokens"])
        self.assertEqual(15.0, completions.request["timeout"])

    def test_configured_global_prompt_is_in_every_model_system_message(self):
        completions = CapturingCompletions()
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        agent = BaseAgent(
            client, "简短回答", lambda value: value,
            "必须礼貌；不得承诺降价或退款。",
        )
        agent.generate("你好", "商品资料", "")
        system = completions.request["messages"][0]["content"]
        self.assertIn("用户配置的全局最高规则", system)
        self.assertIn("不得承诺降价或退款", system)

    def test_semantic_assistance_is_selective_and_does_not_replace_complete_single_hit(self):
        self.assertTrue(XianyuReplyBot.should_analyze_message("这说的是啥呢", None))
        self.assertFalse(XianyuReplyBot.should_analyze_message(
            "200元代金券多少钱", {"kind": "price", "decision": "allow"},
        ))
        self.assertTrue(XianyuReplyBot.should_analyze_message(
            "济南能用吗，还有300的能直接拍吗",
            {"kind": "stores", "decision": "allow"},
        ))
        self.assertFalse(XianyuReplyBot.should_analyze_message(
            "必须退款", {"kind": "refund_dispute", "decision": "review"},
        ))

    def test_semantic_payload_rejects_hallucinated_store_and_number(self):
        payload = {
            "questions": [{
                "intent": "store", "evidence": "这家能用吗",
                "slots": {"store_query": "上海万象城", "sku_amount": "500"},
                "uses_context": False, "confidence": 0.99,
            }],
        }
        self.assertIsNone(XianyuReplyBot._validate_semantic_payload(
            payload, "这家能用吗", "", {"last_store_query": "郑州大卫城"},
        ))

    def test_semantic_parser_returns_only_grounded_structure(self):
        completions = SemanticCompletions()
        bot = object.__new__(XianyuReplyBot)
        bot.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        bot.semantic_prompt = "只输出结构化JSON"
        with patch.dict(os.environ, {"AI_SEMANTIC_ASSIST_ENABLED": "true"}):
            result = bot.analyze_message(
                "济南可以用吗，300的可以直接拍吗", [],
                {"product_title": "测试代金券"},
            )
        self.assertEqual(["store", "purchase"], [q["intent"] for q in result["questions"]])
        self.assertEqual("济南", result["questions"][0]["slots"]["store_query"])
        self.assertEqual({"type": "json_object"}, completions.request["response_format"])


if __name__ == "__main__":
    unittest.main()
