import asyncio
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from astrbot_plugin_muz_gateway.document_extract import (
    extract_document_text,
    redact_sensitive_document_text,
)
from astrbot_plugin_muz_gateway.document_process import (
    extract_document_text_isolated,
)


class DocumentExtractTests(unittest.TestCase):
    def test_extracts_utf8_text_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "message.bin"
            path.write_text("第一行\n第二行", encoding="utf-8")

            result = extract_document_text(path, "说明.txt")

        self.assertEqual(result, "第一行\n第二行")

    def test_extracts_docx_text_without_executing_embedded_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "document.bin"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(
                    "word/document.xml",
                    (
                        '<?xml version="1.0"?>'
                        '<w:document xmlns:w="urn:test"><w:body>'
                        "<w:p><w:r><w:t>项目总结</w:t></w:r></w:p>"
                        "<w:p><w:r><w:t>进度正常</w:t></w:r></w:p>"
                        "</w:body></w:document>"
                    ),
                )

            result = extract_document_text(path, "总结.docx")

        self.assertIn("项目总结", result)
        self.assertIn("进度正常", result)

    def test_rejects_unsupported_or_oversized_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unknown.bin"
            path.write_bytes(b"\x00\x01\x02")

            with self.assertRaisesRegex(ValueError, "不支持"):
                extract_document_text(path, "程序.exe")
            with self.assertRaisesRegex(ValueError, "过大"):
                extract_document_text(path, "文本.txt", max_bytes=2)

    def test_rejects_zip_bomb_member(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "document.bin"
            with zipfile.ZipFile(
                path,
                "w",
                compression=zipfile.ZIP_DEFLATED,
            ) as archive:
                archive.writestr(
                    "word/document.xml",
                    b"A" * (2 * 1024 * 1024),
                )

            with self.assertRaisesRegex(ValueError, "压缩比"):
                extract_document_text(path, "压缩包.docx")

    def test_redacts_credentials_and_personal_identifiers(self):
        value = (
            "邮箱 alice@example.com，手机 13800138000，"
            "密钥 sk-test_abcdefghijklmnopqrstuvwxyz，"
            "身份证 11010519491231002X"
        )

        result = redact_sensitive_document_text(value)

        self.assertNotIn("alice@example.com", result)
        self.assertNotIn("13800138000", result)
        self.assertNotIn("sk-test_", result)
        self.assertNotIn("11010519491231002X", result)
        self.assertGreaterEqual(result.count("[已隐藏]"), 4)

    def test_isolated_worker_extracts_text(self):
        async def scenario(path):
            return await extract_document_text_isolated(path, "说明.txt")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "message.bin"
            path.write_text("隔离进程读取成功", encoding="utf-8")

            result = asyncio.run(scenario(path))

        self.assertEqual(result, "隔离进程读取成功")

    def test_isolated_worker_is_killed_and_reaped_on_timeout(self):
        class HangingStdout:
            async def read(self, _limit):
                await asyncio.Event().wait()

        class FakeProcess:
            def __init__(self):
                self.stdout = HangingStdout()
                self.returncode = None
                self.killed = False
                self.waited = False

            def kill(self):
                self.killed = True
                self.returncode = -9

            async def wait(self):
                self.waited = True
                return self.returncode

        async def scenario(process):
            with patch(
                "astrbot_plugin_muz_gateway.document_process."
                "asyncio.create_subprocess_exec",
                return_value=process,
            ), self.assertRaisesRegex(ValueError, "超时"):
                await extract_document_text_isolated(
                    Path("/tmp/example"),
                    "说明.txt",
                    timeout_seconds=0.01,
                )

        process = FakeProcess()
        asyncio.run(scenario(process))

        self.assertTrue(process.killed)
        self.assertTrue(process.waited)


if __name__ == "__main__":
    unittest.main()
