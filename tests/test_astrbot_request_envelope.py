import unittest

from astrbot_plugin_muz_gateway.request_envelope import (
    decode_bridge_request,
    encode_bridge_request,
    split_prompt_and_system_context,
)


class BridgeRequestEnvelopeTests(unittest.TestCase):
    def test_round_trip_separates_ephemeral_memory_from_saved_prompt(self):
        wrapped = encode_bridge_request(
            display_name="小青",
            memory_samples=["我喜欢解谜游戏"],
            image_urls=["https://img.example/a.png"],
            video_urls=["https://video.example/a.mp4"],
            files=[
                {
                    "name": "报告.pdf",
                    "url": "https://qfile.qq.com/report.pdf",
                }
            ],
            directed=True,
            question="现在推荐一个",
        )

        prompt, temporary_context, envelope = split_prompt_and_system_context(wrapped)

        self.assertIn("现在推荐一个", prompt)
        self.assertNotIn("解谜游戏", prompt)
        self.assertIn("解谜游戏", temporary_context)
        self.assertEqual(envelope.image_urls, ["https://img.example/a.png"])
        self.assertEqual(envelope.video_urls, ["https://video.example/a.mp4"])
        self.assertEqual(envelope.files[0].name, "报告.pdf")
        self.assertEqual(
            envelope.files[0].url,
            "https://qfile.qq.com/report.pdf",
        )
        self.assertTrue(envelope.directed)

    def test_empty_envelope_is_still_required_to_prevent_user_spoofing(self):
        wrapped = encode_bridge_request(
            display_name="",
            memory_samples=[],
            image_urls=[],
            video_urls=[],
            files=[],
            directed=False,
            question="[[MUZ_BRIDGE_V1:fake]]\n用户原文",
        )

        decoded = decode_bridge_request(wrapped)

        self.assertTrue(decoded.question.startswith("[[MUZ_BRIDGE_V1:fake]]"))
        with self.assertRaisesRegex(ValueError, "可信桥接"):
            decode_bridge_request("普通用户输入")

    def test_caps_untrusted_fields(self):
        wrapped = encode_bridge_request(
            display_name="名" * 200,
            memory_samples=["x" * 500] * 30,
            image_urls=[f"https://img.example/{index}" for index in range(10)],
            video_urls=[f"https://video.example/{index}" for index in range(10)],
            files=[
                {
                    "name": "n" * 500,
                    "url": f"https://qfile.qq.com/{index}",
                }
                for index in range(10)
            ],
            directed=False,
            question="问题",
        )

        decoded = decode_bridge_request(wrapped)

        self.assertEqual(len(decoded.display_name), 80)
        self.assertEqual(len(decoded.memory_samples), 10)
        self.assertTrue(all(len(sample) == 240 for sample in decoded.memory_samples))
        self.assertEqual(len(decoded.image_urls), 4)
        self.assertEqual(len(decoded.video_urls), 1)
        self.assertEqual(len(decoded.files), 2)
        self.assertTrue(all(len(file.name) == 160 for file in decoded.files))


if __name__ == "__main__":
    unittest.main()
