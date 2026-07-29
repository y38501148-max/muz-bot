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
            "不设固定字数上限",
            "避免客服腔、说明书腔和 AI 模板感",
            "轻微毒舌",
            "不做人身攻击",
            "严肃求助",
        ):
            with self.subTest(required=required):
                self.assertIn(required, prompt)

        self.assertGreater(len(prompt), 800)
        self.assertLess(len(prompt), 5_000)

    def test_prompt_teaches_short_group_chat_with_contrastive_examples(self):
        prompt = PROMPT_PATH.read_text(encoding="utf-8")

        for required in (
            "回复长度由内容决定",
            "惊讶、开心、得意、犯懒",
            "反应要有情绪",
            "不要每句都用同一个语气词",
            "活泼不等于强行撒娇",
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

        self.assertNotIn("30 个字符", prompt)


if __name__ == "__main__":
    unittest.main()
