# ruff: noqa: UP006 -- muz-bot still declares Python 3.8 support.

from __future__ import annotations

import re
from typing import Iterable, List, Tuple

from nonebot.adapters.onebot.v11 import Message

_HTTP_URL = re.compile(r"^https?://", re.IGNORECASE)


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


def extract_event_media(
    event: object,
    *,
    max_images: int = 4,
    max_videos: int = 1,
) -> Tuple[List[str], List[str]]:
    """Extract bounded HTTP media references from a OneBot event and its reply."""
    messages = []
    event_message = getattr(event, "message", None)
    if isinstance(event_message, Message):
        messages.append(event_message)
    reply_message = getattr(getattr(event, "reply", None), "message", None)
    if isinstance(reply_message, Message):
        messages.append(reply_message)

    images: List[str] = []
    videos: List[str] = []
    for message in messages:
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
