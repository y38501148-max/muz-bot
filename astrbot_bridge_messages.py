import re

from nonebot.adapters.onebot.v11 import Message, MessageSegment


def _strip_unsupported_markdown_emphasis(text: str) -> str:
    result = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"\1", text, flags=re.DOTALL)
    return re.sub(r"__(?=\S)(.+?)(?<=\S)__", r"\1", result, flags=re.DOTALL)


def as_plain_text_message(text: str) -> Message:
    """Keep model-produced CQ syntax inert."""
    return Message(MessageSegment.text(_strip_unsupported_markdown_emphasis(text)))
