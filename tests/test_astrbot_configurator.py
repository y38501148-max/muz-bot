import unittest

from deploy.astrbot.configure import build_astrbot_config


class AstrBotConfiguratorTests(unittest.TestCase):
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
        self.assertTrue(result["provider_settings"]["web_search"])
        providers = {
            item["id"]: item for item in result["provider"]
        }
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


if __name__ == "__main__":
    unittest.main()
