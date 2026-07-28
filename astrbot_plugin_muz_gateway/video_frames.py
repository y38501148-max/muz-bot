# ruff: noqa: UP006 -- keep annotations aligned with the bridge package.

from __future__ import annotations

import asyncio
import json
import os
import resource
import tempfile
import uuid
from pathlib import Path
from typing import List

from .web_access import download_public_video

MAX_VIDEO_DURATION_SECONDS = 120
MAX_VIDEO_PIXELS = 3840 * 2160
MEDIA_PROCESS_MEMORY_BYTES = 512 * 1024 * 1024
VIDEO_SEMAPHORE = None
VIDEO_SEMAPHORE_LOOP = None
MEDIA_INPUT_ARGS = (
    "-protocol_whitelist",
    "pipe",
    "-i",
    "pipe:0",
)
FRAME_FILTER = "select='eq(n,0)+gte(t-prev_selected_t,5)',scale=min(1024\\,iw):-2"


def _limit_media_process() -> None:
    resource.setrlimit(
        resource.RLIMIT_AS,
        (MEDIA_PROCESS_MEMORY_BYTES, MEDIA_PROCESS_MEMORY_BYTES),
    )
    resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024, 64 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))


def _validate_probe(value: object) -> None:
    if not isinstance(value, dict):
        raise TypeError("视频元数据无效")
    streams = value.get("streams")
    video_streams = (
        [stream for stream in streams if stream.get("codec_type") == "video"]
        if isinstance(streams, list)
        else []
    )
    if not video_streams:
        raise ValueError("文件不包含视频画面")
    stream = video_streams[0]
    try:
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
        duration = float((value.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError) as error:
        raise ValueError("视频元数据无效") from error
    if width < 1 or height < 1 or width * height > MAX_VIDEO_PIXELS:
        raise ValueError("视频分辨率超过 4K 安全限制")
    if duration <= 0 or duration > MAX_VIDEO_DURATION_SECONDS:
        raise ValueError("视频时长必须在 120 秒以内")


async def _probe_video(video_bytes: bytes) -> None:
    process = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=codec_type,width,height",
        "-of",
        "json",
        *MEDIA_INPUT_ARGS,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        preexec_fn=_limit_media_process,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(input=video_bytes),
            timeout=8,
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        raise ValueError("视频元数据检查超时")
    except asyncio.CancelledError:
        process.kill()
        await asyncio.shield(process.communicate())
        raise
    if process.returncode != 0:
        detail = stderr.decode(errors="replace")[-200:]
        raise ValueError(f"视频元数据检查失败：{detail}")
    try:
        metadata = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise ValueError("视频元数据无法解析") from error
    _validate_probe(metadata)


def _video_semaphore() -> asyncio.Semaphore:
    global VIDEO_SEMAPHORE, VIDEO_SEMAPHORE_LOOP
    loop = asyncio.get_running_loop()
    if VIDEO_SEMAPHORE_LOOP is not loop:
        VIDEO_SEMAPHORE_LOOP = loop
        VIDEO_SEMAPHORE = asyncio.Semaphore(1)
    return VIDEO_SEMAPHORE


async def extract_video_frames(
    url: object,
    work_root: Path,
    *,
    max_frames: int = 4,
) -> List[str]:
    """Download one bounded public video and extract evenly sampled JPEG frames."""
    async with _video_semaphore():
        return await _extract_video_frames_serial(
            url,
            work_root,
            max_frames=max_frames,
        )


async def _extract_video_frames_serial(
    url: object,
    work_root: Path,
    *,
    max_frames: int,
) -> List[str]:
    work_root.mkdir(parents=True, exist_ok=True)
    descriptor, source_name = tempfile.mkstemp(
        prefix="muz-video-",
        suffix=".bin",
        dir=str(work_root),
    )
    os.close(descriptor)
    video_path = Path(source_name)
    video_path.unlink()
    # The downloader recreates the file with private permissions.
    await download_public_video(url, video_path)
    try:
        video_bytes = video_path.read_bytes()
    finally:
        video_path.unlink(missing_ok=True)
    await _probe_video(video_bytes)
    frame_prefix = f"muz-frame-{uuid.uuid4().hex}"
    output_pattern = str(work_root / f"{frame_prefix}-%02d.jpg")
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        *MEDIA_INPUT_ARGS,
        "-an",
        "-sn",
        "-dn",
        "-vf",
        FRAME_FILTER,
        "-c:v",
        "mjpeg",
        "-q:v",
        "3",
        "-pix_fmt",
        "yuvj420p",
        "-threads",
        "1",
        "-fps_mode",
        "vfr",
        "-frames:v",
        str(max_frames),
        output_pattern,
        stdout=asyncio.subprocess.DEVNULL,
        stdin=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        preexec_fn=_limit_media_process,
    )
    try:
        _, stderr = await asyncio.wait_for(
            process.communicate(input=video_bytes),
            timeout=30,
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        for path in work_root.glob(f"{frame_prefix}-*.jpg"):
            path.unlink(missing_ok=True)
        raise ValueError("视频关键帧提取超时")
    except asyncio.CancelledError:
        process.kill()
        await asyncio.shield(process.communicate())
        for path in work_root.glob(f"{frame_prefix}-*.jpg"):
            path.unlink(missing_ok=True)
        raise
    if process.returncode != 0:
        for path in work_root.glob(f"{frame_prefix}-*.jpg"):
            path.unlink(missing_ok=True)
        message = stderr.decode(errors="replace")[-300:]
        raise ValueError(f"视频关键帧提取失败：{message}")
    frames = sorted(str(path) for path in work_root.glob(f"{frame_prefix}-*.jpg"))
    if not frames:
        raise ValueError("视频中没有可分析的画面")
    return frames[:max_frames]
