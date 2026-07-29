# ruff: noqa: UP006, UP045 -- muz-bot still declares Python 3.8 support.

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple
from urllib.parse import urlparse

from nonebot.adapters.onebot.v11 import Message

_HTTP_URL = re.compile(r"^https?://", re.IGNORECASE)


@dataclass(frozen=True)
class MessageFile:
    name: str
    file_id: str
    busid: int
    url: str


def _event_messages(
    event: object,
    *,
    reply_message: Optional[Message] = None,
    include_event_reply: bool = True,
) -> List[Message]:
    messages: List[Message] = []
    candidates = [getattr(event, "message", None)]
    if include_event_reply:
        candidates.append(getattr(getattr(event, "reply", None), "message", None))
    candidates.append(reply_message)
    for candidate in candidates:
        if isinstance(candidate, Message) and candidate not in messages:
            messages.append(candidate)
    return messages


def _urls_from_message(
    message: Iterable,
    *,
    max_images: int,
    max_videos: int,
) -> Tuple[List[str], List[str]]:
    images: List[str] = []
    videos: List[str] = []
    for segment in message:
        segment_type = str(getattr(segment, "type", ""))
        if segment_type not in {"image", "video"}:
            continue
        data = getattr(segment, "data", {}) or {}
        candidate = str(data.get("url") or data.get("file") or "").strip()
        if not _HTTP_URL.match(candidate):
            continue
        target = images if segment_type == "image" else videos
        limit = max_images if segment_type == "image" else max_videos
        if candidate not in target and len(target) < limit:
            target.append(candidate)
    return images, videos


def extract_message_media(
    message: Iterable,
    *,
    max_images: int = 4,
    max_videos: int = 1,
) -> Tuple[List[str], List[str]]:
    return _urls_from_message(
        message,
        max_images=max_images,
        max_videos=max_videos,
    )


def _clean_label(value: object) -> str:
    label = " ".join(str(value or "").split()).strip("[]【】")
    return label[:80]


def _descriptions_from_message(message: Iterable) -> List[str]:
    descriptions: List[str] = []
    for segment in message:
        segment_type = str(getattr(segment, "type", ""))
        data = getattr(segment, "data", {}) or {}
        if segment_type == "face":
            raw = data.get("raw")
            face_text = raw.get("faceText") if isinstance(raw, dict) else ""
            label = _clean_label(face_text or data.get("summary"))
            if not label:
                label = f"ID {str(data.get('id') or '未知')[:20]}"
            description = f"QQ表情：{label}"
        elif segment_type == "mface":
            label = _clean_label(data.get("summary"))
            if not label:
                label = f"ID {str(data.get('emoji_id') or '未知')[:20]}"
            description = f"表情包：{label}"
        elif segment_type == "image" and data.get("emoji_id"):
            label = _clean_label(data.get("summary"))
            description = f"表情包：{label}" if label else ""
        else:
            description = ""
        if description and description not in descriptions:
            descriptions.append(description)
    return descriptions


def describe_message_media(message: Iterable) -> str:
    descriptions = _descriptions_from_message(message)
    if not descriptions:
        return ""
    return "消息包含" + "；".join(descriptions[:4])


def describe_event_media(
    event: object,
    *,
    reply_message: Optional[Message] = None,
    include_event_reply: bool = True,
) -> str:
    descriptions: List[str] = []
    for message in _event_messages(
        event,
        reply_message=reply_message,
        include_event_reply=include_event_reply,
    ):
        descriptions.extend(
            item
            for item in _descriptions_from_message(message)
            if item not in descriptions
        )
    if not descriptions:
        return ""
    return "消息包含" + "；".join(descriptions[:4])


def extract_reply_message_id(event: object) -> Optional[str]:
    messages = []
    for candidate in (
        getattr(event, "original_message", None),
        getattr(event, "message", None),
    ):
        if isinstance(candidate, Message) and candidate not in messages:
            messages.append(candidate)
    for message in messages:
        for segment in message:
            if str(getattr(segment, "type", "")) != "reply":
                continue
            data = getattr(segment, "data", {}) or {}
            message_id = str(data.get("id") or "").strip()
            if message_id:
                return message_id[:64]
    return None


def _file_from_segment(segment: object) -> Optional[MessageFile]:
    if str(getattr(segment, "type", "")) != "file":
        return None
    data = getattr(segment, "data", {}) or {}
    raw_url = str(data.get("url") or "").strip()
    raw_file = str(data.get("file") or "").strip()
    url = raw_url if _HTTP_URL.match(raw_url) else ""
    if not url and _HTTP_URL.match(raw_file):
        url = raw_file
    raw_name = str(data.get("name") or raw_file or "引用文件")
    if _HTTP_URL.match(raw_name):
        raw_name = urlparse(raw_name).path.rsplit("/", 1)[-1] or "引用文件"
    name = re.split(r"[/\\]", raw_name)[-1].strip()[:160] or "引用文件"
    file_id = str(data.get("file_id") or data.get("id") or "").strip()[:256]
    try:
        busid = int(data.get("busid") or data.get("bus_id") or 0)
    except (TypeError, ValueError):
        busid = 0
    if not file_id and not url:
        return None
    return MessageFile(
        name=name,
        file_id=file_id,
        busid=busid,
        url=url[:2_048],
    )


def extract_message_files(
    message: Iterable,
    *,
    max_files: int = 2,
) -> List[MessageFile]:
    files = []
    for segment in message:
        candidate = _file_from_segment(segment)
        if candidate and candidate not in files:
            files.append(candidate)
        if len(files) >= max_files:
            break
    return files


def extract_event_files(
    event: object,
    *,
    reply_message: Optional[Message] = None,
    max_files: int = 2,
    include_event_reply: bool = True,
) -> List[MessageFile]:
    files: List[MessageFile] = []
    for message in _event_messages(
        event,
        reply_message=reply_message,
        include_event_reply=include_event_reply,
    ):
        for segment in message:
            candidate = _file_from_segment(segment)
            if candidate and candidate not in files:
                files.append(candidate)
            if len(files) >= max_files:
                return files
    return files


def extract_event_media(
    event: object,
    *,
    max_images: int = 4,
    max_videos: int = 1,
    reply_message: Optional[Message] = None,
    include_event_reply: bool = True,
) -> Tuple[List[str], List[str]]:
    """Extract bounded HTTP media references from a OneBot event and its reply."""
    images: List[str] = []
    videos: List[str] = []
    for message in _event_messages(
        event,
        reply_message=reply_message,
        include_event_reply=include_event_reply,
    ):
        next_images, next_videos = _urls_from_message(
            message,
            max_images=max_images - len(images),
            max_videos=max_videos - len(videos),
        )
        images.extend(url for url in next_images if url not in images)
        videos.extend(url for url in next_videos if url not in videos)
        images = images[:max_images]
        videos = videos[:max_videos]
    return images, videos
