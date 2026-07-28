import json
import tempfile
import unittest
from pathlib import Path

from deploy.astrbot.configure import (
    build_astrbot_config,
    configure,
    normalize_openai_base_url,
    normalize_proxy_url,
)


class AstrBotConfiguratorTests(unittest.TestCase):
    def test_top_level_and_endpoint_urls_become_openai_v1_base(self):
        cases = {
            "https://api.example.com": "https://api.example.com/v1",
            "https://api.example.com/": "https://api.example.com/v1",
            "https://api.example.com/v1": "https://api.example.com/v1",
            (
                "https://api.example.com/v1/chat/completions"
            ): "https://api.example.com/v1",
            ("https://api.example.com/v1/responses"): "https://api.example.com/v1",
            ("https://api.example.com/chat/completions"): "https://api.example.com/v1",
            "https://api.example.com/responses": "https://api.example.com/v1",
        }
        for raw_url, expected in cases.items():
            with self.subTest(raw_url=raw_url):
                self.assertEqual(normalize_openai_base_url(raw_url), expected)

    def test_plain_http_is_only_allowed_for_loopback(self):
        self.assertEqual(
            normalize_openai_base_url("http://127.0.0.1:8000"),
            "http://127.0.0.1:8000/v1",
        )
        self.assertEqual(
            normalize_openai_base_url("http://localhost:8000/v1"),
            "http://localhost:8000/v1",
        )
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            normalize_openai_base_url("http://api.example.com")

    def test_adds_three_ordered_providers_and_preserves_unrelated_config(self):
        current = {
            "config_version": 2,
            "timezone": "Asia/Shanghai",
            "platform": [{"id": "leave-me-alone"}],
            "provider": [{"id": "existing-provider"}],
            "provider_settings": {"web_search": True},
        }
        provider_specs = [
            {
                "id": "muz-primary",
                "api_base": "https://one.example/v1",
                "model": "model-a",
                "key_env": "MUZ_LLM_PRIMARY_KEY",
                "reasoning_effort": "high",
                "proxy": "http://192.168.16.1:17890",
            },
            {
                "id": "muz-secondary",
                "api_base": "https://two.example/v1",
                "model": "model-b",
                "key_env": "MUZ_LLM_SECONDARY_KEY",
            },
            {
                "id": "muz-tertiary",
                "api_base": "https://three.example/v1",
                "model": "model-c",
                "key_env": "MUZ_LLM_TERTIARY_KEY",
            },
        ]

        result = build_astrbot_config(current, provider_specs)

        self.assertEqual(result["platform"], [{"id": "leave-me-alone"}])
        providers = {item["id"]: item for item in result["provider"]}
        self.assertIn("existing-provider", providers)
        self.assertEqual(
            providers["muz-primary"]["key"],
            ["$MUZ_LLM_PRIMARY_KEY"],
        )
        self.assertEqual(
            providers["muz-primary"]["api_base"],
            "https://one.example/v1",
        )
        self.assertEqual(
            providers["muz-primary"]["custom_extra_body"],
            {"reasoning_effort": "high"},
        )
        self.assertEqual(
            providers["muz-primary"]["proxy"],
            "http://192.168.16.1:17890",
        )
        self.assertEqual(providers["muz-tertiary"]["proxy"], "")
        self.assertEqual(
            result["provider_settings"]["default_provider_id"],
            "muz-primary",
        )
        self.assertEqual(
            result["provider_settings"]["fallback_chat_models"],
            ["muz-secondary", "muz-tertiary"],
        )
        self.assertEqual(
            result["provider_settings"]["context_limit_reached_strategy"],
            "truncate_by_turns",
        )
        self.assertFalse(result["provider_settings"]["web_search"])
        self.assertEqual(
            result["provider_settings"]["computer_use_runtime"],
            "none",
        )
        self.assertFalse(
            result["provider_settings"]["proactive_capability"]["add_cron_tools"]
        )
        self.assertFalse(result["subagent_orchestrator"]["main_enable"])
        self.assertEqual(
            providers["muz-primary"]["max_context_tokens"],
            60_975,
        )

    def test_requires_exactly_three_unique_provider_specs(self):
        with self.assertRaisesRegex(ValueError, "三个"):
            build_astrbot_config({}, [])

        duplicate = [
            {
                "id": "same",
                "api_base": "https://one.example/v1",
                "model": "a",
                "key_env": "KEY_A",
            },
            {
                "id": "same",
                "api_base": "https://two.example/v1",
                "model": "b",
                "key_env": "KEY_B",
            },
            {
                "id": "third",
                "api_base": "https://three.example/v1",
                "model": "c",
                "key_env": "KEY_C",
            },
        ]
        with self.assertRaisesRegex(ValueError, "唯一"):
            build_astrbot_config({}, duplicate)

    def test_rejects_non_http_base_url_and_invalid_env_name(self):
        specs = [
            {
                "id": "one",
                "api_base": "file:///tmp/api",
                "model": "a",
                "key_env": "KEY_A",
            },
            {
                "id": "two",
                "api_base": "https://two.example/v1",
                "model": "b",
                "key_env": "bad-key",
            },
            {
                "id": "three",
                "api_base": "https://three.example/v1",
                "model": "c",
                "key_env": "KEY_C",
            },
        ]

        with self.assertRaises(ValueError):
            build_astrbot_config({}, specs)

    def test_rejects_unsupported_proxy_scheme(self):
        specs = [
            {
                "id": provider_id,
                "api_base": f"https://{index}.example/v1",
                "model": f"model-{index}",
                "key_env": f"KEY_{index}",
                "proxy": "file:///tmp/proxy" if index == 1 else "",
            }
            for index, provider_id in enumerate(
                ("muz-primary", "muz-secondary", "muz-tertiary"),
                start=1,
            )
        ]

        with self.assertRaisesRegex(ValueError, "proxy"):
            build_astrbot_config({}, specs)

    def test_normalizes_supported_proxy_urls(self):
        self.assertEqual(
            normalize_proxy_url("HTTP://192.168.16.1:17890/"),
            "http://192.168.16.1:17890",
        )
        self.assertEqual(
            normalize_proxy_url("socks5://proxy.internal:1080"),
            "socks5://proxy.internal:1080",
        )
        self.assertEqual(normalize_proxy_url(""), "")

    def test_rejects_unsafe_or_malformed_proxy_urls(self):
        invalid_urls = (
            "http://user:secret@proxy.internal:8080",
            "http://:7890",
            "http://proxy.internal:notaport",
            "http://proxy host:8080",
            "http://proxy.internal:8080/unexpected",
        )
        for proxy_url in invalid_urls:
            subtest = self.subTest(proxy_url=proxy_url)
            rejects_proxy = self.assertRaisesRegex(ValueError, "proxy")
            with subtest, rejects_proxy:
                normalize_proxy_url(proxy_url)

    def test_configure_accepts_astrbot_utf8_bom_config(self):
        provider_specs = [
            {
                "id": provider_id,
                "api_base": f"https://{index}.example/v1",
                "model": f"model-{index}",
                "key_env": f"KEY_{index}",
            }
            for index, provider_id in enumerate(
                ("muz-primary", "muz-secondary", "muz-tertiary"),
                start=1,
            )
        ]
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "cmd_config.json"
            providers_path = Path(directory) / "providers.json"
            config_path.write_text(
                json.dumps({"config_version": 2}),
                encoding="utf-8-sig",
            )
            providers_path.write_text(
                json.dumps(provider_specs),
                encoding="utf-8",
            )

            configure(config_path, providers_path)

            result = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(
                result["provider_settings"]["default_provider_id"],
                "muz-primary",
            )


if __name__ == "__main__":
    unittest.main()
