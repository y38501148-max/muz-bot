import re

from nonebot.adapters.onebot.v11 import Message, MessageSegment

MAX_REPLY_CHARS = 30
_SENTENCE_END_RE = re.compile(r"[。！？!?…]+[”’」』】）》]*")


def _strip_unsupported_markdown_emphasis(text: str) -> str:
    result = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"\1", text, flags=re.DOTALL)
    return re.sub(r"__(?=\S)(.+?)(?<=\S)__", r"\1", result, flags=re.DOTALL)


def _prepare_reply(text: str) -> str:
    normalized = " ".join(_strip_unsupported_markdown_emphasis(text).split())
    sentence_end = _SENTENCE_END_RE.search(normalized)
    if (
        len(normalized) > MAX_REPLY_CHARS
        and sentence_end
        and sentence_end.end() <= MAX_REPLY_CHARS
        and sentence_end.end() < len(normalized)
    ):
        return normalized[: sentence_end.end()]
    if len(normalized) <= MAX_REPLY_CHARS:
        return normalized

    candidate = normalized[:MAX_REPLY_CHARS]
    sentence_end = _SENTENCE_END_RE.search(candidate)
    if sentence_end:
        return candidate[: sentence_end.end()]
    return candidate.rstrip()


def as_plain_text_message(text: str) -> Message:
    """Keep model-produced CQ syntax inert."""
    return Message(MessageSegment.text(_prepare_reply(text)))
