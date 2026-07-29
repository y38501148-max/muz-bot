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
    ConversationQueue,
    ConversationQueueExpiredError,
    ConversationQueueFullError,
    ConversationRateLimiter,
    JsonSessionStore,
    RequestBudgetExceededError,
    SlidingWindowBudget,
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
        self.assertEqual(config.passive_trigger_probability, 0.25)

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


class ConversationQueueTests(unittest.TestCase):
    def test_same_conversation_is_processed_in_fifo_order(self):
        async def scenario():
            queue = ConversationQueue(max_concurrent=2)
            first_started = asyncio.Event()
            release_first = asyncio.Event()
            events = []

            async def worker(name, wait=False):
                async with queue.turn("group:1"):
                    events.append(f"{name}:start")
                    if wait:
                        first_started.set()
                        await release_first.wait()
                    events.append(f"{name}:end")

            first = asyncio.create_task(worker("first", wait=True))
            await first_started.wait()
            second = asyncio.create_task(worker("second"))
            third = asyncio.create_task(worker("third"))
            await asyncio.sleep(0)
            self.assertEqual(events, ["first:start"])
            release_first.set()
            await asyncio.gather(first, second, third)
            return events

        self.assertEqual(
            asyncio.run(scenario()),
            [
                "first:start",
                "first:end",
                "second:start",
                "second:end",
                "third:start",
                "third:end",
            ],
        )

    def test_different_conversations_can_run_up_to_global_limit(self):
        async def scenario():
            queue = ConversationQueue(max_concurrent=2)
            both_started = asyncio.Event()
            release = asyncio.Event()
            active = 0
            peak = 0

            async def worker(key):
                nonlocal active, peak
                async with queue.turn(key), queue.global_slot():
                    active += 1
                    peak = max(peak, active)
                    if active == 2:
                        both_started.set()
                    await release.wait()
                    active -= 1

            first = asyncio.create_task(worker("group:1"))
            second = asyncio.create_task(worker("group:2"))
            await asyncio.wait_for(both_started.wait(), timeout=1)
            release.set()
            await asyncio.gather(first, second)
            return peak

        self.assertEqual(asyncio.run(scenario()), 2)

    def test_extreme_backlog_is_bounded(self):
        async def scenario():
            queue = ConversationQueue(
                max_concurrent=1,
                max_per_conversation=2,
                max_pending_total=2,
            )
            first_started = asyncio.Event()
            release_first = asyncio.Event()

            async def first_worker():
                async with queue.turn("group:1"):
                    first_started.set()
                    await release_first.wait()

            async def second_worker():
                async with queue.turn("group:1"):
                    return

            first = asyncio.create_task(first_worker())
            await first_started.wait()
            second = asyncio.create_task(second_worker())
            await asyncio.sleep(0)
            with self.assertRaises(ConversationQueueFullError):
                async with queue.turn("group:1"):
                    pass
            release_first.set()
            await asyncio.gather(first, second)

        asyncio.run(scenario())

    def test_queue_wait_expires_and_constructor_is_loop_safe(self):
        queue = ConversationQueue(max_concurrent=1)

        async def scenario():
            first_started = asyncio.Event()
            release = asyncio.Event()

            async def first_worker():
                async with queue.turn("group:1"):
                    first_started.set()
                    await release.wait()

            first = asyncio.create_task(first_worker())
            await first_started.wait()
            with self.assertRaises(ConversationQueueExpiredError):
                async with queue.turn(
                    "group:1",
                    wait_timeout_seconds=0.01,
                ):
                    pass
            release.set()
            await first

        asyncio.run(scenario())

    def test_global_slot_wait_also_expires(self):
        async def scenario():
            queue = ConversationQueue(max_concurrent=1)
            first_started = asyncio.Event()
            release = asyncio.Event()

            async def first_worker():
                async with queue.global_slot():
                    first_started.set()
                    await release.wait()

            first = asyncio.create_task(first_worker())
            await first_started.wait()
            with self.assertRaises(ConversationQueueExpiredError):
                async with queue.global_slot(wait_timeout_seconds=0.01):
                    pass
            release.set()
            await first

        asyncio.run(scenario())


class SlidingWindowBudgetTests(unittest.TestCase):
    def test_member_group_and_global_budgets_are_rolling(self):
        budget = SlidingWindowBudget(
            limits={"member": 2, "group": 3, "global": 4},
            window_seconds=10,
        )
        first = (
            ("member", "group-1:user-1"),
            ("group", "group-1"),
            ("global", "all"),
        )
        budget.consume(first, now=1)
        budget.consume(first, now=2)

        with self.assertRaises(RequestBudgetExceededError):
            budget.consume(first, now=3)

        budget.consume(first, now=12.1)

    def test_expired_identity_buckets_are_removed(self):
        budget = SlidingWindowBudget(
            limits={"member": 2, "global": 10},
            window_seconds=10,
        )
        budget.consume(
            (("member", "old"), ("global", "all")),
            now=1,
        )
        budget.consume(
            (("member", "new"), ("global", "all")),
            now=20,
        )

        self.assertNotIn("member:old", budget._events)


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

    def test_session_file_is_private_and_capacity_is_bounded(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sessions.json"
            store = JsonSessionStore(path, max_sessions=2)
            store.get_or_create("group:1")
            store.get_or_create("group:2")
            store.get_or_create("group:3")

            payload = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(len(payload["sessions"]), 2)
            self.assertNotIn("group:2", path.read_text(encoding="utf-8"))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(store.key_path.stat().st_mode & 0o777, 0o600)


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
            self.assertTrue(payload["_skip_user_history"])
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
