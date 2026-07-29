import unittest
from types import SimpleNamespace

from nonebot.adapters.onebot.v11 import Message, MessageSegment

from astrbot_bridge_media import (
    describe_event_media,
    extract_event_files,
    extract_event_media,
    extract_reply_message_id,
)


class BridgeMediaTests(unittest.TestCase):
    def test_extracts_bounded_images_video_and_quoted_media(self):
        event = SimpleNamespace(
            message=Message(
                [
                    MessageSegment.image("https://img.example/1.png"),
                    MessageSegment(
                        "video",
                        {"url": "https://video.example/1.mp4", "file": "v.mp4"},
                    ),
                ]
            ),
            reply=SimpleNamespace(
                message=Message(
                    [
                        MessageSegment.image("https://img.example/2.png"),
                        MessageSegment.image("file:///etc/passwd"),
                    ]
                )
            ),
        )

        images, videos = extract_event_media(event)

        self.assertEqual(
            images,
            ["https://img.example/1.png", "https://img.example/2.png"],
        )
        self.assertEqual(videos, ["https://video.example/1.mp4"])

    def test_rejects_non_http_media_and_caps_counts(self):
        event = SimpleNamespace(
            message=Message(
                [
                    MessageSegment.image(f"https://img.example/{index}.png")
                    for index in range(8)
                ]
                + [
                    MessageSegment("video", {"url": "http://video.example/1.mp4"}),
                    MessageSegment("video", {"url": "http://video.example/2.mp4"}),
                ]
            ),
            reply=None,
        )

        images, videos = extract_event_media(event)

        self.assertEqual(len(images), 4)
        self.assertEqual(len(videos), 1)

    def test_describes_qq_and_market_faces(self):
        event = SimpleNamespace(
            message=Message(
                [
                    MessageSegment(
                        "face",
                        {
                            "id": "297",
                            "raw": {"faceText": "[拜谢]"},
                        },
                    ),
                    MessageSegment(
                        "mface",
                        {
                            "emoji_id": "123",
                            "summary": "猫猫震惊",
                        },
                    ),
                ]
            ),
            reply=None,
        )

        self.assertEqual(
            describe_event_media(event),
            "消息包含QQ表情：拜谢；表情包：猫猫震惊",
        )

    def test_extracts_reply_id_from_original_message(self):
        event = SimpleNamespace(
            message=Message(
                [
                    MessageSegment("reply", {"id": "549327693"}),
                    MessageSegment.text("这是什么"),
                ]
            ),
            reply=None,
        )

        self.assertEqual(extract_reply_message_id(event), "549327693")

    def test_extracts_quoted_file_metadata_and_url(self):
        event = SimpleNamespace(message=Message("请分析"), reply=None)
        quoted = Message(
            [
                MessageSegment(
                    "file",
                    {
                        "file": "报告.pdf",
                        "file_id": "file-123",
                        "busid": 102,
                        "url": "https://qfile.qq.com/report.pdf",
                    },
                )
            ]
        )

        files = extract_event_files(event, reply_message=quoted)

        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].name, "报告.pdf")
        self.assertEqual(files[0].file_id, "file-123")
        self.assertEqual(files[0].busid, 102)
        self.assertEqual(files[0].url, "https://qfile.qq.com/report.pdf")


if __name__ == "__main__":
    unittest.main()
