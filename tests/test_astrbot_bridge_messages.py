import unittest

from astrbot_bridge_messages import as_plain_text_message


class AstrBotBridgeMessageTests(unittest.TestCase):
    def test_model_cq_syntax_stays_a_single_text_segment(self):
        message = as_plain_text_message(
            "不要解析 [CQ:at,qq=all] 或 [CQ:image,file=https://example.com/x]"
        )

        self.assertEqual(len(message), 1)
        self.assertEqual(message[0].type, "text")
        self.assertIn("[CQ:at,qq=all]", message[0].data["text"])

    def test_unsupported_markdown_bold_markers_are_removed(self):
        message = as_plain_text_message("这是**重点**，也是__重点__。")

        self.assertEqual(message.extract_plain_text(), "这是重点，也是重点。")


if __name__ == "__main__":
    unittest.main()
