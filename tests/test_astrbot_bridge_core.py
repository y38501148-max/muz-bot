import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from astrbot_bridge_core import (
    AstrBotApiError,
    AstrBotClient,
    BridgeConfig,
    ConversationInFlightGate,
    ConversationRateLimiter,
    JsonSessionStore,
    build_conversation_identity,
    consume_sse_lines,
    is_status_command,
    load_bridge_config,
    should_passively_reply,
)


class BridgeConfigTests(unittest.TestCase):
    def test_disabled_config_can_be_committed_without_secret(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "ENABLED": False,
                        "ASTRBOT_BASE_URL": "http://127.0.0.1:6185/",
                        "API_KEY": "",
                    }
                ),
                encoding="utf-8",
            )

            config = load_bridge_config(path)

        self.assertFalse(config.enabled)
        self.assertEqual(config.base_url, "http://127.0.0.1:6185")
        self.assertEqual(config.passive_trigger_probability, 0.42)

    def test_enabled_config_requires_api_key(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            path.write_text(
                json.dumps({"ENABLED": True, "API_KEY": ""}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "API_KEY"):
                load_bridge_config(path)

    def test_base_url_rejects_embedded_credentials(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            path.write_text(
                json.dumps({"ASTRBOT_BASE_URL": ("https://secret@example.com")}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "凭据"):
                load_bridge_config(path)

    def test_base_url_rejects_external_plain_http(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            path.write_text(
                json.dumps({"ASTRBOT_BASE_URL": "http://astrbot.example.com"}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "HTTPS"):
                load_bridge_config(path)

    def test_passive_probability_is_configurable_and_bounded(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            path.write_text(
                json.dumps({"PASSIVE_TRIGGER_PROBABILITY": 0.25}),
                encoding="utf-8",
            )

            self.assertEqual(
                load_bridge_config(path).passive_trigger_probability,
                0.25,
            )

            path.write_text(
                json.dumps({"PASSIVE_TRIGGER_PROBABILITY": 1.01}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "0 到 1"):
                load_bridge_config(path)


class TriggerPolicyTests(unittest.TestCase):
    def test_ai_command_only_keeps_chinese_status_action(self):
        self.assertTrue(is_status_command(" 状态 "))
        self.assertFalse(is_status_command("status"))
        self.assertFalse(is_status_command("你好"))
        self.assertFalse(is_status_command(""))

    def test_passive_trigger_uses_probability_and_skips_direct_messages(self):
        self.assertTrue(
            should_passively_reply(
                "今天吃什么？",
                directed=False,
                probability=0.42,
                sample=0.419,
            )
        )
        self.assertFalse(
            should_passively_reply(
                "今天吃什么？",
                directed=False,
                probability=0.42,
                sample=0.42,
            )
        )
        self.assertFalse(
            should_passively_reply(
                "引用机器人",
                directed=True,
                probability=1,
                sample=0,
            )
        )

    def test_passive_trigger_skips_empty_and_command_messages(self):
        for message in ("", "   ", "/ai 状态", " /help"):
            with self.subTest(message=message):
                self.assertFalse(
                    should_passively_reply(
                        message,
                        directed=False,
                        probability=1,
                        sample=0,
                    )
                )


class ConversationIdentityTests(unittest.TestCase):
    def test_group_context_is_shared_but_private_context_is_per_user(self):
        group = build_conversation_identity(
            user_id="10001",
            group_id="778899",
        )
        same_group_other_user = build_conversation_identity(
            user_id="10002",
            group_id="778899",
        )
        private = build_conversation_identity(user_id="10001", group_id=None)

        self.assertEqual(group, same_group_other_user)
        self.assertEqual(group.key, "group:778899")
        self.assertEqual(group.username, "qq-group")
        self.assertEqual(private.key, "private:10001")
        self.assertEqual(private.username, "qq-user")


class ConversationRateLimiterTests(unittest.TestCase):
    def test_limits_each_conversation_independently(self):
        limiter = ConversationRateLimiter(min_interval_seconds=3)

        self.assertEqual(limiter.check("group:1", now=10), 0)
        self.assertEqual(limiter.check("group:2", now=11), 0)
        self.assertAlmostEqual(limiter.check("group:1", now=12), 1)
        self.assertEqual(limiter.check("group:1", now=13), 0)


class ConversationInFlightGateTests(unittest.TestCase):
    def test_rejects_same_conversation_and_global_overflow_without_queueing(self):
        gate = ConversationInFlightGate(max_concurrent=2)

        self.assertTrue(gate.try_enter("group:1"))
        self.assertFalse(gate.try_enter("group:1"))
        self.assertTrue(gate.try_enter("group:2"))
        self.assertFalse(gate.try_enter("group:3"))

        gate.leave("group:1")
        self.assertTrue(gate.try_enter("group:3"))


class JsonSessionStoreTests(unittest.TestCase):
    def test_session_survives_restart_and_reset_changes_only_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sessions.json"
            store = JsonSessionStore(path)
            first = store.get_or_create("group:1")
            other = store.get_or_create("group:2")

            reloaded = JsonSessionStore(path)
            self.assertEqual(reloaded.get_or_create("group:1"), first)
            self.assertEqual(reloaded.get_or_create("group:2"), other)

            replacement = reloaded.reset("group:1")

            self.assertNotEqual(replacement, first)
            self.assertEqual(reloaded.get_or_create("group:2"), other)

    def test_corrupt_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sessions.json"
            path.write_text("{broken", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "会话状态"):
                JsonSessionStore(path).get_or_create("group:1")


class SseTests(unittest.TestCase):
    def test_plain_stream_is_joined_and_control_events_are_ignored(self):
        lines = [
            'data: {"type":"session_id","session_id":"abc"}',
            "",
            'data: {"type":"plain","data":"你","streaming":true}',
            "",
            'data: {"type":"plain","data":"好","streaming":true}',
            "",
            'data: {"type":"plain","data":"tool","chain_type":"tool_call"}',
            "",
            'data: {"type":"end","data":""}',
            "",
        ]

        result = consume_sse_lines(lines)

        self.assertEqual(result.text, "你好")
        self.assertEqual(result.session_id, "abc")

    def test_error_event_raises_useful_exception(self):
        lines = [
            'data: {"type":"error","data":"all providers unavailable"}',
            "",
        ]

        with self.assertRaisesRegex(
            AstrBotApiError,
            "all providers unavailable",
        ):
            consume_sse_lines(lines)


class AstrBotClientTests(unittest.TestCase):
    def test_client_sends_stable_session_and_parses_sse(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/api/v1/chat")
            self.assertEqual(
                request.headers["Authorization"],
                "Bearer abk_test",
            )
            payload = json.loads(request.content)
            self.assertEqual(payload["username"], "qq-group-1")
            self.assertEqual(payload["session_id"], "session-1")
            self.assertEqual(payload["message"], "hello")
            body = (
                'data: {"type":"plain","data":"world","streaming":false}\n\n'
                'data: {"type":"end","data":""}\n\n'
            )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=body,
            )

        config = BridgeConfig(
            enabled=True,
            base_url="http://astrbot.test",
            api_key="abk_test",
            config_id="default",
            timeout_seconds=10,
            max_concurrent=2,
        )
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = AstrBotClient(config, http_client=http_client)

        result = asyncio.run(
            client.chat(
                username="qq-group-1",
                session_id="session-1",
                message="hello",
            )
        )
        asyncio.run(http_client.aclose())

        self.assertEqual(result.text, "world")

    def test_http_error_does_not_leak_api_key(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="upstream unavailable")

        config = BridgeConfig(
            enabled=True,
            base_url="http://astrbot.test",
            api_key="abk_secret",
            config_id="default",
            timeout_seconds=10,
            max_concurrent=2,
        )
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = AstrBotClient(config, http_client=http_client)

        with self.assertRaises(AstrBotApiError) as raised:
            asyncio.run(
                client.chat(
                    username="qq-user-1",
                    session_id="session-1",
                    message="hello",
                )
            )
        asyncio.run(http_client.aclose())

        self.assertIn("503", str(raised.exception))
        self.assertNotIn("abk_secret", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
