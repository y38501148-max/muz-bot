import re

from nonebot.adapters.onebot.v11 import Message, MessageSegment

MAX_TRANSPORT_CHARS = 4_000
_TRUNCATION_SUFFIX = "…（回复过长，已截断）"


def _strip_unsupported_markdown_emphasis(text: str) -> str:
    result = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"\1", text, flags=re.DOTALL)
    return re.sub(r"__(?=\S)(.+?)(?<=\S)__", r"\1", result, flags=re.DOTALL)


def _prepare_reply(text: str) -> str:
    stripped = _strip_unsupported_markdown_emphasis(text)
    lines = [" ".join(line.split()) for line in stripped.splitlines()]
    normalized = "\n".join(line for line in lines if line)
    if len(normalized) <= MAX_TRANSPORT_CHARS:
        return normalized
    content_limit = MAX_TRANSPORT_CHARS - len(_TRUNCATION_SUFFIX)
    return normalized[:content_limit] + _TRUNCATION_SUFFIX


def as_plain_text_message(text: str) -> Message:
    """Keep model-produced CQ syntax inert."""
    return Message(MessageSegment.text(_prepare_reply(text)))
