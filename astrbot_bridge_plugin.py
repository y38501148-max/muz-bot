# ruff: noqa: UP006, UP045 -- muz-bot still declares Python 3.8 support.

from __future__ import annotations

import asyncio
from collections import defaultdict
from pathlib import Path
from typing import DefaultDict, Optional

from nonebot import get_driver, logger, on_command, on_message
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
    ConversationRateLimiter,
    JsonSessionStore,
    build_conversation_identity,
    load_bridge_config,
)

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
SUPERUSERS = frozenset(
    str(user_id) for user_id in get_driver().config.superusers
)
GLOBAL_SEMAPHORE = asyncio.Semaphore(CONFIG.max_concurrent)
SESSION_LOCKS: DefaultDict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
RATE_LIMITER = ConversationRateLimiter(CONFIG.min_interval_seconds)

ai_command = on_command("ai", priority=5, block=True)
ai_mention = on_message(rule=to_me(), priority=50, block=False)


def _identity_for_event(event: MessageEvent):
    group_id: Optional[str] = None
    if isinstance(event, GroupMessageEvent):
        group_id = str(event.group_id)
    return build_conversation_identity(
        user_id=event.get_user_id(),
        group_id=group_id,
    )


def _can_reset_group(event: GroupMessageEvent) -> bool:
    role = str(getattr(event.sender, "role", "member"))
    return (
        role in {"owner", "admin"}
        or event.get_user_id() in SUPERUSERS
    )


def _format_user_message(event: MessageEvent, question: str) -> str:
    if not isinstance(event, GroupMessageEvent):
        return question
    display_name = str(
        getattr(event.sender, "card", "")
        or getattr(event.sender, "nickname", "")
        or event.get_user_id()
    )
    display_name = " ".join(display_name.split())[:80]
    return (
        f"[QQ群用户 {display_name}（{event.get_user_id()}）]\n"
        f"{question}"
    )


async def _ask_astrbot(event: MessageEvent, question: str) -> str:
    if CONFIG_ERROR:
        raise AstrBotApiError(f"配置错误：{CONFIG_ERROR}")
    if not CONFIG.enabled:
        raise AstrBotApiError(
            f"AstrBot 接入尚未启用，请先配置 {CONFIG_PATH}"
        )
    identity = _identity_for_event(event)
    async with SESSION_LOCKS[identity.key]:
        retry_after = RATE_LIMITER.check(identity.key)
        if retry_after > 0:
            raise AstrBotApiError(
                f"请求过于频繁，请在 {retry_after:.1f} 秒后重试"
            )
        session_id = SESSION_STORE.get_or_create(identity.key)
        async with GLOBAL_SEMAPHORE:
            result = await CLIENT.chat(
                username=identity.username,
                session_id=session_id,
                message=_format_user_message(event, question),
            )
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
        f"地址：{CONFIG.base_url}\n"
        f"配置路由：{CONFIG.config_id or 'default'}\n"
        "群聊上下文：每群共享；私聊上下文：每人独立\n"
        "有效上下文上限：50,000 tokens，本地 compact"
    )


@ai_command.handle()
async def handle_ai_command(
    event: MessageEvent,
    args: Message = CommandArg(),  # noqa: B008
) -> None:
    question = args.extract_plain_text().strip()
    action = question.casefold()
    if action in {"状态", "status"}:
        await ai_command.finish(_status_text())

    if action in {"新对话", "重置", "reset", "new"}:
        if isinstance(event, GroupMessageEvent) and not _can_reset_group(event):
            await ai_command.finish(
                "只有群主、管理员或机器人超级用户可以重置群聊上下文。"
            )
        identity = _identity_for_event(event)
        async with SESSION_LOCKS[identity.key]:
            SESSION_STORE.reset(identity.key)
        await ai_command.finish("✅ 已创建新的 AstrBot 对话。")

    if not question:
        await ai_command.finish(
            "用法：\n"
            "/ai <问题>：向 AstrBot 提问\n"
            "/ai 新对话：清空当前会话上下文\n"
            "/ai 状态：查看接入状态\n"
            "群聊中也可以直接 @机器人 提问。"
        )

    try:
        answer = await _ask_astrbot(event, question)
    except (AstrBotApiError, ValueError) as error:
        logger.warning(
            "AstrBot 请求失败，用户 {}，错误类型 {}",
            event.get_user_id(),
            error.__class__.__name__,
        )
        if "请求过于频繁" in str(error):
            await ai_command.finish(f"❌ {error}")
        await ai_command.finish("❌ AstrBot 暂时不可用，请稍后再试。")
    await ai_command.finish(answer)


@ai_mention.handle()
async def handle_ai_mention(event: MessageEvent) -> None:
    if not CONFIG.enabled or CONFIG_ERROR:
        return
    question = event.get_plaintext().strip()
    if not question:
        return
    try:
        answer = await _ask_astrbot(event, question)
    except (AstrBotApiError, ValueError) as error:
        logger.warning(
            "AstrBot @消息请求失败，用户 {}，错误类型 {}",
            event.get_user_id(),
            error.__class__.__name__,
        )
        if "请求过于频繁" in str(error):
            await ai_mention.finish(f"❌ {error}")
        await ai_mention.finish("❌ AstrBot 暂时不可用，请稍后再试。")
    await ai_mention.finish(answer)
