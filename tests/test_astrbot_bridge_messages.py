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

    def test_reply_is_hard_limited_to_thirty_characters(self):
        message = as_plain_text_message("一二三四五六七八九十" * 4)

        self.assertEqual(len(message.extract_plain_text()), 30)

    def test_long_reply_prefers_a_complete_short_sentence(self):
        message = as_plain_text_message(
            "先说结论，这件事可以做。后面这些冗长解释不该被发送出去，"
            "这些内容已经明显超过三十个字符。"
        )

        self.assertEqual(message.extract_plain_text(), "先说结论，这件事可以做。")

    def test_reply_whitespace_is_compacted_before_limiting(self):
        message = as_plain_text_message("  简短回答。\n\n不要   拉长格式。  ")

        self.assertEqual(message.extract_plain_text(), "简短回答。 不要 拉长格式。")

    def test_sentence_truncation_keeps_closing_marks(self):
        message = as_plain_text_message(
            "她说“可以。”后面的解释很长很长，而且已经超过了三十个字符……"
        )

        self.assertEqual(message.extract_plain_text(), "她说“可以。”")


if __name__ == "__main__":
    unittest.main()
