import unittest

from astrbot_plugin_muz_gateway.compact import (
    compact_request,
    estimate_request_tokens,
)


def long_text(length: int, marker: str = "x") -> str:
    return marker * length


class EstimateRequestTokensTests(unittest.TestCase):
    def test_counts_nested_text_without_mutating_messages(self):
        contexts = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hello"},
                    {"type": "image_url", "image_url": {"url": "x"}},
                ],
            }
        ]
        original = [dict(contexts[0])]

        count = estimate_request_tokens(
            contexts,
            prompt="world",
            system_prompt="system",
        )

        self.assertGreater(count, 0)
        self.assertEqual(contexts, original)


class CompactRequestTests(unittest.TestCase):
    def test_under_limit_is_unchanged(self):
        contexts = [
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "answer"},
        ]

        result = compact_request(
            contexts,
            prompt="new",
            system_prompt="",
            max_tokens=50_000,
            target_tokens=45_000,
        )

        self.assertFalse(result.compacted)
        self.assertEqual(result.contexts, contexts)
        self.assertIsNot(result.contexts, contexts)

    def test_drops_oldest_complete_round_and_keeps_latest_tool_chain(self):
        contexts = [
            {"role": "user", "content": long_text(100_000, "a")},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "latest question"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": "tool result",
            },
            {"role": "assistant", "content": "latest answer"},
        ]

        result = compact_request(
            contexts,
            prompt="follow-up",
            system_prompt="",
            max_tokens=20_000,
            target_tokens=10_000,
        )

        self.assertTrue(result.compacted)
        roles = [message["role"] for message in result.contexts]
        self.assertEqual(
            roles,
            ["user", "assistant", "tool", "assistant"],
        )
        self.assertEqual(
            result.contexts[0]["content"],
            "latest question",
        )

    def test_huge_current_prompt_is_trimmed_locally_to_target(self):
        result = compact_request(
            [],
            prompt=long_text(220_000),
            system_prompt="",
            max_tokens=50_000,
            target_tokens=45_000,
        )

        self.assertTrue(result.compacted)
        self.assertTrue(result.prompt.endswith("x" * 100))
        self.assertLessEqual(result.estimated_tokens, 45_000)

    def test_huge_system_prompt_cannot_bypass_strict_limit(self):
        result = compact_request(
            [],
            prompt="latest user request",
            system_prompt=long_text(220_000),
            max_tokens=50_000,
            target_tokens=45_000,
        )

        self.assertTrue(result.compacted)
        self.assertTrue(result.system_prompt.startswith("x" * 100))
        self.assertLessEqual(result.estimated_tokens, 45_000)

    def test_original_input_is_never_mutated(self):
        contexts = [
            {"role": "user", "content": long_text(100_000)},
            {"role": "assistant", "content": "answer"},
        ]
        snapshot = [dict(item) for item in contexts]

        compact_request(
            contexts,
            prompt=long_text(100_000),
            system_prompt="",
            max_tokens=10_000,
            target_tokens=8_000,
        )

        self.assertEqual(contexts, snapshot)


if __name__ == "__main__":
    unittest.main()
