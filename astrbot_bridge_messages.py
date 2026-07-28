from nonebot.adapters.onebot.v11 import Message, MessageSegment


def as_plain_text_message(text: str) -> Message:
    """Keep model-produced CQ syntax inert."""
    return Message(MessageSegment.text(text))
