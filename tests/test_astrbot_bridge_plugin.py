import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

import nonebot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment

nonebot.init()

import astrbot_bridge_plugin as bridge_plugin
from astrbot_bridge_core import BridgeConfig
from astrbot_member_memory import MemberMemoryStore
from astrbot_plugin_muz_gateway.request_envelope import (
    split_prompt_and_system_context,
)


def group_event(
    *,
    group_id: int = 10001,
    user_id: int = 20001,
    nickname: str = "小青",
    message: str = "我喜欢解谜游戏",
    message_id: int = 1,
) -> GroupMessageEvent:
    onebot_message = Message(message)
    return GroupMessageEvent(
        time=1,
        self_id=99999,
        post_type="message",
        sub_type="normal",
        user_id=user_id,
        message_type="group",
        message_id=message_id,
        message=onebot_message,
        original_message=onebot_message,
        raw_message=str(onebot_message),
        font=0,
        sender={
            "user_id": user_id,
            "nickname": nickname,
            "card": "",
        },
        group_id=group_id,
    )


class AstrBotBridgePluginMemoryTests(unittest.TestCase):
    def test_passive_handler_skips_messages_mentioning_only_other_members(self):
        async def unexpected_request(event, question):
            raise AssertionError("不应触发模型请求")

        async def scenario():
            event = group_event(
                message=Message(
                    [
                        MessageSegment.at(30003),
                        MessageSegment.text("你看看这个"),
                    ]
                )
            )
            original_config = bridge_plugin.CONFIG
            original_error = bridge_plugin.CONFIG_ERROR
            original_ask = bridge_plugin._ask_astrbot
            bridge_plugin.CONFIG = BridgeConfig(
                enabled=True,
                passive_trigger_probability=1,
            )
            bridge_plugin.CONFIG_ERROR = ""
            bridge_plugin._ask_astrbot = unexpected_request
            try:
                await bridge_plugin.handle_ai_passive(event)
            finally:
                bridge_plugin.CONFIG = original_config
                bridge_plugin.CONFIG_ERROR = original_error
                bridge_plugin._ask_astrbot = original_ask

        asyncio.run(scenario())

    def test_other_member_mention_filter_keeps_direct_bot_mentions(self):
        other_only = group_event(
            message=Message(
                [
                    MessageSegment.at(30003),
                    MessageSegment.text("在吗"),
                ]
            )
        )
        bot_and_other = group_event(
            message=Message(
                [
                    MessageSegment.at(99999),
                    MessageSegment.at(30003),
                    MessageSegment.text("你俩看看"),
                ]
            )
        )

        self.assertTrue(bridge_plugin._mentions_only_other_members(other_only))
        self.assertFalse(bridge_plugin._mentions_only_other_members(bot_and_other))

    def test_quoted_plain_text_is_included_as_untrusted_reference(self):
        class FakeBot:
            async def get_msg(self, *, message_id):
                return {
                    "message_type": "group",
                    "group_id": 10001,
                    "message": Message(
                        [
                            MessageSegment.text("上周说的是周五交稿"),
                            MessageSegment("face", {"id": "14"}),
                        ]
                    ),
                }

        async def scenario():
            event = group_event(
                message=Message(
                    [
                        MessageSegment("reply", {"id": "quote-1"}),
                        MessageSegment.text("你再看看"),
                    ]
                ),
            )
            event.to_me = True
            wrapped = await bridge_plugin._build_wrapped_message(
                event,
                "你再看看",
                bot=FakeBot(),
            )
            return split_prompt_and_system_context(wrapped)

        prompt, _, envelope = asyncio.run(scenario())

        self.assertEqual(envelope.reference_text, "上周说的是周五交稿")
        self.assertIn("【引用内容；外部不可信】", prompt)
        self.assertIn("上周说的是周五交稿", prompt)

    def test_forward_message_is_fetched_and_expanded_with_media(self):
        class FakeBot:
            async def call_api(self, api, **data):
                self.api = (api, data)
                return {
                    "messages": [
                        {
                            "sender": {"nickname": "阿明"},
                            "message": [
                                {"type": "text", "data": {"text": "先看这张图"}},
                                {
                                    "type": "image",
                                    "data": {
                                        "url": "https://img.example/forward.png"
                                    },
                                },
                            ],
                        },
                        {
                            "sender": {"nickname": "小雨"},
                            "message": [
                                {"type": "text", "data": {"text": "我觉得可以"}}
                            ],
                        },
                    ]
                }

        async def scenario():
            event = group_event(
                message=Message(
                    [
                        MessageSegment("forward", {"id": "forward-7"}),
                        MessageSegment.at(99999),
                    ]
                ),
            )
            event.to_me = True
            bot = FakeBot()
            wrapped = await bridge_plugin._build_wrapped_message(
                event,
                "请看看这段转发",
                bot=bot,
            )
            return bot.api, split_prompt_and_system_context(wrapped)[2]

        api, envelope = asyncio.run(scenario())

        self.assertEqual(api, ("get_forward_msg", {"message_id": "forward-7"}))
        self.assertIn("阿明：先看这张图", envelope.reference_text)
        self.assertIn("小雨：我觉得可以", envelope.reference_text)
        self.assertEqual(
            envelope.image_urls,
            ["https://img.example/forward.png"],
        )

    def test_forward_payload_is_bounded_and_not_sent_by_ordinary_passive_chat(self):
        class FakeBot:
            async def call_api(self, api, **data):
                return {
                    "messages": [
                        {
                            "sender": {"nickname": f"成员{index}"},
                            "message": [
                                {"type": "text", "data": {"text": "内容" * 500}}
                            ],
                        }
                        for index in range(80)
                    ]
                }

        async def scenario(question, directed):
            event = group_event(
                message=Message(
                    [
                        MessageSegment("forward", {"id": "forward-large"}),
                        MessageSegment.text(question),
                    ]
                ),
            )
            event.to_me = directed
            wrapped = await bridge_plugin._build_wrapped_message(
                event,
                question,
                bot=FakeBot(),
            )
            return split_prompt_and_system_context(wrapped)[2]

        ordinary = asyncio.run(scenario("大家下午好", False))
        explicit = asyncio.run(scenario("请总结这段转发", False))

        self.assertEqual(ordinary.reference_text, "")
        self.assertLessEqual(len(explicit.reference_text), 8_000)

    def test_ordinary_passive_chat_does_not_upload_local_quoted_media(self):
        async def scenario():
            event = group_event(message="大家下午好")
            event.reply = SimpleNamespace(
                message=Message(
                    [
                        MessageSegment.image(
                            "https://img.example/private-quote.png"
                        )
                    ]
                ),
                sender=SimpleNamespace(user_id=20002),
            )
            wrapped = await bridge_plugin._build_wrapped_message(
                event,
                "大家下午好",
            )
            return split_prompt_and_system_context(wrapped)[2]

        envelope = asyncio.run(scenario())

        self.assertEqual(envelope.image_urls, [])
        self.assertEqual(envelope.reference_text, "")

    def test_quoted_image_is_fetched_from_onebot(self):
        class FakeBot:
            async def get_msg(self, *, message_id):
                self.message_id = message_id
                return {
                    "message_type": "group",
                    "group_id": 10001,
                    "message": Message(
                        [
                            MessageSegment.image(
                                "https://multimedia.nt.qq.com.cn/quoted.jpg"
                            )
                        ]
                    )
                }

        async def scenario():
            event = group_event(
                message=Message(
                    [
                        MessageSegment("reply", {"id": "549327693"}),
                        MessageSegment.text("这是什么"),
                    ]
                ),
            )
            bot = FakeBot()
            wrapped = await bridge_plugin._build_wrapped_message(
                event,
                "这是什么",
                bot=bot,
            )
            return bot.message_id, split_prompt_and_system_context(wrapped)[2]

        message_id, envelope = asyncio.run(scenario())

        self.assertEqual(message_id, "549327693")
        self.assertEqual(
            envelope.image_urls,
            ["https://multimedia.nt.qq.com.cn/quoted.jpg"],
        )

    def test_quoted_group_file_url_is_resolved_from_onebot(self):
        class FakeBot:
            async def get_msg(self, *, message_id):
                return {
                    "message_type": "group",
                    "group_id": 10001,
                    "message": Message(
                        [
                            MessageSegment(
                                "file",
                                {
                                    "file": "课表.xlsx",
                                    "file_id": "file-456",
                                    "busid": 102,
                                },
                            )
                        ]
                    )
                }

            async def call_api(self, api, **data):
                self.file_api = (api, data)
                return {"url": "https://qfile.qq.com/timetable.xlsx"}

        async def scenario():
            event = group_event(
                message=Message(
                    [
                        MessageSegment("reply", {"id": "803547687"}),
                        MessageSegment.text("分析这个文件"),
                    ]
                ),
            )
            bot = FakeBot()
            wrapped = await bridge_plugin._build_wrapped_message(
                event,
                "分析这个文件",
                bot=bot,
            )
            return bot.file_api, split_prompt_and_system_context(wrapped)[2]

        file_api, envelope = asyncio.run(scenario())

        self.assertEqual(file_api[0], "get_group_file_url")
        self.assertEqual(file_api[1]["file_id"], "file-456")
        self.assertEqual(envelope.files[0].name, "课表.xlsx")
        self.assertEqual(
            envelope.files[0].url,
            "https://qfile.qq.com/timetable.xlsx",
        )

    def test_quoted_message_from_another_group_is_rejected(self):
        class FakeBot:
            async def get_msg(self, *, message_id):
                return {
                    "message_type": "group",
                    "group_id": 99999,
                    "message": Message(
                        [
                            MessageSegment.image(
                                "https://multimedia.nt.qq.com.cn/private.jpg"
                            )
                        ]
                    ),
                }

        async def scenario():
            event = group_event(
                message=Message(
                    [
                        MessageSegment("reply", {"id": "123456"}),
                        MessageSegment.text("看看这个"),
                    ]
                ),
            )
            wrapped = await bridge_plugin._build_wrapped_message(
                event,
                "看看这个",
                bot=FakeBot(),
            )
            return split_prompt_and_system_context(wrapped)[2]

        envelope = asyncio.run(scenario())

        self.assertEqual(envelope.image_urls, [])

    def test_private_quote_requires_matching_peer_metadata(self):
        event = SimpleNamespace(user_id=20001, self_id=99999)
        message = Message([MessageSegment.text("仅限当前私聊")])

        missing_peer = bridge_plugin._message_from_api_result(
            {
                "message_type": "private",
                "user_id": 99999,
                "message": message,
            },
            event,
        )
        matching_peer = bridge_plugin._message_from_api_result(
            {
                "message_type": "private",
                "user_id": 99999,
                "target_id": 20001,
                "message": message,
            },
            event,
        )

        self.assertIsNone(missing_peer)
        self.assertEqual(matching_peer, message)

    def test_passive_file_is_only_forwarded_for_explicit_analysis(self):
        async def scenario(question):
            event = group_event(
                message=Message(
                    [
                        MessageSegment(
                            "file",
                            {
                                "file": "成员名单.txt",
                                "url": "https://qfile.qq.com/members.txt",
                            },
                        ),
                        MessageSegment.text(question),
                    ]
                ),
            )
            wrapped = await bridge_plugin._build_wrapped_message(event, question)
            return split_prompt_and_system_context(wrapped)[2]

        ordinary = asyncio.run(scenario("大家下午好"))
        explicit = asyncio.run(scenario("请分析这个文件"))
        negated = asyncio.run(scenario("不要分析这个文件"))

        self.assertEqual(ordinary.files, [])
        self.assertEqual(explicit.files[0].name, "成员名单.txt")
        self.assertEqual(negated.files, [])

    def test_disabled_bridge_does_not_collect_group_messages(self):
        async def scenario():
            await bridge_plugin._record_group_message(group_event())

        with tempfile.TemporaryDirectory() as directory:
            original_store = bridge_plugin.MEMORY_STORE
            original_config = bridge_plugin.CONFIG
            path = Path(directory) / "member_memories.sqlite3"
            bridge_plugin.CONFIG = BridgeConfig(enabled=False)
            bridge_plugin.MEMORY_STORE = MemberMemoryStore(path)
            try:
                asyncio.run(scenario())
            finally:
                bridge_plugin.MEMORY_STORE = original_store
                bridge_plugin.CONFIG = original_config

        self.assertFalse(path.exists())

    def test_cancelled_store_call_holds_lock_until_executor_stops(self):
        async def scenario():
            first_started = threading.Event()
            first_release = threading.Event()
            second_started = threading.Event()

            def first():
                first_started.set()
                first_release.wait(timeout=2)

            def second():
                second_started.set()

            first_task = asyncio.create_task(bridge_plugin._memory_store_call(first))
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, first_started.wait, 1)
            first_task.cancel()
            second_task = asyncio.create_task(bridge_plugin._memory_store_call(second))
            await asyncio.sleep(0.02)
            blocked = not second_started.is_set()
            first_task.cancel()
            await asyncio.sleep(0)
            blocked = blocked and not second_started.is_set()
            first_release.set()
            with self.assertRaises(asyncio.CancelledError):
                await first_task
            await second_task
            return blocked

        self.assertTrue(asyncio.run(scenario()))

    def test_stale_recorder_checks_generation_inside_store_lock(self):
        class CountingStore:
            def __init__(self):
                self.calls = 0

            def snapshot_and_remember(self, *_args):
                self.calls += 1
                return "小青", []

        async def scenario(store):
            event = group_event(message="删除前到达", message_id=30)
            lock = bridge_plugin._memory_store_lock()
            async with lock:
                task = asyncio.create_task(bridge_plugin._record_group_message(event))
                await asyncio.sleep(0)
                bridge_plugin._bump_memory_member_generation(event)
            await task
            return store.calls

        original_store = bridge_plugin.MEMORY_STORE
        original_config = bridge_plugin.CONFIG
        store = CountingStore()
        bridge_plugin.MEMORY_MEMBER_GENERATIONS.clear()
        bridge_plugin.CONFIG = BridgeConfig(enabled=True, api_key="test")
        bridge_plugin.MEMORY_STORE = store
        try:
            calls = asyncio.run(scenario(store))
        finally:
            bridge_plugin.MEMORY_STORE = original_store
            bridge_plugin.CONFIG = original_config
            bridge_plugin.MEMORY_MEMBER_GENERATIONS.clear()

        self.assertEqual(calls, 0)

    def test_all_group_messages_are_learned_with_arrival_time_snapshot(self):
        async def scenario():
            first = group_event(message="我喜欢解谜游戏", message_id=1)
            second = group_event(message="现在推荐一个游戏", message_id=2)
            later = group_event(
                message="后来我又喜欢竞速游戏",
                message_id=3,
            )
            await bridge_plugin._record_group_message(first)
            bridge_plugin._discard_memory_snapshot(first)
            await bridge_plugin._record_group_message(second)
            await bridge_plugin._record_group_message(later)
            second_wrapped = await bridge_plugin._build_wrapped_message(
                second,
                second.get_plaintext(),
            )
            return second_wrapped

        with tempfile.TemporaryDirectory() as directory:
            original_store = bridge_plugin.MEMORY_STORE
            original_config = bridge_plugin.CONFIG
            bridge_plugin.MEMORY_SNAPSHOT_CACHE.clear()
            bridge_plugin.MEMORY_SNAPSHOT_TIMES.clear()
            bridge_plugin.CONFIG = BridgeConfig(enabled=True, api_key="test")
            bridge_plugin.MEMORY_STORE = MemberMemoryStore(
                Path(directory) / "member_memories.sqlite3"
            )
            try:
                second_wrapped = asyncio.run(scenario())
            finally:
                bridge_plugin.MEMORY_STORE = original_store
                bridge_plugin.CONFIG = original_config
                bridge_plugin.MEMORY_SNAPSHOT_CACHE.clear()
                bridge_plugin.MEMORY_SNAPSHOT_TIMES.clear()

        second_prompt, second_context, _ = split_prompt_and_system_context(
            second_wrapped
        )
        self.assertNotIn("解谜游戏", second_prompt)
        self.assertIn("解谜游戏", second_context)
        self.assertNotIn("后来我又喜欢竞速游戏", second_context)
        self.assertIn("现在推荐一个游戏", second_prompt)
        self.assertIn("小青", second_prompt)
        self.assertNotIn("10001", second_wrapped)
        self.assertNotIn("20001", second_wrapped)

    def test_empty_media_event_uses_empty_arrival_tombstone(self):
        async def scenario():
            media_event = group_event(message="", message_id=40)
            later = group_event(message="到达后记忆", message_id=41)
            await bridge_plugin._record_group_message(media_event)
            await bridge_plugin._record_group_message(later)
            return await bridge_plugin._build_wrapped_message(
                media_event,
                "请分析这张图片。",
            )

        with tempfile.TemporaryDirectory() as directory:
            original_store = bridge_plugin.MEMORY_STORE
            original_config = bridge_plugin.CONFIG
            bridge_plugin.MEMORY_SNAPSHOT_CACHE.clear()
            bridge_plugin.MEMORY_SNAPSHOT_TIMES.clear()
            bridge_plugin.CONFIG = BridgeConfig(enabled=True, api_key="test")
            bridge_plugin.MEMORY_STORE = MemberMemoryStore(
                Path(directory) / "member_memories.sqlite3"
            )
            try:
                wrapped = asyncio.run(scenario())
            finally:
                bridge_plugin.MEMORY_STORE = original_store
                bridge_plugin.CONFIG = original_config
                bridge_plugin.MEMORY_SNAPSHOT_CACHE.clear()
                bridge_plugin.MEMORY_SNAPSHOT_TIMES.clear()

        _, context, _ = split_prompt_and_system_context(wrapped)
        self.assertNotIn("到达后记忆", context)

    def test_forget_request_clears_only_current_member(self):
        async def scenario():
            event = group_event()
            await bridge_plugin._record_group_message(event)
            response = await bridge_plugin._clear_member_memory(event)
            _, snapshot = await bridge_plugin._run_blocking(
                bridge_plugin.MEMORY_STORE.snapshot_and_remember,
                event.group_id,
                event.user_id,
                "小青",
                "新消息",
            )
            return response, snapshot

        with tempfile.TemporaryDirectory() as directory:
            original_store = bridge_plugin.MEMORY_STORE
            original_config = bridge_plugin.CONFIG
            bridge_plugin.MEMORY_SNAPSHOT_CACHE.clear()
            bridge_plugin.MEMORY_SNAPSHOT_TIMES.clear()
            bridge_plugin.CONFIG = BridgeConfig(enabled=True, api_key="test")
            bridge_plugin.MEMORY_STORE = MemberMemoryStore(
                Path(directory) / "member_memories.sqlite3"
            )
            try:
                response, snapshot = asyncio.run(scenario())
            finally:
                bridge_plugin.MEMORY_STORE = original_store
                bridge_plugin.CONFIG = original_config
                bridge_plugin.MEMORY_SNAPSHOT_CACHE.clear()
                bridge_plugin.MEMORY_SNAPSHOT_TIMES.clear()

        self.assertIn("清空", response)
        self.assertEqual(snapshot, [])

    def test_forget_invalidates_pending_ephemeral_snapshots(self):
        async def scenario():
            secret = group_event(
                message="我喜欢安静的地方",
                message_id=10,
            )
            pending = group_event(
                message="给我一个建议",
                message_id=11,
            )
            forget = group_event(message="忘掉我", message_id=12)
            await bridge_plugin._record_group_message(secret)
            bridge_plugin._discard_memory_snapshot(secret)
            await bridge_plugin._record_group_message(pending)
            await bridge_plugin._clear_member_memory(forget)
            return await bridge_plugin._build_wrapped_message(
                pending,
                pending.get_plaintext(),
            )

        with tempfile.TemporaryDirectory() as directory:
            original_store = bridge_plugin.MEMORY_STORE
            original_config = bridge_plugin.CONFIG
            bridge_plugin.MEMORY_SNAPSHOT_CACHE.clear()
            bridge_plugin.MEMORY_SNAPSHOT_TIMES.clear()
            bridge_plugin.CONFIG = BridgeConfig(enabled=True, api_key="test")
            bridge_plugin.MEMORY_STORE = MemberMemoryStore(
                Path(directory) / "member_memories.sqlite3"
            )
            try:
                wrapped = asyncio.run(scenario())
            finally:
                bridge_plugin.MEMORY_STORE = original_store
                bridge_plugin.CONFIG = original_config
                bridge_plugin.MEMORY_SNAPSHOT_CACHE.clear()
                bridge_plugin.MEMORY_SNAPSHOT_TIMES.clear()

        _, context, _ = split_prompt_and_system_context(wrapped)
        self.assertNotIn("安静的地方", context)

    def test_forget_generation_rejects_inflight_recorder_snapshot(self):
        class BlockingStore:
            def __init__(self):
                self.started = threading.Event()
                self.release = threading.Event()

            def snapshot_and_remember(self, *_args):
                self.started.set()
                self.release.wait(timeout=2)
                return "小青", ["OLD_SECRET"]

            def clear(self, *_args):
                return True

        async def scenario(store):
            event = group_event(message="普通消息", message_id=20)
            forget = group_event(message="忘掉我", message_id=21)
            record_task = asyncio.create_task(
                bridge_plugin._record_group_message(event)
            )
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, store.started.wait, 1)
            clear_task = asyncio.create_task(bridge_plugin._clear_member_memory(forget))
            for _ in range(100):
                if bridge_plugin._memory_member_generation(forget) > 0:
                    break
                await asyncio.sleep(0)
            store.release.set()
            await asyncio.gather(record_task, clear_task)
            return list(bridge_plugin.MEMORY_SNAPSHOT_CACHE.values())

        original_store = bridge_plugin.MEMORY_STORE
        original_config = bridge_plugin.CONFIG
        store = BlockingStore()
        bridge_plugin.MEMORY_SNAPSHOT_CACHE.clear()
        bridge_plugin.MEMORY_SNAPSHOT_TIMES.clear()
        bridge_plugin.MEMORY_MEMBER_GENERATIONS.clear()
        bridge_plugin.CONFIG = BridgeConfig(enabled=True, api_key="test")
        bridge_plugin.MEMORY_STORE = store
        try:
            snapshots = asyncio.run(scenario(store))
        finally:
            bridge_plugin.MEMORY_STORE = original_store
            bridge_plugin.CONFIG = original_config
            bridge_plugin.MEMORY_SNAPSHOT_CACHE.clear()
            bridge_plugin.MEMORY_SNAPSHOT_TIMES.clear()
            bridge_plugin.MEMORY_MEMBER_GENERATIONS.clear()

        self.assertNotIn(["OLD_SECRET"], snapshots)


if __name__ == "__main__":
    unittest.main()
