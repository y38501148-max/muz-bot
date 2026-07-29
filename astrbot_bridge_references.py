# ruff: noqa: UP006, UP045 -- muz-bot still declares Python 3.8 support.

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from nonebot.adapters.onebot.v11 import Message, MessageSegment

MAX_FORWARD_ITEMS = 2
MAX_FORWARD_NODES = 40
MAX_FORWARD_SEGMENTS = 200
MAX_REFERENCE_CHARS = 8_000


@dataclass(frozen=True)
class ForwardItem:
    message_id: str
    inline_content: object


@dataclass(frozen=True)
class ForwardSnapshot:
    text: str
    message: Message


def _clean_text(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def extract_message_text(message: Optional[Message], limit: int = 8_000) -> str:
    if not isinstance(message, Message):
        return ""
    chunks = []
    for segment in message:
        if str(getattr(segment, "type", "")) != "text":
            continue
        data = getattr(segment, "data", {}) or {}
        text = str(data.get("text") or "").strip()
        if text:
            chunks.append(text)
    return " ".join(chunks).strip()[:limit]


def extract_forward_items(message: object) -> List[ForwardItem]:
    if not isinstance(message, Message):
        return []
    result = []
    for segment in message:
        segment_type = str(getattr(segment, "type", "")).lower()
        if segment_type not in {"forward", "nodes"}:
            continue
        data = getattr(segment, "data", {}) or {}
        message_id = str(data.get("id") or data.get("message_id") or "").strip()
        inline_content = data.get("content") or data.get("messages")
        if message_id or inline_content:
            result.append(
                ForwardItem(
                    message_id=message_id[:128],
                    inline_content=inline_content,
                )
            )
        if len(result) >= MAX_FORWARD_ITEMS:
            break
    return result


def _sender_name(record: dict) -> str:
    sender = record.get("sender")
    if not isinstance(sender, dict):
        sender = {}
    data = record.get("data")
    if not isinstance(data, dict):
        data = {}
    return _clean_text(
        sender.get("card")
        or sender.get("nickname")
        or record.get("nickname")
        or record.get("name")
        or data.get("nickname")
        or data.get("name")
        or "群友",
        80,
    )


def _record_content(record: dict) -> object:
    data = record.get("data")
    if not isinstance(data, dict):
        data = {}
    return (
        record.get("message")
        or record.get("content")
        or data.get("content")
        or data.get("message")
    )


def _as_message(value: object, max_segments: int) -> Optional[Message]:
    if isinstance(value, Message):
        return value
    if isinstance(value, list):
        normalized = []
        for item in value[:max_segments]:
            if isinstance(item, MessageSegment):
                normalized.append(item)
            elif isinstance(item, dict) and item.get("type"):
                data = item.get("data")
                normalized.append(
                    MessageSegment(
                        str(item.get("type")),
                        data if isinstance(data, dict) else {},
                    )
                )
            else:
                return None
        return Message(normalized)
    if isinstance(value, str):
        try:
            return Message(value[:MAX_REFERENCE_CHARS])
        except (TypeError, ValueError):
            return None
    return None


def _render_message(
    value: object,
    *,
    depth: int,
    flat_segments: List[MessageSegment],
    node_budget: List[int],
    segment_budget: List[int],
    char_budget: List[int],
) -> str:
    if depth > 2:
        return "[嵌套转发已省略]"
    message = _as_message(value, segment_budget[0])
    if message is None:
        return _clean_text(value, 500) if isinstance(value, str) else ""
    chunks = []
    for segment in message:
        if segment_budget[0] <= 0 or char_budget[0] <= 0:
            break
        segment_budget[0] -= 1
        segment_type = str(getattr(segment, "type", "")).lower()
        data = getattr(segment, "data", {}) or {}
        if segment_type == "text":
            text = _clean_text(
                data.get("text"),
                min(2_000, char_budget[0]),
            )
            if text:
                chunks.append(text)
                char_budget[0] -= len(text)
        elif segment_type in {"image", "video", "file", "face", "mface"}:
            if len(flat_segments) < 20:
                flat_segments.append(segment)
            if segment_type == "image":
                placeholder = "[图片]"
            elif segment_type == "video":
                placeholder = "[视频]"
            elif segment_type == "file":
                name = _clean_text(data.get("name") or data.get("file"), 80)
                placeholder = f"[文件：{name or '未命名'}]"
            elif segment_type in {"face", "mface"}:
                summary = _clean_text(data.get("summary"), 40)
                placeholder = f"[表情{f'：{summary}' if summary else ''}]"
            placeholder = placeholder[: char_budget[0]]
            if placeholder:
                chunks.append(placeholder)
                char_budget[0] -= len(placeholder)
        elif segment_type in {"forward", "nodes", "node"}:
            nested = _record_content({"data": data})
            if nested:
                snapshot = _parse_forward_payload(
                    nested,
                    depth=depth + 1,
                    flat_segments=flat_segments,
                    node_budget=node_budget,
                    segment_budget=segment_budget,
                    char_budget=char_budget,
                )
                if snapshot.text:
                    chunks.append(snapshot.text)
    return " ".join(chunks).strip()


def _payload_records(payload: object) -> List[object]:
    if isinstance(payload, dict):
        for key in ("messages", "message", "content"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        return [payload]
    if isinstance(payload, (list, Message)):
        return list(payload)
    return []


def _parse_forward_payload(
    payload: object,
    *,
    depth: int,
    flat_segments: List[MessageSegment],
    node_budget: List[int],
    segment_budget: List[int],
    char_budget: List[int],
) -> ForwardSnapshot:
    lines = []
    for record in _payload_records(payload)[:MAX_FORWARD_NODES]:
        if node_budget[0] <= 0:
            break
        node_budget[0] -= 1
        if isinstance(record, MessageSegment):
            record = {"type": record.type, "data": record.data}
        if not isinstance(record, dict):
            text = _render_message(
                record,
                depth=depth,
                flat_segments=flat_segments,
                node_budget=node_budget,
                segment_budget=segment_budget,
                char_budget=char_budget,
            )
            if text:
                lines.append(text)
            continue
        record_type = str(record.get("type") or "").lower()
        if record_type not in {"node", "forward", "nodes"} and (
            "message" not in record and "content" not in record
        ):
            text = _render_message(
                [record],
                depth=depth,
                flat_segments=flat_segments,
                node_budget=node_budget,
                segment_budget=segment_budget,
                char_budget=char_budget,
            )
            if text:
                lines.append(text)
            continue
        content = _record_content(record)
        text = _render_message(
            content,
            depth=depth,
            flat_segments=flat_segments,
            node_budget=node_budget,
            segment_budget=segment_budget,
            char_budget=char_budget,
        )
        if text:
            lines.append(f"{_sender_name(record)}：{text}")
        if sum(len(line) for line in lines) >= MAX_REFERENCE_CHARS:
            break
    return ForwardSnapshot(
        text="\n".join(lines)[:MAX_REFERENCE_CHARS],
        message=Message(flat_segments),
    )


def parse_forward_payload(payload: object) -> ForwardSnapshot:
    return _parse_forward_payload(
        payload,
        depth=0,
        flat_segments=[],
        node_budget=[MAX_FORWARD_NODES],
        segment_budget=[MAX_FORWARD_SEGMENTS],
        char_budget=[MAX_REFERENCE_CHARS],
    )
