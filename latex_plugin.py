import asyncio
import re
from io import BytesIO
from pathlib import Path

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
CODE_FENCE_PATTERN = re.compile(r"```(?:latex|tex|math)?\s*\n?(.*?)```", re.IGNORECASE | re.DOTALL)
DISPLAY_MATH_PATTERN = re.compile(r"\$\$(.*?)\$\$", re.DOTALL)
BRACKET_MATH_PATTERN = re.compile(r"\\\[(.*?)\\\]", re.DOTALL)
PAREN_MATH_PATTERN = re.compile(r"\\\((.*?)\\\)", re.DOTALL)
INLINE_MATH_PATTERN = re.compile(r"(?<!\\)\$(?!\$)(.*?)(?<!\\)\$(?!\$)", re.DOTALL)
CJK_FONT_PATHS = (
    "yuruka.otf",
    "data/pjsk/yuruka.otf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
)
CJK_FONT_FAMILIES = (
    "Noto Sans CJK SC",
    "Noto Sans CJK JP",
    "Source Han Sans SC",
    "Source Han Serif SC",
    "WenQuanYi Zen Hei",
    "PingFang SC",
    "Microsoft YaHei",
    "SimHei",
    "Arial Unicode MS",
    "DejaVu Sans",
)


def normalize_markdown_latex(text: str) -> str:
    formula = text.strip()

    code_match = CODE_FENCE_PATTERN.fullmatch(formula)
    if code_match:
        return code_match.group(1).strip()

    formula = CODE_FENCE_PATTERN.sub(lambda match: f"\n{match.group(1).strip()}\n", formula)
    formula = DISPLAY_MATH_PATTERN.sub(lambda match: f"\n${match.group(1).strip()}$\n", formula)
    formula = BRACKET_MATH_PATTERN.sub(lambda match: f"\n${match.group(1).strip()}$\n", formula)
    formula = PAREN_MATH_PATTERN.sub(lambda match: f"${match.group(1).strip()}$", formula)
    formula = re.sub(r"\n{2,}", "\n", formula)
    return formula.strip()


def normalize_latex_source(text: str) -> str:
    formula = normalize_markdown_latex(text)

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


def contains_cjk(text: str) -> bool:
    return any(
        "\u3400" <= char <= "\u9fff"
        or "\uf900" <= char <= "\ufaff"
        or "\u3040" <= char <= "\u30ff"
        for char in text
    )


def format_render_line(line: str) -> str:
    if INLINE_MATH_PATTERN.search(line) or contains_cjk(line):
        return line
    return wrap_for_mathtext(line)


def resolve_text_font_families():
    from matplotlib import font_manager

    font_families = []
    for font_path in CJK_FONT_PATHS:
        path = Path(font_path)
        if not path.exists():
            continue
        try:
            font_manager.fontManager.addfont(str(path))
            font_name = font_manager.FontProperties(fname=str(path)).get_name()
        except Exception:
            continue
        font_families.append(font_name)

    available_font_names = {font.name for font in font_manager.fontManager.ttflist}
    font_families.extend(
        font_family
        for font_family in CJK_FONT_FAMILIES
        if font_family in available_font_names
    )
    if "DejaVu Sans" not in font_families:
        font_families.append("DejaVu Sans")
    return list(dict.fromkeys(font_families))


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
        if char in "=+-,;，。；、：":
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


def render_latex_png_with_matplotlib(lines: list[str]) -> bytes:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as e:
        raise RuntimeError("服务器缺少 matplotlib 依赖，请先安装项目依赖后再使用 /lt。") from e

    fig_width, fig_height = calculate_figure_size(lines)
    font_families = resolve_text_font_families()

    with matplotlib.rc_context(
        {
            "font.family": font_families,
            "mathtext.fontset": "cm",
            "mathtext.default": "it",
        }
    ):
        fig = plt.figure(figsize=(fig_width, fig_height), dpi=220)
        fig.patch.set_alpha(0.0)

        try:
            for index, line in enumerate(lines):
                y_position = 1 - ((index + 1) / (len(lines) + 1))
                fig.text(
                    0.5,
                    y_position,
                    format_render_line(line),
                    fontsize=24,
                    color="black",
                    fontfamily=font_families,
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


async def render_latex_png(formula: str) -> bytes:
    lines = split_latex_formula(formula)
    try:
        from katex_renderer import (
            KatexEngineUnavailable,
            KatexRenderError,
            render_katex_png,
        )
    except ImportError:
        logger.warning("KaTeX 渲染器导入失败，回退到 Matplotlib。")
        return await asyncio.to_thread(render_latex_png_with_matplotlib, lines)

    try:
        return await render_katex_png(lines)
    except KatexEngineUnavailable as e:
        logger.warning(f"KaTeX 渲染器不可用，回退到 Matplotlib：{e}")
        return await asyncio.to_thread(render_latex_png_with_matplotlib, lines)
    except KatexRenderError:
        raise


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
        image_bytes = await render_latex_png(formula)
    except RuntimeError as e:
        await latex_cmd.finish(str(e))
    except Exception as e:
        logger.opt(exception=True).error("LaTeX 公式渲染失败")
        await latex_cmd.finish(format_latex_error(e))

    await latex_cmd.finish(MessageSegment.image(image_bytes))
