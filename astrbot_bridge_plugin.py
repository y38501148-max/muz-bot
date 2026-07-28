# ruff: noqa: UP045 -- muz-bot still declares Python 3.8 support.

from __future__ import annotations

import asyncio
import random
from pathlib import Path
from typing import Optional

from nonebot import logger, on_command, on_message
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
    ConversationInFlightGate,
    ConversationRateLimiter,
    JsonSessionStore,
    build_conversation_identity,
    is_status_command,
    load_bridge_config,
    should_passively_reply,
)
from astrbot_bridge_messages import as_plain_text_message

BASE_DIR = Path(__file__).parent / "data" / "astrbot_bridge"
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "sessions.json"

CONFIG_ERROR = ""
try:
    CONFIG = load_bridge_config(CONFIG_PATH)
except (TypeError, ValueError) as error:
    CONFIG = BridgeConfig()
    CONFIG_ERROR = str(error)
    logger.error("AstrBot Bridge 配置无效：{}", error)

SESSION_STORE = JsonSessionStore(STATE_PATH)
CLIENT = AstrBotClient(CONFIG)
IN_FLIGHT_GATE = ConversationInFlightGate(CONFIG.max_concurrent)
SESSION_STORE_LOCK = asyncio.Lock()
RATE_LIMITER = ConversationRateLimiter(CONFIG.min_interval_seconds)

ai_command = on_command("ai", priority=5, block=True)
ai_mention = on_message(rule=to_me(), priority=50, block=False)
ai_passive = on_message(priority=99, block=False)


def _identity_for_event(event: MessageEvent):
    group_id: Optional[str] = None
    if isinstance(event, GroupMessageEvent):
        group_id = str(event.group_id)
    return build_conversation_identity(
        user_id=event.get_user_id(),
        group_id=group_id,
    )


def _format_user_message(event: MessageEvent, question: str) -> str:
    if not isinstance(event, GroupMessageEvent):
        return question
    display_name = str(
        getattr(event.sender, "card", "")
        or getattr(event.sender, "nickname", "")
        or "匿名群友"
    )
    display_name = " ".join(display_name.split())[:80]
    return f"[QQ群用户 {display_name}]\n{question}"


async def _ask_astrbot(event: MessageEvent, question: str) -> str:
    if CONFIG_ERROR:
        raise AstrBotApiError(f"配置错误：{CONFIG_ERROR}")
    if not CONFIG.enabled:
        raise AstrBotApiError(f"AstrBot 接入尚未启用，请先配置 {CONFIG_PATH}")
    identity = _identity_for_event(event)
    if not IN_FLIGHT_GATE.try_enter(identity.key):
        raise AstrBotApiError("当前对话正在处理其他消息，请稍后再试")
    try:
        retry_after = RATE_LIMITER.check(identity.key)
        if retry_after > 0:
            raise AstrBotApiError(f"请求过于频繁，请在 {retry_after:.1f} 秒后重试")
        async with SESSION_STORE_LOCK:
            session_id = SESSION_STORE.get_or_create(identity.key)
        result = await CLIENT.chat(
            username=identity.username,
            session_id=session_id,
            message=_format_user_message(event, question),
        )
    finally:
        IN_FLIGHT_GATE.leave(identity.key)
    return result.text


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
        "唯一 /ai 指令：/ai 状态\n"
        "有效上下文上限：50,000 tokens，本地 compact"
    )


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


@ai_mention.handle()
async def handle_ai_mention(event: MessageEvent) -> None:
    if not CONFIG.enabled or CONFIG_ERROR:
        return
    question = event.get_plaintext().strip()
    if not question:
        reply = getattr(event, "reply", None)
        reply_sender = getattr(reply, "sender", None)
        if str(getattr(reply_sender, "user_id", "")) == str(event.self_id):
            question = "请接着上一条回复继续说。"
        else:
            return
    try:
        answer = await _ask_astrbot(event, question)
    except (AstrBotApiError, ValueError) as error:
        logger.warning(
            "AstrBot 定向消息请求失败，错误类型 {}",
            error.__class__.__name__,
        )
        if any(marker in str(error) for marker in ("请求过于频繁", "正在处理其他消息")):
            await ai_mention.finish(f"❌ {error}")
        await ai_mention.finish("❌ AstrBot 暂时不可用，请稍后再试。")
    await ai_mention.finish(as_plain_text_message(answer))


@ai_passive.handle()
async def handle_ai_passive(event: GroupMessageEvent) -> None:
    if not CONFIG.enabled or CONFIG_ERROR or str(event.user_id) == str(event.self_id):
        return
    question = event.get_plaintext().strip()
    if not should_passively_reply(
        question,
        directed=event.is_tome(),
        probability=CONFIG.passive_trigger_probability,
        sample=random.random(),
    ):
        return
    try:
        answer = await _ask_astrbot(event, question)
    except (AstrBotApiError, ValueError) as error:
        logger.warning(
            "AstrBot 被动消息请求失败，错误类型 {}",
            error.__class__.__name__,
        )
        return
    await ai_passive.finish(as_plain_text_message(answer))
