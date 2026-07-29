from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

MAX_WORKER_OUTPUT_BYTES = 64 * 1024
DEFAULT_WORKER_TIMEOUT_SECONDS = 12.0
WORKER_PATH = Path(__file__).with_name("document_worker.py")


async def _kill_and_wait(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        process.kill()
    wait_task = asyncio.create_task(process.wait())
    while not wait_task.done():
        try:
            await asyncio.shield(wait_task)
        except asyncio.CancelledError:
            # Do not release the global file semaphore while the parser still
            # owns CPU, memory, file descriptors, or the downloaded file.
            continue
    await wait_task


async def _read_bounded_output(process: asyncio.subprocess.Process) -> bytes:
    assert process.stdout is not None
    output = await process.stdout.read(MAX_WORKER_OUTPUT_BYTES + 1)
    if len(output) > MAX_WORKER_OUTPUT_BYTES:
        await _kill_and_wait(process)
        raise ValueError("文件解析进程输出过大")
    return_code = await process.wait()
    if return_code != 0:
        raise ValueError("文件解析进程失败")
    return output


async def extract_document_text_isolated(
    path: Path,
    original_name: object,
    *,
    timeout_seconds: float = DEFAULT_WORKER_TIMEOUT_SECONDS,
) -> str:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(WORKER_PATH),
        str(path),
        str(original_name or ""),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        limit=MAX_WORKER_OUTPUT_BYTES + 1,
    )
    try:
        raw = await asyncio.wait_for(
            _read_bounded_output(process),
            timeout=max(0.1, float(timeout_seconds)),
        )
    except asyncio.TimeoutError as error:
        await _kill_and_wait(process)
        raise ValueError("文件解析超时") from error
    except asyncio.CancelledError:
        await _kill_and_wait(process)
        raise
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("文件解析结果无效") from error
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise ValueError("文件内容无法安全解析")
    text = payload.get("text")
    if not isinstance(text, str) or not text:
        raise ValueError("文件解析结果无效")
    return text[:12_000]
