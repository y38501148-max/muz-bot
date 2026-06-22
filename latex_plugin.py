import re
from io import BytesIO

from nonebot import logger, on_command
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment
from nonebot.params import CommandArg


latex_cmd = on_command("lt", aliases={"latex", "公式"}, priority=5, block=True)

MAX_LATEX_LINE_CHARS = 72
MIN_LATEX_BREAK_CHARS = 36
MIN_FIG_WIDTH = 4.0
MAX_FIG_WIDTH = 12.0
FIG_WIDTH_PER_CHAR = 0.13
FIG_HEIGHT_PER_LINE = 0.85
FIG_HEIGHT_PADDING = 1.2
MARKDOWN_LATEX_PATTERNS = (
    re.compile(r"```(?:latex|tex|math)?\s*\n?(.*?)```", re.IGNORECASE | re.DOTALL),
    re.compile(r"\$\$(.*?)\$\$", re.DOTALL),
    re.compile(r"\\\[(.*?)\\\]", re.DOTALL),
    re.compile(r"\\\((.*?)\\\)", re.DOTALL),
    re.compile(r"(?<!\\)\$(?!\$)(.*?)(?<!\\)\$(?!\$)", re.DOTALL),
)


def extract_markdown_latex(text: str) -> str:
    for pattern in MARKDOWN_LATEX_PATTERNS:
        matches = [match.strip() for match in pattern.findall(text) if match.strip()]
        if matches:
            return "\n".join(matches)
    return ""


def normalize_latex_source(text: str) -> str:
    formula = text.strip()
    markdown_formula = extract_markdown_latex(formula)
    if markdown_formula:
        formula = markdown_formula

    wrappers = (
        ("```latex", "```"),
        ("```tex", "```"),
        ("```", "```"),
        ("`", "`"),
        ("$$", "$$"),
        ("\\[", "\\]"),
        ("\\(", "\\)"),
        ("$", "$"),
        ("**", "**"),
        ("*", "*"),
        ("_", "_"),
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


def find_latex_break_position(text: str, max_chars: int) -> int:
    depth = 0
    last_break = -1
    search_limit = min(len(text), max_chars)

    for index, char in enumerate(text[:search_limit]):
        if char == "\\":
            continue
        if char in "{[(":
            depth += 1
            continue
        if char in "}])" and depth > 0:
            depth -= 1
            continue
        if depth != 0 or index < MIN_LATEX_BREAK_CHARS:
            continue
        if char in "=+-,;":
            last_break = index

    return last_break


def split_latex_line(line: str, max_chars: int = MAX_LATEX_LINE_CHARS) -> list[str]:
    parts = []
    rest = line.strip()

    while len(rest) > max_chars:
        break_position = find_latex_break_position(rest, max_chars)
        if break_position <= 0:
            break

        parts.append(rest[:break_position].strip())
        rest = rest[break_position:].strip()

    if rest:
        parts.append(rest)
    return parts


def split_latex_formula(formula: str) -> list[str]:
    lines = []
    for line in formula.splitlines():
        line = line.strip()
        if line:
            lines.extend(split_latex_line(line))
    return lines or [formula]


def calculate_figure_size(lines: list[str]) -> tuple[float, float]:
    longest_line = max(len(line) for line in lines)
    width = min(MAX_FIG_WIDTH, max(MIN_FIG_WIDTH, longest_line * FIG_WIDTH_PER_CHAR))
    height = max(2.0, len(lines) * FIG_HEIGHT_PER_LINE + FIG_HEIGHT_PADDING)
    return width, height


def format_latex_error(error: Exception) -> str:
    detail = str(error).strip() or error.__class__.__name__
    if len(detail) > 1500:
        detail = f"{detail[:1500]}..."
    return f"LaTeX 渲染失败，请检查公式语法。\n错误信息：\n{detail}"


def render_latex_png(formula: str) -> bytes:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as e:
        raise RuntimeError("服务器缺少 matplotlib 依赖，请先安装项目依赖后再使用 /lt。") from e

    lines = split_latex_formula(formula)
    fig_width, fig_height = calculate_figure_size(lines)

    with matplotlib.rc_context({"font.family": "serif", "mathtext.fontset": "cm", "mathtext.default": "it"}):
        fig = plt.figure(figsize=(fig_width, fig_height), dpi=220)
        fig.patch.set_alpha(0.0)

        try:
            for index, line in enumerate(lines):
                y_position = 1 - ((index + 1) / (len(lines) + 1))
                fig.text(
                    0.5,
                    y_position,
                    wrap_for_mathtext(line),
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
                pad_inches=0.25,
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
    except Exception as e:
        logger.opt(exception=True).error("LaTeX 公式渲染失败")
        await latex_cmd.finish(format_latex_error(e))

    await latex_cmd.finish(MessageSegment.image(image_bytes))
