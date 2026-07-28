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
        ):
            with self.subTest(required=required):
                self.assertIn(required, prompt)

        self.assertGreater(len(prompt), 800)
        self.assertLess(len(prompt), 5_000)


if __name__ == "__main__":
    unittest.main()
