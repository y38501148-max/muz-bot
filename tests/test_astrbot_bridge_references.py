import unittest

from astrbot_bridge_references import parse_forward_payload


class BridgeReferenceTests(unittest.TestCase):
    def test_inline_nested_forward_content_is_expanded(self):
        payload = [
            {
                "type": "node",
                "data": {
                    "name": "阿明",
                    "content": [
                        {"type": "text", "data": {"text": "外层消息"}},
                        {
                            "type": "forward",
                            "data": {
                                "content": [
                                    {
                                        "type": "node",
                                        "data": {
                                            "name": "小雨",
                                            "content": [
                                                {
                                                    "type": "text",
                                                    "data": {"text": "内层消息"},
                                                }
                                            ],
                                        },
                                    }
                                ]
                            },
                        },
                    ],
                },
            }
        ]

        snapshot = parse_forward_payload(payload)

        self.assertIn("阿明：外层消息", snapshot.text)
        self.assertIn("小雨：内层消息", snapshot.text)

    def test_single_node_segment_count_and_text_are_bounded(self):
        payload = {
            "messages": [
                {
                    "sender": {"nickname": "刷屏者"},
                    "message": [
                        {"type": "text", "data": {"text": ""}}
                        for _ in range(2_000)
                    ]
                    + [{"type": "text", "data": {"text": "不应处理到这里"}}],
                }
            ]
        }

        snapshot = parse_forward_payload(payload)

        self.assertLessEqual(len(snapshot.text), 8_000)
        self.assertNotIn("不应处理到这里", snapshot.text)


if __name__ == "__main__":
    unittest.main()
