# ruff: noqa: UP006, UP045 -- muz-bot still declares Python 3.8 support.

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Deque, Dict, Iterable, Optional, Sequence, Tuple
from urllib.parse import urlparse
from uuid import uuid4

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:6185"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class AstrBotApiError(RuntimeError):
    """Raised when AstrBot cannot complete a chat request."""


class ConversationQueueFullError(AstrBotApiError):
    """Raised when the bounded paid-request backlog is full."""


class ConversationQueueExpiredError(AstrBotApiError):
    """Raised when a queued message is too old to remain useful."""


class RequestBudgetExceededError(AstrBotApiError):
    """Raised when a member, group, or global paid-request budget is spent."""


@dataclass(frozen=True)
class BridgeConfig:
    enabled: bool = False
    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""
    config_id: str = "default"
    timeout_seconds: float = 180.0
    max_concurrent: int = 3
    min_interval_seconds: float = 3.0
    passive_trigger_probability: float = 0.15
    queue_wait_seconds: float = 120.0
    member_requests_per_hour: int = 20
    group_requests_per_hour: int = 120
    global_requests_per_hour: int = 300


@dataclass(frozen=True)
class ConversationIdentity:
    key: str
    username: str


@dataclass(frozen=True)
class ChatResult:
    text: str
    session_id: Optional[str] = None


def _parse_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _validate_base_url(value: object) -> str:
    base_url = str(value or DEFAULT_BASE_URL).strip().rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("ASTRBOT_BASE_URL 必须是合法的 HTTP(S) 地址")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("ASTRBOT_BASE_URL 不允许内嵌凭据")
    if parsed.scheme == "http" and parsed.hostname not in LOOPBACK_HOSTS:
        raise ValueError("外部 ASTRBOT_BASE_URL 必须使用 HTTPS")
    return base_url


def load_bridge_config(path: Path) -> BridgeConfig:
    if not path.exists():
        return BridgeConfig()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"{path} 不是合法的 AstrBot 配置：{error}") from error
    if not isinstance(raw, dict):
        raise ValueError(  # noqa: TRY004
            f"{path} 的顶层必须是 JSON 对象"
        )

    enabled = _parse_bool(raw.get("ENABLED"), False)
    api_key = str(raw.get("API_KEY") or "").strip()
    if enabled and not api_key:
        raise ValueError("启用 AstrBot 接入时必须配置 API_KEY")

    try:
        timeout_seconds = float(raw.get("TIMEOUT_SECONDS", 180))
        max_concurrent = int(raw.get("MAX_CONCURRENT", 3))
        min_interval_seconds = float(raw.get("MIN_INTERVAL_SECONDS", 3))
        passive_trigger_probability = float(
            raw.get("PASSIVE_TRIGGER_PROBABILITY", 0.15)
        )
        queue_wait_seconds = float(raw.get("QUEUE_WAIT_SECONDS", 120))
        member_requests_per_hour = int(raw.get("MEMBER_REQUESTS_PER_HOUR", 20))
        group_requests_per_hour = int(raw.get("GROUP_REQUESTS_PER_HOUR", 120))
        global_requests_per_hour = int(raw.get("GLOBAL_REQUESTS_PER_HOUR", 300))
    except (TypeError, ValueError) as error:
        raise ValueError(
            "TIMEOUT_SECONDS、MAX_CONCURRENT 和 "
            "MIN_INTERVAL_SECONDS、PASSIVE_TRIGGER_PROBABILITY、队列与预算配置"
            "必须是数字"
        ) from error
    if timeout_seconds <= 0:
        raise ValueError("TIMEOUT_SECONDS 必须大于 0")
    if max_concurrent < 1 or max_concurrent > 20:
        raise ValueError("MAX_CONCURRENT 必须在 1 到 20 之间")
    if min_interval_seconds < 0 or min_interval_seconds > 300:
        raise ValueError("MIN_INTERVAL_SECONDS 必须在 0 到 300 之间")
    if not 0 <= passive_trigger_probability <= 1:
        raise ValueError("PASSIVE_TRIGGER_PROBABILITY 必须在 0 到 1 之间")
    if queue_wait_seconds < 1 or queue_wait_seconds > 600:
        raise ValueError("QUEUE_WAIT_SECONDS 必须在 1 到 600 之间")
    hourly_limits = (
        member_requests_per_hour,
        group_requests_per_hour,
        global_requests_per_hour,
    )
    if any(limit < 1 or limit > 10_000 for limit in hourly_limits):
        raise ValueError("每小时请求预算必须在 1 到 10000 之间")

    return BridgeConfig(
        enabled=enabled,
        base_url=_validate_base_url(raw.get("ASTRBOT_BASE_URL")),
        api_key=api_key,
        config_id=str(raw.get("CONFIG_ID") or "default").strip(),
        timeout_seconds=timeout_seconds,
        max_concurrent=max_concurrent,
        min_interval_seconds=min_interval_seconds,
        passive_trigger_probability=passive_trigger_probability,
        queue_wait_seconds=queue_wait_seconds,
        member_requests_per_hour=member_requests_per_hour,
        group_requests_per_hour=group_requests_per_hour,
        global_requests_per_hour=global_requests_per_hour,
    )


def is_status_command(argument: str) -> bool:
    """Return whether an /ai argument is the sole supported action."""
    return argument.strip() == "状态"


def should_passively_reply(
    message: str,
    *,
    directed: bool,
    probability: float,
    sample: float,
) -> bool:
    """Apply the passive group-message trigger policy."""
    normalized = message.strip()
    if not normalized or normalized.startswith("/") or directed:
        return False
    return sample < probability


def _validate_qq_id(value: object, label: str) -> str:
    normalized = str(value).strip()
    if not normalized.isdigit():
        raise ValueError(f"{label} 必须是 QQ 数字 ID")
    return normalized


def build_conversation_identity(
    user_id: object,
    group_id: Optional[object],
) -> ConversationIdentity:
    if group_id is not None:
        normalized_group = _validate_qq_id(group_id, "群号")
        return ConversationIdentity(
            key=f"group:{normalized_group}",
            username="qq-group",
        )
    normalized_user = _validate_qq_id(user_id, "QQ 号")
    return ConversationIdentity(
        key=f"private:{normalized_user}",
        username="qq-user",
    )


class ConversationRateLimiter:
    def __init__(
        self,
        min_interval_seconds: float,
        *,
        max_identities: int = 5_000,
    ) -> None:
        if max_identities < 1:
            raise ValueError("限流身份容量必须大于 0")
        self.min_interval_seconds = min_interval_seconds
        self.max_identities = max_identities
        self._last_requests: Dict[str, float] = {}

    def check(
        self,
        key: str,
        *,
        now: Optional[float] = None,
    ) -> float:
        current = time.monotonic() if now is None else now
        previous = self._last_requests.get(key)
        if previous is not None:
            retry_after = self.min_interval_seconds - (current - previous)
            if retry_after > 0:
                return retry_after
        cutoff = current - max(self.min_interval_seconds, 1)
        active = {
            identity: timestamp
            for identity, timestamp in self._last_requests.items()
            if timestamp > cutoff and identity != key
        }
        if len(active) >= self.max_identities:
            oldest = min(active, key=active.get)
            active = {
                identity: timestamp
                for identity, timestamp in active.items()
                if identity != oldest
            }
        self._last_requests = {**active, key: current}
        return 0


@dataclass(frozen=True)
class _ConversationQueueEntry:
    lock: asyncio.Lock
    references: int


class ConversationQueue:
    """Process each conversation FIFO while allowing bounded cross-chat work."""

    def __init__(
        self,
        max_concurrent: int,
        *,
        max_per_conversation: int = 20,
        max_pending_total: int = 60,
    ) -> None:
        limits = (max_concurrent, max_per_conversation, max_pending_total)
        if any(limit < 1 for limit in limits):
            raise ValueError("队列容量必须大于 0")
        self.max_concurrent = max_concurrent
        self.max_per_conversation = max_per_conversation
        self.max_pending_total = max_pending_total
        self._global_limit: Optional[asyncio.Semaphore] = None
        self._state_lock: Optional[asyncio.Lock] = None
        self._runtime_loop: Optional[asyncio.AbstractEventLoop] = None
        self._entries: Dict[str, _ConversationQueueEntry] = {}

    def _ensure_runtime(self) -> Tuple[asyncio.Lock, asyncio.Semaphore]:
        loop = asyncio.get_running_loop()
        if self._runtime_loop is not loop:
            if self._entries:
                raise RuntimeError("ConversationQueue 不能跨事件循环复用活动队列")
            self._runtime_loop = loop
            self._state_lock = asyncio.Lock()
            self._global_limit = asyncio.Semaphore(self.max_concurrent)
        assert self._state_lock is not None
        assert self._global_limit is not None
        return self._state_lock, self._global_limit

    @asynccontextmanager
    async def turn(
        self,
        key: str,
        *,
        wait_timeout_seconds: Optional[float] = None,
        bypass_capacity: bool = False,
    ) -> AsyncIterator[None]:
        if not key:
            raise ValueError("conversation key 不能为空")
        state_lock, _ = self._ensure_runtime()
        async with state_lock:
            existing = self._entries.get(key)
            conversation_references = existing.references if existing else 0
            total_references = sum(item.references for item in self._entries.values())
            if (
                not bypass_capacity
                and conversation_references >= self.max_per_conversation
            ):
                raise ConversationQueueFullError("当前对话排队消息过多，请稍后再试")
            if not bypass_capacity and total_references >= self.max_pending_total:
                raise ConversationQueueFullError("机器人总排队消息过多，请稍后再试")
            entry = existing or _ConversationQueueEntry(
                lock=asyncio.Lock(),
                references=0,
            )
            self._entries = {
                **self._entries,
                key: _ConversationQueueEntry(
                    lock=entry.lock,
                    references=entry.references + 1,
                ),
            }
        try:
            try:
                if wait_timeout_seconds is None:
                    await entry.lock.acquire()
                else:
                    await asyncio.wait_for(
                        entry.lock.acquire(),
                        timeout=wait_timeout_seconds,
                    )
            except asyncio.TimeoutError as error:
                raise ConversationQueueExpiredError(
                    "消息排队超时，已取消本次请求"
                ) from error
            try:
                yield
            finally:
                entry.lock.release()
        finally:
            async with state_lock:
                current = self._entries.get(key)
                if current is not None and current.references <= 1:
                    self._entries = {
                        item_key: item
                        for item_key, item in self._entries.items()
                        if item_key != key
                    }
                elif current is not None:
                    self._entries = {
                        **self._entries,
                        key: _ConversationQueueEntry(
                            lock=current.lock,
                            references=current.references - 1,
                        ),
                    }

    @asynccontextmanager
    async def global_slot(
        self,
        *,
        wait_timeout_seconds: Optional[float] = None,
    ) -> AsyncIterator[None]:
        """Acquire global paid-call concurrency only around the network call."""
        _, global_limit = self._ensure_runtime()
        try:
            if wait_timeout_seconds is None:
                await global_limit.acquire()
            else:
                await asyncio.wait_for(
                    global_limit.acquire(),
                    timeout=wait_timeout_seconds,
                )
        except asyncio.TimeoutError as error:
            raise ConversationQueueExpiredError(
                "消息等待全局并发槽超时，已取消本次请求"
            ) from error
        try:
            yield
        finally:
            global_limit.release()


class SlidingWindowBudget:
    """Bound paid calls by multiple identities within one rolling window."""

    def __init__(
        self,
        *,
        limits: Dict[str, int],
        window_seconds: float = 3600,
    ) -> None:
        if window_seconds <= 0 or not limits:
            raise ValueError("调用预算窗口和限制必须大于 0")
        if any(limit < 1 for limit in limits.values()):
            raise ValueError("调用预算限制必须大于 0")
        self.limits = dict(limits)
        self.window_seconds = window_seconds
        self._events: Dict[str, Deque[float]] = {}

    def consume(
        self,
        buckets: Sequence[Tuple[str, str]],
        *,
        now: Optional[float] = None,
    ) -> None:
        current = time.monotonic() if now is None else now
        cutoff = current - self.window_seconds
        self._events = {
            key: deque(timestamp for timestamp in events if timestamp > cutoff)
            for key, events in self._events.items()
            if any(timestamp > cutoff for timestamp in events)
        }
        prepared = []
        for scope, identity in buckets:
            if scope not in self.limits:
                raise ValueError(f"未知预算范围：{scope}")
            key = f"{scope}:{identity}"
            events = deque(
                timestamp
                for timestamp in self._events.get(key, ())
                if timestamp > cutoff
            )
            if len(events) >= self.limits[scope]:
                raise RequestBudgetExceededError("请求频率已达到安全预算，请稍后再试")
            prepared.append((key, events))
        for key, events in prepared:
            events.append(current)
            self._events = {**self._events, key: events}


class JsonSessionStore:
    def __init__(
        self,
        path: Path,
        *,
        key_path: Optional[Path] = None,
        max_sessions: int = 5_000,
    ) -> None:
        if max_sessions < 1:
            raise ValueError("会话状态容量必须大于 0")
        self.path = path
        self.key_path = key_path or path.with_suffix(".key")
        self.max_sessions = max_sessions

    def _load_or_create_key(self) -> bytes:
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.key_path.parent, 0o700)
        try:
            key = self.key_path.read_bytes()
        except FileNotFoundError:
            key = secrets.token_bytes(32)
            try:
                descriptor = os.open(
                    str(self.key_path),
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                key = self.key_path.read_bytes()
            else:
                try:
                    os.write(descriptor, key)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except OSError as error:
            raise ValueError(f"AstrBot 会话状态密钥不可用：{error}") from error
        if len(key) != 32:
            raise ValueError("AstrBot 会话状态密钥长度无效")
        os.chmod(self.key_path, 0o600)
        return key

    def _storage_key(self, key: str) -> str:
        if key.startswith("hmac:") and len(key) == 69:
            return key
        digest = hmac.new(
            self._load_or_create_key(),
            key.encode(),
            hashlib.sha256,
        ).hexdigest()
        return f"hmac:{digest}"

    def _load(self) -> Dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"AstrBot 会话状态文件损坏：{error}") from error
        sessions = raw.get("sessions") if isinstance(raw, dict) else None
        if not isinstance(sessions, dict):
            raise ValueError(  # noqa: TRY004
                "AstrBot 会话状态文件缺少 sessions 对象"
            )
        if not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in sessions.items()
        ):
            raise ValueError("AstrBot 会话状态文件包含无效会话")
        normalized = {self._storage_key(key): value for key, value in sessions.items()}
        if normalized != sessions:
            self._save(normalized)
        return normalized

    def _save(self, sessions: Dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        bounded_sessions = dict(list(sessions.items())[-self.max_sessions :])
        payload = json.dumps(
            {"version": 1, "sessions": bounded_sessions},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        descriptor, temporary_name = tempfile.mkstemp(
            dir=str(self.path.parent),
            prefix=f".{self.path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    def get_or_create(self, key: str) -> str:
        sessions = self._load()
        storage_key = self._storage_key(key)
        existing = sessions.get(storage_key)
        if existing:
            return existing
        session_id = str(uuid4())
        self._save({**sessions, storage_key: session_id})
        return session_id

    def reset(self, key: str) -> str:
        sessions = self._load()
        storage_key = self._storage_key(key)
        session_id = str(uuid4())
        self._save({**sessions, storage_key: session_id})
        return session_id


class _SseAccumulator:
    def __init__(self) -> None:
        self.streaming_text = ""
        self.final_text = ""
        self.session_id: Optional[str] = None

    def add(self, event: object) -> None:
        if not isinstance(event, dict):
            return
        event_type = str(event.get("type") or "")
        if event_type == "error":
            message = str(event.get("data") or "AstrBot 返回未知错误")
            raise AstrBotApiError(message)
        if event_type == "session_id":
            raw_session_id = event.get("session_id")
            if raw_session_id:
                self.session_id = str(raw_session_id)
            return
        if event_type != "plain":
            return
        chain_type = event.get("chain_type")
        if chain_type in {"reasoning", "tool_call", "tool_call_result"}:
            return
        data = str(event.get("data") or "")
        if event.get("streaming"):
            self.streaming_text += data
        else:
            self.final_text = data

    def result(self) -> ChatResult:
        text = self.streaming_text or self.final_text
        if not text.strip():
            raise AstrBotApiError("AstrBot 未返回可发送的文本内容")
        return ChatResult(text=text, session_id=self.session_id)


def _parse_sse_data(raw_data: str) -> object:
    try:
        return json.loads(raw_data)
    except json.JSONDecodeError as error:
        raise AstrBotApiError("AstrBot 返回了无法解析的 SSE 数据") from error


def consume_sse_lines(lines: Iterable[str]) -> ChatResult:
    accumulator = _SseAccumulator()
    data_lines = []
    for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        if not line:
            if data_lines:
                accumulator.add(_parse_sse_data("\n".join(data_lines)))
                data_lines = []
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        accumulator.add(_parse_sse_data("\n".join(data_lines)))
    return accumulator.result()


class AstrBotClient:
    def __init__(
        self,
        config: BridgeConfig,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.config = config
        self._http_client = http_client

    async def chat(
        self,
        *,
        username: str,
        session_id: str,
        message: str,
    ) -> ChatResult:
        if not self.config.enabled:
            raise AstrBotApiError("AstrBot 接入尚未启用")
        payload = {
            "username": username,
            "session_id": session_id,
            "message": message,
            "enable_streaming": False,
            # The bridge envelope may contain ephemeral member memory/media
            # metadata. The gateway strips it before conversation history is
            # assembled, and this prevents a second raw copy in WebChat history.
            "_skip_user_history": True,
        }
        if self.config.config_id:
            payload["config_id"] = self.config.config_id
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Accept": "text/event-stream",
        }
        client = self._http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                self.config.timeout_seconds,
                connect=min(15.0, self.config.timeout_seconds),
            ),
            trust_env=False,
        )
        owns_client = self._http_client is None
        try:
            async with client.stream(
                "POST",
                f"{self.config.base_url}/api/v1/chat",
                json=payload,
                headers=headers,
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise AstrBotApiError(f"AstrBot HTTP {response.status_code}")
                accumulator = _SseAccumulator()
                data_lines = []
                async for raw_line in response.aiter_lines():
                    line = raw_line.rstrip("\r\n")
                    if not line:
                        if data_lines:
                            accumulator.add(_parse_sse_data("\n".join(data_lines)))
                            data_lines = []
                        continue
                    if line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                if data_lines:
                    accumulator.add(_parse_sse_data("\n".join(data_lines)))
                return accumulator.result()
        except httpx.TimeoutException as error:
            raise AstrBotApiError("AstrBot 请求超时") from error
        except httpx.RequestError as error:
            raise AstrBotApiError(
                f"无法连接 AstrBot：{error.__class__.__name__}"
            ) from error
        finally:
            if owns_client:
                await client.aclose()
