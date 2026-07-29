# ruff: noqa: UP006, UP045 -- muz-bot still declares Python 3.8 support.

from __future__ import annotations

import asyncio
import contextlib
import functools
import random
import re
import time
from collections import OrderedDict
from pathlib import Path
from typing import List, Optional, Tuple

from nonebot import get_bot, get_driver, logger, on_command, on_fullmatch, on_message
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
    MessageEvent,
)
from nonebot.params import CommandArg
from nonebot.rule import to_me

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
    is_status_command,
    load_bridge_config,
    should_passively_reply,
)
from astrbot_bridge_media import (
    MessageFile,
    describe_event_media,
    describe_message_media,
    extract_event_files,
    extract_event_media,
    extract_message_files,
    extract_message_media,
    extract_reply_message_id,
)
from astrbot_bridge_messages import as_plain_text_message
from astrbot_bridge_references import (
    ForwardItem,
    ForwardSnapshot,
    extract_forward_items,
    extract_message_text,
    parse_forward_payload,
)
from astrbot_member_memory import MemberMemoryStore
from astrbot_plugin_muz_gateway.request_envelope import encode_bridge_request

BASE_DIR = Path(__file__).parent / "data" / "astrbot_bridge"
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "sessions.json"
MEMORY_PATH = BASE_DIR / "member_memories.sqlite3"

CONFIG_ERROR = ""
try:
    CONFIG = load_bridge_config(CONFIG_PATH)
except (TypeError, ValueError) as error:
    CONFIG = BridgeConfig()
    CONFIG_ERROR = str(error)
    logger.error("AstrBot Bridge 配置无效：{}", error)

SESSION_STORE = JsonSessionStore(STATE_PATH)
MEMORY_STORE = MemberMemoryStore(MEMORY_PATH)
CLIENT = AstrBotClient(CONFIG)
CONVERSATION_QUEUE = ConversationQueue(CONFIG.max_concurrent)
SESSION_STORE_LOCK: Optional[asyncio.Lock] = None
SESSION_STORE_LOOP = None
MEMORY_STORE_LOCK: Optional[asyncio.Lock] = None
MEMORY_STORE_LOOP = None
MEMORY_RECORD_SEMAPHORE: Optional[asyncio.Semaphore] = None
MEMORY_RECORD_LOOP = None
RATE_LIMITER = ConversationRateLimiter(CONFIG.min_interval_seconds)
REQUEST_BUDGET = SlidingWindowBudget(
    limits={
        "member": CONFIG.member_requests_per_hour,
        "group": CONFIG.group_requests_per_hour,
        "global": CONFIG.global_requests_per_hour,
    }
)
LAST_ERROR_LOG: dict = {}
MEMORY_MAINTENANCE_TASK = None
MEMORY_SNAPSHOT_CACHE: OrderedDict = OrderedDict()
MEMORY_SNAPSHOT_TIMES: dict = {}
MEMORY_MEMBER_GENERATIONS: OrderedDict = OrderedDict()
MAX_MEMORY_SNAPSHOT_CACHE = 2_000
MEMORY_SNAPSHOT_TTL_SECONDS = 10 * 60
MAX_PENDING_MEMORY_RECORDS = 100
MAX_MEMORY_MEMBER_GENERATIONS = 5_000
_FILE_ANALYSIS_INTENT = re.compile(
    r"(?:分析|看看|读取|总结|识别|解释|检查).{0,10}"
    r"(?:文件|文档|附件|表格|PDF)"
    r"|(?:这个|这份|该)(?:文件|文档|附件|表格|PDF)"
    r"|(?:文件|文档|附件|表格|PDF).{0,10}(?:什么|内容|讲了什么)",
    re.IGNORECASE,
)
_FILE_ANALYSIS_NEGATION = re.compile(
    r"(?:不要|别|无需|不必|不用|禁止|请勿|不想).{0,20}"
    r"(?:分析|查看|看看|读取|总结|识别|解释|检查|处理).{0,10}"
    r"(?:文件|文档|附件|表格|PDF)",
    re.IGNORECASE,
)
_REFERENCE_ANALYSIS_INTENT = re.compile(
    r"(?:分析|看看|查看|读取|总结|概括|解释|识别|评价).{0,12}"
    r"(?:引用|回复|转发|聊天记录|这段|这条|这个)"
    r"|(?:引用|回复|转发|聊天记录).{0,12}"
    r"(?:内容|说了什么|讲了什么|是什么|总结|看看)"
    r"|(?:这|这个|这条|这段).{0,8}(?:是什么|说了什么|内容)",
    re.IGNORECASE,
)
_REFERENCE_ANALYSIS_NEGATION = re.compile(
    r"(?:不要|别|无需|不必|不用|禁止|请勿|不想).{0,20}"
    r"(?:分析|查看|看看|读取|总结|概括|解释|识别|处理).{0,10}"
    r"(?:引用|回复|转发|聊天记录)",
    re.IGNORECASE,
)
DRIVER = get_driver()

ai_memory_recorder = on_message(priority=1, block=False)
ai_command = on_command("ai", priority=5, block=True)
ai_forget = on_fullmatch(
    ("忘掉我", "清除我的记忆", "删除我的记忆"),
    priority=4,
    block=True,
)
ai_mention = on_message(rule=to_me(), priority=50, block=False)
ai_passive = on_message(priority=99, block=False)
ai_memory_cleanup = on_message(priority=100, block=False)


async def _memory_maintenance_loop() -> None:
    while True:
        try:
            await _memory_store_call(MEMORY_STORE.purge_expired)
        except ValueError as error:
            _log_failure("成员记忆维护", error)
        await asyncio.sleep(24 * 60 * 60)


@DRIVER.on_startup
async def start_memory_maintenance() -> None:
    global MEMORY_MAINTENANCE_TASK
    MEMORY_MAINTENANCE_TASK = asyncio.create_task(_memory_maintenance_loop())


@DRIVER.on_shutdown
async def stop_memory_maintenance() -> None:
    if MEMORY_MAINTENANCE_TASK is None:
        return
    MEMORY_MAINTENANCE_TASK.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await MEMORY_MAINTENANCE_TASK


def _identity_for_event(event: MessageEvent):
    group_id: Optional[str] = None
    if isinstance(event, GroupMessageEvent):
        group_id = str(event.group_id)
    return build_conversation_identity(
        user_id=event.get_user_id(),
        group_id=group_id,
    )


def _display_name_for_event(event: GroupMessageEvent) -> str:
    display_name = str(
        getattr(event.sender, "card", "")
        or getattr(event.sender, "nickname", "")
        or "匿名群友"
    )
    return " ".join(display_name.split())[:80] or "匿名群友"


def _budget_buckets(event: MessageEvent) -> Tuple[Tuple[str, str], ...]:
    user_id = str(event.get_user_id())
    if isinstance(event, GroupMessageEvent):
        group_id = str(event.group_id)
        member = f"group:{group_id}:user:{user_id}"
        group = f"group:{group_id}"
    else:
        member = f"private:{user_id}"
        group = member
    return (
        ("member", member),
        ("group", group),
        ("global", "all"),
    )


def _is_memory_delete_request(question: str) -> bool:
    return " ".join(question.split()) in {
        "忘掉我",
        "清除我的记忆",
        "删除我的记忆",
    }


def _memory_cache_key(event: GroupMessageEvent) -> Tuple[str, str, str]:
    return (
        str(event.group_id),
        str(event.user_id),
        str(event.message_id),
    )


def _memory_member_key(event: GroupMessageEvent) -> Tuple[str, str]:
    return str(event.group_id), str(event.user_id)


def _memory_member_generation(event: GroupMessageEvent) -> int:
    return int(MEMORY_MEMBER_GENERATIONS.get(_memory_member_key(event), 0))


def _bump_memory_member_generation(event: GroupMessageEvent) -> None:
    key = _memory_member_key(event)
    generation = int(MEMORY_MEMBER_GENERATIONS.pop(key, 0)) + 1
    MEMORY_MEMBER_GENERATIONS[key] = generation
    while len(MEMORY_MEMBER_GENERATIONS) > MAX_MEMORY_MEMBER_GENERATIONS:
        MEMORY_MEMBER_GENERATIONS.popitem(last=False)


def _cache_memory_snapshot(
    event: GroupMessageEvent,
    memory_samples: List[str],
) -> None:
    _prune_memory_snapshot_cache()
    key = _memory_cache_key(event)
    MEMORY_SNAPSHOT_CACHE.pop(key, None)
    MEMORY_SNAPSHOT_CACHE[key] = list(memory_samples)
    MEMORY_SNAPSHOT_TIMES[key] = time.monotonic()
    while len(MEMORY_SNAPSHOT_CACHE) > MAX_MEMORY_SNAPSHOT_CACHE:
        evicted_key, _ = MEMORY_SNAPSHOT_CACHE.popitem(last=False)
        MEMORY_SNAPSHOT_TIMES.pop(evicted_key, None)


def _prune_memory_snapshot_cache() -> None:
    cutoff = time.monotonic() - MEMORY_SNAPSHOT_TTL_SECONDS
    expired_keys = [
        key for key, created_at in MEMORY_SNAPSHOT_TIMES.items() if created_at < cutoff
    ]
    for key in expired_keys:
        MEMORY_SNAPSHOT_CACHE.pop(key, None)
        MEMORY_SNAPSHOT_TIMES.pop(key, None)


def _take_memory_snapshot(event: GroupMessageEvent) -> Optional[List[str]]:
    _prune_memory_snapshot_cache()
    key = _memory_cache_key(event)
    snapshot = MEMORY_SNAPSHOT_CACHE.pop(key, None)
    MEMORY_SNAPSHOT_TIMES.pop(key, None)
    return list(snapshot) if snapshot is not None else None


def _discard_memory_snapshot(event: GroupMessageEvent) -> None:
    key = _memory_cache_key(event)
    MEMORY_SNAPSHOT_CACHE.pop(key, None)
    MEMORY_SNAPSHOT_TIMES.pop(key, None)


def _invalidate_member_snapshots(event: GroupMessageEvent) -> None:
    group_id = str(event.group_id)
    user_id = str(event.user_id)
    now = time.monotonic()
    for key in list(MEMORY_SNAPSHOT_CACHE):
        if key[:2] == (group_id, user_id):
            # Keep an empty tombstone so an already-arrived AI event cannot
            # fall back to re-reading or re-recording content after deletion.
            MEMORY_SNAPSHOT_CACHE[key] = []
            MEMORY_SNAPSHOT_TIMES[key] = now


async def _record_group_message(event: GroupMessageEvent) -> None:
    """Record every eligible group message before trigger matchers run."""
    if not CONFIG.enabled or CONFIG_ERROR or str(event.user_id) == str(event.self_id):
        return
    question = event.get_plaintext().strip()
    if not question:
        _cache_memory_snapshot(event, [])
        return
    if question.startswith("/") or _is_memory_delete_request(question):
        return
    generation = _memory_member_generation(event)
    semaphore = _memory_record_semaphore()
    try:
        await asyncio.wait_for(
            semaphore.acquire(),
            timeout=0.05,
        )
    except asyncio.TimeoutError:
        _cache_memory_snapshot(event, [])
        return
    try:
        try:
            result = await _memory_store_call_if_generation(
                event,
                generation,
                MEMORY_STORE.snapshot_and_remember,
                event.group_id,
                event.user_id,
                _display_name_for_event(event),
                question,
            )
            if result is None:
                _cache_memory_snapshot(event, [])
                return
            _, memory_samples = result
        except ValueError as error:
            _log_failure("成员记忆采集", error)
            _cache_memory_snapshot(event, [])
            return
    finally:
        semaphore.release()
    if generation != _memory_member_generation(event):
        return
    # Freeze the prior snapshot at arrival time. If this event later waits in
    # the AI queue, messages arriving after it cannot leak into its prompt.
    _cache_memory_snapshot(event, memory_samples)


def _message_from_api_result(
    result: object,
    event: MessageEvent,
) -> Optional[Message]:
    if not isinstance(result, dict):
        return None
    message_type = str(result.get("message_type") or "")
    if isinstance(event, GroupMessageEvent):
        if (
            message_type != "group"
            or str(result.get("group_id") or "") != str(event.group_id)
        ):
            return None
    else:
        sender = result.get("sender")
        sender_id = sender.get("user_id") if isinstance(sender, dict) else None
        result_user_id = result.get("user_id") or sender_id
        result_peer_id = result.get("target_id") or result.get("peer_id")
        same_incoming_peer = str(result_user_id or "") == str(event.user_id)
        same_outgoing_peer = (
            str(result_user_id or "") == str(event.self_id)
            and str(result_peer_id or "") == str(event.user_id)
        )
        if message_type != "private" or not (
            same_incoming_peer or same_outgoing_peer
        ):
            return None
    value = result.get("message")
    if isinstance(value, Message):
        return value
    if isinstance(value, (str, list)):
        try:
            return Message(value)
        except (TypeError, ValueError):
            return None
    return None


async def _fetch_quoted_message(event: MessageEvent, bot: object) -> Optional[Message]:
    if isinstance(getattr(getattr(event, "reply", None), "message", None), Message):
        return None
    message_id = extract_reply_message_id(event)
    if not message_id:
        return None
    try:
        result = await bot.get_msg(message_id=message_id)
    except Exception as error:  # noqa: BLE001
        _log_failure("引用消息读取", error)
        return None
    return _message_from_api_result(result, event)


def _should_include_files(event: MessageEvent, question: str) -> bool:
    if _FILE_ANALYSIS_NEGATION.search(question):
        return False
    return event.is_tome() or bool(_FILE_ANALYSIS_INTENT.search(question))


def _should_include_references(event: MessageEvent, question: str) -> bool:
    if _REFERENCE_ANALYSIS_NEGATION.search(question):
        return False
    return event.is_tome() or bool(_REFERENCE_ANALYSIS_INTENT.search(question))


def _event_forward_items(event: MessageEvent) -> List[ForwardItem]:
    result = []
    seen = set()
    for message in (
        getattr(event, "original_message", None),
        getattr(event, "message", None),
    ):
        for item in extract_forward_items(message):
            key = item.message_id or repr(item.inline_content)[:256]
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
    return result[:2]


async def _fetch_forward_snapshot(
    bot: object,
    items: List[ForwardItem],
) -> ForwardSnapshot:
    text_parts = []
    segments = []
    for item in items:
        payload = item.inline_content
        if not payload and item.message_id:
            try:
                payload = await bot.call_api(
                    "get_forward_msg",
                    message_id=item.message_id,
                )
            except Exception as error:  # noqa: BLE001
                _log_failure("合并转发读取", error)
                continue
        snapshot = parse_forward_payload(payload)
        if snapshot.text:
            text_parts.append(snapshot.text)
        segments.extend(snapshot.message)
        if sum(len(part) for part in text_parts) >= 8_000:
            break
    return ForwardSnapshot(
        text="\n".join(text_parts)[:8_000],
        message=Message(segments),
    )


def _result_url(result: object) -> str:
    if isinstance(result, str):
        candidate = result.strip()
    elif isinstance(result, dict):
        candidate = str(
            result.get("url")
            or result.get("download_url")
            or result.get("downloadUrl")
            or ""
        ).strip()
    else:
        candidate = ""
    return candidate[:2_048] if candidate.startswith(("http://", "https://")) else ""


async def _resolve_file_references(
    event: MessageEvent,
    bot: Optional[object],
    files: List[MessageFile],
) -> List[dict]:
    resolved = []
    for file in files:
        url = file.url
        if not url and file.file_id:
            if bot is None:
                continue
            try:
                if isinstance(event, GroupMessageEvent):
                    result = await bot.call_api(
                        "get_group_file_url",
                        group_id=event.group_id,
                        file_id=file.file_id,
                        busid=file.busid,
                    )
                else:
                    result = await bot.call_api(
                        "get_private_file_url",
                        user_id=event.user_id,
                        file_id=file.file_id,
                    )
                url = _result_url(result)
            except Exception as error:  # noqa: BLE001
                _log_failure("引用文件地址读取", error)
        if url:
            resolved.append({"name": file.name, "url": url})
    return resolved


async def _build_wrapped_message(
    event: MessageEvent,
    question: str,
    *,
    bot: Optional[object] = None,
) -> str:
    runtime_bot = bot
    quoted_message = None
    include_references = _should_include_references(event, question)
    forward_items = _event_forward_items(event) if include_references else []
    if include_references and extract_reply_message_id(event):
        runtime_bot = runtime_bot or get_bot(str(event.self_id))
        quoted_message = await _fetch_quoted_message(event, runtime_bot)
    local_quoted_message = getattr(getattr(event, "reply", None), "message", None)
    if not isinstance(local_quoted_message, Message):
        local_quoted_message = None
    effective_quote = quoted_message or (
        local_quoted_message if include_references else None
    )
    forward_snapshot = ForwardSnapshot(text="", message=Message())
    if forward_items:
        runtime_bot = runtime_bot or get_bot(str(event.self_id))
        forward_snapshot = await _fetch_forward_snapshot(
            runtime_bot,
            forward_items,
        )
    image_urls, video_urls = extract_event_media(
        event,
        reply_message=effective_quote,
        include_event_reply=False,
    )
    forward_images, forward_videos = extract_message_media(
        forward_snapshot.message,
        max_images=max(0, 4 - len(image_urls)),
        max_videos=max(0, 1 - len(video_urls)),
    )
    image_urls.extend(url for url in forward_images if url not in image_urls)
    video_urls.extend(url for url in forward_videos if url not in video_urls)
    message_files = extract_event_files(
        event,
        reply_message=effective_quote,
        include_event_reply=False,
    )
    if include_references and len(message_files) < 2:
        for file in extract_message_files(
            forward_snapshot.message,
            max_files=2 - len(message_files),
        ):
            if file not in message_files:
                message_files.append(file)
    files = []
    if message_files and _should_include_files(event, question):
        if any(not file.url for file in message_files):
            runtime_bot = runtime_bot or get_bot(str(event.self_id))
        files = await _resolve_file_references(event, runtime_bot, message_files)
    media_description = describe_event_media(
        event,
        reply_message=effective_quote,
        include_event_reply=False,
    )
    forward_description = describe_message_media(forward_snapshot.message)
    if forward_description:
        media_description = (
            f"{media_description}；{forward_description}"
            if media_description
            else forward_description
        )
    enriched_question = question.strip()
    if media_description and media_description not in enriched_question:
        enriched_question = (
            f"{enriched_question}\n{media_description}"
            if enriched_question
            else media_description
        )
    display_name = (
        _display_name_for_event(event)
        if isinstance(event, GroupMessageEvent)
        else ""
    )
    memory_samples = (
        _take_memory_snapshot(event) or []
        if isinstance(event, GroupMessageEvent)
        else []
    )
    reference_parts = []
    quoted_text = extract_message_text(effective_quote)
    if quoted_text:
        reference_parts.append(quoted_text)
    if forward_snapshot.text:
        reference_parts.append(forward_snapshot.text)
    return encode_bridge_request(
        display_name=display_name,
        memory_samples=memory_samples,
        image_urls=image_urls,
        video_urls=video_urls,
        files=files,
        reference_text="\n".join(reference_parts)[:8_000],
        directed=event.is_tome(),
        question=enriched_question,
    )


async def _clear_member_memory(event: GroupMessageEvent) -> str:
    identity = _identity_for_event(event)
    async with CONVERSATION_QUEUE.turn(
        identity.key,
        wait_timeout_seconds=CONFIG.queue_wait_seconds,
    ):
        _bump_memory_member_generation(event)
        _invalidate_member_snapshots(event)
        try:
            cleared = await _memory_store_call(
                MEMORY_STORE.clear,
                event.group_id,
                event.user_id,
            )
        except ValueError as error:
            _log_failure("成员记忆", error)
            return "记忆区暂时打不开，稍后再试一下。"
        _invalidate_member_snapshots(event)
    if cleared:
        return "好，属于你的专属记忆已经清空了。"
    return "你的专属记忆区本来就是空的。"


async def _run_blocking(function, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        functools.partial(function, *args),
    )


def _session_store_lock() -> asyncio.Lock:
    global SESSION_STORE_LOCK, SESSION_STORE_LOOP
    loop = asyncio.get_running_loop()
    if SESSION_STORE_LOOP is not loop:
        SESSION_STORE_LOOP = loop
        SESSION_STORE_LOCK = asyncio.Lock()
    assert SESSION_STORE_LOCK is not None
    return SESSION_STORE_LOCK


def _memory_store_lock() -> asyncio.Lock:
    global MEMORY_STORE_LOCK, MEMORY_STORE_LOOP
    loop = asyncio.get_running_loop()
    if MEMORY_STORE_LOOP is not loop:
        MEMORY_STORE_LOOP = loop
        MEMORY_STORE_LOCK = asyncio.Lock()
    assert MEMORY_STORE_LOCK is not None
    return MEMORY_STORE_LOCK


def _memory_record_semaphore() -> asyncio.Semaphore:
    global MEMORY_RECORD_SEMAPHORE, MEMORY_RECORD_LOOP
    loop = asyncio.get_running_loop()
    if MEMORY_RECORD_LOOP is not loop:
        MEMORY_RECORD_LOOP = loop
        MEMORY_RECORD_SEMAPHORE = asyncio.Semaphore(MAX_PENDING_MEMORY_RECORDS)
    assert MEMORY_RECORD_SEMAPHORE is not None
    return MEMORY_RECORD_SEMAPHORE


async def _memory_store_call(function, *args):
    async with _memory_store_lock():
        return await _run_store_executor(function, *args)


async def _memory_store_call_if_generation(
    event: GroupMessageEvent,
    generation: int,
    function,
    *args,
):
    async with _memory_store_lock():
        if generation != _memory_member_generation(event):
            return None
        return await _run_store_executor(function, *args)


async def _run_store_executor(function, *args):
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(
        None,
        functools.partial(function, *args),
    )
    cancelled = False
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            # Repeated cancellation still cannot unlock SQLite while its
            # executor thread is touching the store.
            cancelled = True
    if cancelled:
        with contextlib.suppress(BaseException):
            future.result()
        raise asyncio.CancelledError
    return future.result()


async def _wait_for_rate_limit(key: str) -> None:
    while True:
        retry_after = RATE_LIMITER.check(key)
        if retry_after <= 0:
            return
        await asyncio.sleep(retry_after)


async def _ask_astrbot(event: MessageEvent, question: str) -> str:
    if CONFIG_ERROR:
        raise AstrBotApiError(f"配置错误：{CONFIG_ERROR}")
    if not CONFIG.enabled:
        raise AstrBotApiError(f"AstrBot 接入尚未启用，请先配置 {CONFIG_PATH}")
    if isinstance(event, GroupMessageEvent) and _is_memory_delete_request(question):
        return await _clear_member_memory(event)

    identity = _identity_for_event(event)
    queue_deadline = time.monotonic() + CONFIG.queue_wait_seconds
    async with CONVERSATION_QUEUE.turn(
        identity.key,
        wait_timeout_seconds=CONFIG.queue_wait_seconds,
    ):
        await _wait_for_rate_limit(identity.key)
        async with _session_store_lock():
            session_id = SESSION_STORE.get_or_create(identity.key)
        message = await _build_wrapped_message(event, question)
        remaining_wait = queue_deadline - time.monotonic()
        if remaining_wait <= 0:
            raise ConversationQueueExpiredError("消息排队超时，已取消本次请求")
        async with CONVERSATION_QUEUE.global_slot(
            wait_timeout_seconds=remaining_wait,
        ):
            # Consume only once the request has survived queueing and is about
            # to leave the bridge. Queue overflow/timeouts cannot burn budget.
            REQUEST_BUDGET.consume(_budget_buckets(event))
            result = await CLIENT.chat(
                username=identity.username,
                session_id=session_id,
                message=message,
            )
    return result.text


def _log_failure(area: str, error: Exception) -> None:
    """Rate-limit expected failure logs to avoid flood-driven disk growth."""
    now = time.monotonic()
    key = f"{area}:{error.__class__.__name__}"
    if now - float(LAST_ERROR_LOG.get(key, 0)) < 60:
        return
    LAST_ERROR_LOG[key] = now
    logger.warning(
        "{}失败，错误类型 {}；同类日志 60 秒内不重复记录",
        area,
        error.__class__.__name__,
    )


def _status_text() -> str:
    if CONFIG_ERROR:
        state = "配置错误，请检查机器人日志"
    elif CONFIG.enabled:
        state = "已启用"
    else:
        state = "未启用"
    return (
        "🤖 AstrBot LLM 接入\n"
        f"状态：{state}\n"
        "群聊上下文：每群共享；私聊上下文：每人独立\n"
        f"群消息被动触发：{CONFIG.passive_trigger_probability:.0%}\n"
        "直接触发：@机器人或引用机器人消息\n"
        "成员人格记忆：每群每成员独立；采集全部群聊文字，保留 30 天\n"
        "隐私：敏感格式不记忆；发送“忘掉我”可立即清除本群个人记忆\n"
        f"消息处理：同一对话 FIFO 排队；跨对话最多并行 {CONFIG.max_concurrent} 条\n"
        f"队列保护：等待最长 {CONFIG.queue_wait_seconds:.0f} 秒，"
        "单对话最多 20 条、全局最多 60 条\n"
        f"调用预算：每成员 {CONFIG.member_requests_per_hour}/小时，"
        f"每群 {CONFIG.group_requests_per_hour}/小时\n"
        "联网与多模态：支持受控网页搜索、链接、图片和视频关键帧分析\n"
        "唯一 /ai 指令：/ai 状态\n"
        "有效上下文上限：50,000 tokens，本地 compact"
    )


def _media_question(
    event: MessageEvent,
    *,
    include_files: bool = False,
    include_references: bool = False,
) -> str:
    description = describe_event_media(event)
    if description:
        return description
    image_urls, video_urls = extract_event_media(event)
    files = extract_event_files(event)
    if image_urls or video_urls:
        return "请分析这张图片或视频。"
    if files and include_files:
        return "请分析这个文件。"
    if include_references and _event_forward_items(event):
        return "请分析这段转发内容。"
    if include_references and extract_reply_message_id(event):
        return "请分析引用内容。"
    return ""


@ai_command.handle()
async def handle_ai_command(
    event: MessageEvent,
    args: Message = CommandArg(),  # noqa: B008
) -> None:
    del event
    action = args.extract_plain_text()
    if is_status_command(action):
        await ai_command.finish(_status_text())

    await ai_command.finish("该指令仅保留状态查询：/ai 状态")


@ai_memory_recorder.handle()
async def handle_ai_memory_recorder(event: MessageEvent) -> None:
    if isinstance(event, GroupMessageEvent):
        await _record_group_message(event)


@ai_forget.handle()
async def handle_ai_forget(event: MessageEvent) -> None:
    if not isinstance(event, GroupMessageEvent):
        await ai_forget.finish("私聊没有群成员记忆区。")
    try:
        result = await _clear_member_memory(event)
    except (ConversationQueueFullError, ConversationQueueExpiredError):
        await ai_forget.finish("当前消息太多，删除请求尚未执行，请稍后再试。")
    await ai_forget.finish(result)


@ai_mention.handle()
async def handle_ai_mention(event: MessageEvent) -> None:
    if not CONFIG.enabled or CONFIG_ERROR:
        return
    question = event.get_plaintext().strip()
    if not question:
        question = _media_question(
            event,
            include_files=True,
            include_references=True,
        )
        if not question:
            reply = getattr(event, "reply", None)
            reply_sender = getattr(reply, "sender", None)
            if str(getattr(reply_sender, "user_id", "")) == str(event.self_id):
                question = "请接着上一条回复继续说。"
            else:
                return
    try:
        answer = await _ask_astrbot(event, question)
    except (
        ConversationQueueFullError,
        ConversationQueueExpiredError,
        RequestBudgetExceededError,
    ):
        await ai_mention.finish("这边消息有点多，本轮先歇一下，稍后再叫我。")
    except (AstrBotApiError, ValueError) as error:
        _log_failure("AstrBot 定向请求", error)
        await ai_mention.finish("AstrBot 暂时不可用，请稍后再试。")
    await ai_mention.finish(as_plain_text_message(answer))


@ai_passive.handle()
async def handle_ai_passive(event: GroupMessageEvent) -> None:
    if not CONFIG.enabled or CONFIG_ERROR or str(event.user_id) == str(event.self_id):
        return
    question = event.get_plaintext().strip() or _media_question(event)
    if not should_passively_reply(
        question,
        directed=event.is_tome(),
        probability=CONFIG.passive_trigger_probability,
        sample=random.random(),
    ):
        return
    try:
        answer = await _ask_astrbot(event, question)
    except (
        ConversationQueueFullError,
        ConversationQueueExpiredError,
        RequestBudgetExceededError,
    ):
        return
    except (AstrBotApiError, ValueError) as error:
        _log_failure("AstrBot 被动请求", error)
        return
    await ai_passive.finish(as_plain_text_message(answer))


@ai_memory_cleanup.handle()
async def handle_ai_memory_cleanup(event: MessageEvent) -> None:
    if isinstance(event, GroupMessageEvent):
        _discard_memory_snapshot(event)
