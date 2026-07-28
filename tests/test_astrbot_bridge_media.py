import unittest
from types import SimpleNamespace

from nonebot.adapters.onebot.v11 import Message, MessageSegment

from astrbot_bridge_media import extract_event_media


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


if __name__ == "__main__":
    unittest.main()
