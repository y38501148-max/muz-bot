import unittest

from astrbot_bridge_references import parse_forward_payload


class BridgeReferenceTests(unittest.TestCase):
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
