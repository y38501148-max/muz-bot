from io import BytesIO

from nonebot import logger, on_command
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment
from nonebot.params import CommandArg


latex_cmd = on_command("lt", aliases={"latex", "公式"}, priority=5, block=True)


def normalize_latex_source(text: str) -> str:
    formula = text.strip()
    wrappers = (
        ("```latex", "```"),
        ("```tex", "```"),
        ("```", "```"),
        ("$$", "$$"),
        ("\\[", "\\]"),
        ("\\(", "\\)"),
        ("$", "$"),
    )

    changed = True
    while changed:
        changed = False
        formula = formula.strip()
        for prefix, suffix in wrappers:
            if formula.startswith(prefix) and formula.endswith(suffix) and len(formula) >= len(prefix) + len(suffix):
                formula = formula[len(prefix) : -len(suffix)].strip()
                changed = True
                break

    return formula


def extract_reply_text(event: MessageEvent) -> str:
    reply = getattr(event, "reply", None)
    if not reply:
        return ""

    message = getattr(reply, "message", None)
    if message is not None:
        return Message(message).extract_plain_text()

    raw_message = getattr(reply, "raw_message", "")
    return str(raw_message or "")


def extract_reply_message_id(event: MessageEvent) -> str:
    for segment in event.message:
        if segment.type == "reply":
            return str(segment.data.get("id") or "").strip()
    return ""


def extract_latex_formula(event: MessageEvent, args: Message) -> str:
    arg_text = args.extract_plain_text().strip()
    if arg_text:
        return normalize_latex_source(arg_text)

    return normalize_latex_source(extract_reply_text(event))


async def fetch_reply_latex_formula(bot: Bot, event: MessageEvent) -> str:
    reply_message_id = extract_reply_message_id(event)
    if not reply_message_id:
        return ""

    message_data = await bot.get_msg(message_id=int(reply_message_id))
    message = message_data.get("message")
    if message is not None:
        return normalize_latex_source(Message(message).extract_plain_text())

    return normalize_latex_source(str(message_data.get("raw_message") or ""))


def wrap_for_mathtext(formula: str) -> str:
    return formula if formula.startswith("$") and formula.endswith("$") else f"${formula}$"


def render_latex_png(formula: str) -> bytes:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as e:
        raise RuntimeError("服务器缺少 matplotlib 依赖，请先安装项目依赖后再使用 /lt。") from e

    with matplotlib.rc_context({"font.family": "serif", "mathtext.fontset": "dejavuserif"}):
        fig = plt.figure(figsize=(8, 2), dpi=220)
        fig.patch.set_alpha(0.0)

        try:
            fig.text(
                0.5,
                0.5,
                wrap_for_mathtext(formula),
                fontsize=24,
                color="black",
                fontfamily="serif",
                ha="center",
                va="center",
            )
            buffer = BytesIO()
            fig.savefig(
                buffer,
                format="png",
                transparent=True,
                bbox_inches="tight",
                pad_inches=0.2,
            )
            return buffer.getvalue()
        finally:
            plt.close(fig)


@latex_cmd.handle()
async def handle_latex(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    formula = extract_latex_formula(event, args)
    if not formula:
        try:
            formula = await fetch_reply_latex_formula(bot, event)
        except Exception:
            logger.opt(exception=True).warning("通过 reply 段拉取原消息失败")

    if not formula:
        await latex_cmd.finish("用法：引用一条 LaTeX 公式消息后发送 /lt，也可以直接发送 /lt E=mc^2")

    try:
        image_bytes = render_latex_png(formula)
    except RuntimeError as e:
        await latex_cmd.finish(str(e))
    except Exception:
        logger.opt(exception=True).error("LaTeX 公式渲染失败")
        await latex_cmd.finish("LaTeX 渲染失败，请检查公式语法。")

    await latex_cmd.finish(MessageSegment.image(image_bytes))
