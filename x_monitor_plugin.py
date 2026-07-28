import asyncio
from pathlib import Path

from nonebot import get_bot, get_driver, logger, on_command, require
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
)
from nonebot.params import CommandArg

from x_monitor_core import (
    JsonStateStore,
    PollReport,
    StateLoadError,
    XMonitorService,
    can_manage_group,
    format_status,
    load_monitor_config,
)
from x_monitor_fetcher import PlaywrightXFetcher

require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler  # noqa: E402

BASE_DIR = Path(__file__).parent / "data" / "x_monitor"
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"

CONFIG = load_monitor_config(CONFIG_PATH)
STATE_STORE = JsonStateStore(STATE_PATH)
SUPERUSERS = frozenset(
    str(user_id) for user_id in get_driver().config.superusers
)
POLL_LOCK = asyncio.Lock()


async def send_group_message(group_id: str, message: str) -> None:
    bot: Bot = get_bot()
    await bot.send_group_msg(group_id=int(group_id), message=message)


SERVICE = XMonitorService(
    CONFIG,
    STATE_STORE,
    PlaywrightXFetcher(CONFIG),
    send_group_message,
)


def format_poll_report(report: PollReport) -> str:
    if report.skipped:
        return "当前没有启用监控的群聊，未启动浏览器。"
    message = (
        f"发现 {report.discovered} 条，"
        f"新检测 {report.new_posts} 条，"
        f"匹配 {report.matched} 条，"
        f"发送 {report.sent} 条，"
        f"失败 {report.failed} 条。"
    )
    if report.coverage_warning:
        message += f"\n⚠️ {report.coverage_warning}"
    if report.error:
        message += f"\n❌ {report.error}"
    return message


async def run_poll() -> PollReport:
    async with POLL_LOCK:
        return await SERVICE.poll()


@scheduler.scheduled_job(
    "interval",
    minutes=CONFIG.poll_minutes,
    id="tibo_x_monitor_job",
    max_instances=1,
    coalesce=True,
    misfire_grace_time=60,
)
async def scheduled_tibo_poll() -> None:
    try:
        report = await run_poll()
    except StateLoadError as error:
        logger.error("Tibo X 监控状态损坏，已停止本轮任务：{}", error)
        return
    except Exception:
        logger.exception("Tibo X 监控任务出现未处理异常")
        return
    if report.error:
        logger.warning("Tibo X 监控本轮存在错误：{}", report.error)
    elif report.coverage_warning:
        logger.warning(
            "Tibo X 监控回溯警告：{}",
            report.coverage_warning,
        )
    elif not report.skipped:
        logger.info("Tibo X 监控完成：{}", format_poll_report(report))


tibo_cmd = on_command("tibo", priority=5, block=True)


@tibo_cmd.handle()
async def handle_tibo(
    event: MessageEvent,
    args: Message = CommandArg(),
) -> None:
    action = args.extract_plain_text().strip().casefold()
    aliases = {
        "on": "开启",
        "off": "关闭",
        "status": "状态",
        "check": "检查",
    }
    action = aliases.get(action, action)

    if action in {"开启", "关闭"}:
        if not isinstance(event, GroupMessageEvent):
            await tibo_cmd.finish("该操作只能在目标群聊中执行。")
        sender_role = getattr(event.sender, "role", "member")
        if not can_manage_group(
            event.get_user_id(),
            sender_role,
            SUPERUSERS,
        ):
            await tibo_cmd.finish(
                "只有群主、管理员或机器人超级用户可以修改监控状态。"
            )
        await _handle_subscription_action(event, action)
        return

    if action == "状态":
        group_id = (
            str(event.group_id)
            if isinstance(event, GroupMessageEvent)
            else None
        )
        try:
            state = SERVICE.get_state()
        except StateLoadError as error:
            await tibo_cmd.finish(f"❌ 监控状态文件损坏：{error}")
        await tibo_cmd.finish(format_status(CONFIG, state, group_id))

    if action == "检查":
        if event.get_user_id() not in SUPERUSERS:
            await tibo_cmd.finish("只有机器人超级用户可以手动检查。")
        await tibo_cmd.send("正在检查 @Tibo 最近 24 小时的帖子……")
        try:
            report = await run_poll()
        except StateLoadError as error:
            await tibo_cmd.finish(f"❌ 监控状态文件损坏：{error}")
        await tibo_cmd.finish(format_poll_report(report))

    await tibo_cmd.finish(
        "📡 Tibo X 监控\n"
        "/tibo 开启：在当前群开启并立即回溯 24 小时\n"
        "/tibo 关闭：关闭当前群监控\n"
        "/tibo 状态：查看运行状态\n"
        "/tibo 检查：超级用户立即执行一次检查"
    )


async def _handle_subscription_action(
    event: GroupMessageEvent,
    action: str,
) -> None:
    group_id = str(event.group_id)
    try:
        if action == "关闭":
            async with POLL_LOCK:
                changed = SERVICE.disable_group(group_id)
            suffix = "已关闭。" if changed else "本来就是关闭状态。"
            await tibo_cmd.finish(f"🛑 当前群的 Tibo X 监控{suffix}")

        async with POLL_LOCK:
            changed = SERVICE.enable_group(group_id)
            report = await SERVICE.poll()
        prefix = "已开启" if changed else "已保持开启"
        await tibo_cmd.finish(
            f"✅ 当前群的 Tibo X 监控{prefix}。\n"
            f"{format_poll_report(report)}"
        )
    except StateLoadError as error:
        await tibo_cmd.finish(f"❌ 监控状态文件损坏：{error}")
