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


if __name__ == "__main__":
    unittest.main()
