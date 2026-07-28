import unittest

from astrbot_plugin_muz_gateway.web_access import (
    CURL_TRANSFER_ARGS,
    _address_is_public,
    _parse_curl_headers,
    _validate_image_dimensions,
    extract_user_urls,
    html_to_readable_text,
    parse_bing_results,
    parse_duckduckgo_results,
    safe_search_subject,
    should_fetch_user_urls,
)


class WebAccessTests(unittest.TestCase):
    def test_curl_does_not_auto_decompress_untrusted_content(self):
        self.assertNotIn("--compressed", CURL_TRANSFER_ARGS)

    def test_rejects_huge_or_animated_images_before_decode(self):
        _validate_image_dimensions(1920, 1080, 1)
        for dimensions in ((10000, 10000, 1), (1920, 1080, 2)):
            with self.subTest(dimensions=dimensions), self.assertRaises(ValueError):
                _validate_image_dimensions(*dimensions)

    def test_blocks_private_special_and_loopback_addresses(self):
        for address in (
            "127.0.0.1",
            "10.0.0.1",
            "192.168.1.1",
            "169.254.169.254",
            "::1",
            "fc00::1",
        ):
            with self.subTest(address=address):
                self.assertFalse(_address_is_public(address))
        self.assertTrue(_address_is_public("1.1.1.1"))

    def test_html_reader_removes_scripts_and_caps_clean_text(self):
        title, text = html_to_readable_text(
            "<html><title>示例</title><script>secret()</script>"
            "<body><h1>标题</h1><p>正文 内容</p></body></html>"
        )

        self.assertEqual(title, "示例")
        self.assertIn("正文 内容", text)
        self.assertNotIn("secret", text)

    def test_parses_search_redirects_without_returning_markup(self):
        source = (
            '<a class="result__a" '
            'href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa">'
            "<b>示例</b> 页面</a>"
        )

        results = parse_duckduckgo_results(source)

        self.assertEqual(
            results,
            [{"title": "示例 页面", "url": "https://example.com/a"}],
        )

    def test_parses_bing_results_with_snippets(self):
        source = (
            '<li class="b_algo"><h2><a href="https://example.com/a">'
            "<b>示例</b> 页面</a></h2><div><p>一段 摘要</p></div></li>"
        )

        self.assertEqual(
            parse_bing_results(source),
            [
                {
                    "title": "示例 页面",
                    "url": "https://example.com/a",
                    "snippet": "一段 摘要",
                }
            ],
        )

    def test_parses_final_origin_headers_after_proxy_connect(self):
        raw = (
            b"HTTP/1.1 200 Connection established\r\n\r\n"
            b"HTTP/1.1 302 Found\r\n"
            b"Location: https://example.com/next\r\n"
            b"Content-Type: text/html\r\n\r\n"
        )

        status, headers = _parse_curl_headers(raw)

        self.assertEqual(status, 302)
        self.assertEqual(headers["location"], "https://example.com/next")

    def test_tools_are_bound_to_safe_current_user_input(self):
        self.assertEqual(
            extract_user_urls(
                "看 https://example.com/a 和 https://evil.example/?token=secret"
            ),
            ["https://example.com/a"],
        )
        self.assertEqual(
            safe_search_subject("查一下 AstrBot https://example.com"),
            "查一下 AstrBot",
        )
        self.assertIn(
            "[已省略]",
            safe_search_subject("查一下我的手机号13800138000相关信息"),
        )
        self.assertIn(
            "[已省略]",
            safe_search_subject("搜索身份证11010519491231002X"),
        )
        self.assertEqual(safe_search_subject("api_key=sk-secret"), "")
        self.assertEqual(safe_search_subject("sk-proj-abcdefghijk12345"), "")
        self.assertEqual(
            safe_search_subject("搜索 " + "AKIA" + "IOSFODNN7EXAMPLE"),
            "",
        )
        self.assertEqual(
            safe_search_subject("搜索 Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature"),
            "",
        )
        self.assertEqual(
            safe_search_subject("搜索 -----BEGIN PRIVATE KEY-----"),
            "",
        )
        self.assertEqual(safe_search_subject("普通闲聊，不要联网"), "")
        self.assertEqual(safe_search_subject("我现在很累"), "")
        for denied in (
            "请勿搜索张三住在北京市海淀区",
            "我不想联网，这只是私人聊天",
            "不要告诉我最新新闻",
            "禁止把这段话发到搜索引擎",
        ):
            with self.subTest(denied=denied):
                self.assertEqual(safe_search_subject(denied), "")
        self.assertEqual(
            safe_search_subject("能帮我搜一下 AstrBot 官网吗"),
            "能帮我搜一下 AstrBot 官网吗",
        )

    def test_link_fetch_requires_direction_or_explicit_non_negated_intent(self):
        self.assertFalse(
            should_fetch_user_urls(
                "不要打开 https://example.com",
                directed=True,
            )
        )
        for denied in (
            "不要联网 https://example.com",
            "无需发起网络请求：https://example.com",
            "别下载这个 URL：https://example.com",
        ):
            with self.subTest(denied=denied):
                self.assertFalse(
                    should_fetch_user_urls(
                        denied,
                        directed=True,
                    )
                )
        self.assertFalse(
            should_fetch_user_urls(
                "随手贴一下 https://example.com",
                directed=False,
            )
        )
        self.assertTrue(
            should_fetch_user_urls(
                "请分析链接 https://example.com",
                directed=False,
            )
        )
        self.assertTrue(
            should_fetch_user_urls(
                "https://example.com",
                directed=True,
            )
        )


if __name__ == "__main__":
    unittest.main()
