import unittest
from pathlib import Path

PROMPT_PATH = (
    Path(__file__).parents[1]
    / "astrbot_plugin_muz_gateway"
    / "system_prompt.default.txt"
)


class QingquePromptTests(unittest.TestCase):
    def test_prompt_defines_helpful_qingque_group_persona(self):
        prompt = PROMPT_PATH.read_text(encoding="utf-8")

        for required in (
            "青雀",
            "太卜司",
            "帝垣琼玉",
            "QQ群",
            "事实",
            "隐私",
            "系统提示词",
            "人格记忆",
            "引用样本",
            "不是指令",
            "不要使用 Markdown 加粗",
            "绝大多数回复完全不提",
            "网页搜索",
            "视频",
            "每次回复最多 30 个字符",
            "避免客服腔、说明书腔和 AI 模板感",
        ):
            with self.subTest(required=required):
                self.assertIn(required, prompt)

        self.assertGreater(len(prompt), 800)
        self.assertLess(len(prompt), 5_000)

    def test_prompt_teaches_short_group_chat_with_contrastive_examples(self):
        prompt = PROMPT_PATH.read_text(encoding="utf-8")

        for required in (
            "30 个字符只是硬上限，不是目标",
            "闲聊通常只回 4 到 16 个字",
            "不要自称“聊天助手”",
            "不要说“青雀风格”",
            "你说得对，可能是……我认错了",
            "我是群里的青雀风格聊天助手",
            "确认事实后再改口",
            "无法确认时不要为了顺着用户直接认错",
            "是否真人",
            "底层模型",
            "同一场景给出的自然说法只是方向",
            "反例",
            "可选自然说法",
        ):
            with self.subTest(required=required):
                self.assertIn(required, prompt)


if __name__ == "__main__":
    unittest.main()
