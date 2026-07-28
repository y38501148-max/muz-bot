import unittest

from astrbot_plugin_muz_gateway.video_frames import (
    MEDIA_INPUT_ARGS,
    _validate_probe,
)


class VideoProbeValidationTests(unittest.TestCase):
    def test_media_subprocess_only_accepts_stdin_pipe_protocol(self):
        self.assertEqual(
            MEDIA_INPUT_ARGS,
            ("-protocol_whitelist", "pipe", "-i", "pipe:0"),
        )

    def test_accepts_short_4k_or_smaller_video(self):
        _validate_probe(
            {
                "streams": [{"codec_type": "video", "width": 1920, "height": 1080}],
                "format": {"duration": "30.5"},
            }
        )

    def test_rejects_missing_video_huge_frames_and_long_duration(self):
        invalid = (
            {"streams": [], "format": {"duration": "1"}},
            {
                "streams": [{"codec_type": "video", "width": 7680, "height": 4320}],
                "format": {"duration": "10"},
            },
            {
                "streams": [{"codec_type": "video", "width": 1280, "height": 720}],
                "format": {"duration": "121"},
            },
        )
        for metadata in invalid:
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                _validate_probe(metadata)


if __name__ == "__main__":
    unittest.main()
