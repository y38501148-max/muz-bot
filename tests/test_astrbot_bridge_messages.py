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

    def test_long_reply_is_not_truncated(self):
        source = "一二三四五六七八九十" * 4

        message = as_plain_text_message(source)

        self.assertEqual(message.extract_plain_text(), source)

    def test_multiple_sentences_are_preserved(self):
        source = (
            "先说结论，这件事可以做。后面这些冗长解释不该被发送出去，"
            "这些内容已经明显超过三十个字符。"
        )

        message = as_plain_text_message(source)

        self.assertEqual(message.extract_plain_text(), source)

    def test_reply_whitespace_is_compacted_without_flattening_lines(self):
        message = as_plain_text_message("  简短回答。\n\n不要   拉长格式。  ")

        self.assertEqual(message.extract_plain_text(), "简短回答。\n不要 拉长格式。")

    def test_quoted_sentence_and_followup_are_both_preserved(self):
        source = (
            "她说“可以。”后面的解释很长很长，而且已经超过了三十个字符……"
        )

        message = as_plain_text_message(source)

        self.assertEqual(message.extract_plain_text(), source)


if __name__ == "__main__":
    unittest.main()
