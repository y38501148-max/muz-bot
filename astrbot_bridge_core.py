# ruff: noqa: UP006, UP045 -- muz-bot still declares Python 3.8 support.

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, Optional
from urllib.parse import urlparse
from uuid import uuid4

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:6185"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class AstrBotApiError(RuntimeError):
    """Raised when AstrBot cannot complete a chat request."""


@dataclass(frozen=True)
class BridgeConfig:
    enabled: bool = False
    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""
    config_id: str = "default"
    timeout_seconds: float = 180.0
    max_concurrent: int = 3
    min_interval_seconds: float = 3.0
    passive_trigger_probability: float = 0.42


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
            raw.get("PASSIVE_TRIGGER_PROBABILITY", 0.42)
        )
    except (TypeError, ValueError) as error:
        raise ValueError(
            "TIMEOUT_SECONDS、MAX_CONCURRENT 和 "
            "MIN_INTERVAL_SECONDS、PASSIVE_TRIGGER_PROBABILITY 必须是数字"
        ) from error
    if timeout_seconds <= 0:
        raise ValueError("TIMEOUT_SECONDS 必须大于 0")
    if max_concurrent < 1 or max_concurrent > 20:
        raise ValueError("MAX_CONCURRENT 必须在 1 到 20 之间")
    if min_interval_seconds < 0 or min_interval_seconds > 300:
        raise ValueError("MIN_INTERVAL_SECONDS 必须在 0 到 300 之间")
    if not 0 <= passive_trigger_probability <= 1:
        raise ValueError("PASSIVE_TRIGGER_PROBABILITY 必须在 0 到 1 之间")

    return BridgeConfig(
        enabled=enabled,
        base_url=_validate_base_url(raw.get("ASTRBOT_BASE_URL")),
        api_key=api_key,
        config_id=str(raw.get("CONFIG_ID") or "default").strip(),
        timeout_seconds=timeout_seconds,
        max_concurrent=max_concurrent,
        min_interval_seconds=min_interval_seconds,
        passive_trigger_probability=passive_trigger_probability,
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
    def __init__(self, min_interval_seconds: float) -> None:
        self.min_interval_seconds = min_interval_seconds
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
        self._last_requests = {**self._last_requests, key: current}
        return 0


class ConversationInFlightGate:
    """Reject excess work immediately instead of building a paid request queue."""

    def __init__(self, max_concurrent: int) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent 必须大于 0")
        self.max_concurrent = max_concurrent
        self._active: FrozenSet[str] = frozenset()

    def try_enter(self, key: str) -> bool:
        if key in self._active or len(self._active) >= self.max_concurrent:
            return False
        self._active = self._active.union((key,))
        return True

    def leave(self, key: str) -> None:
        self._active = self._active.difference((key,))


class JsonSessionStore:
    def __init__(self, path: Path) -> None:
        self.path = path

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
        return dict(sessions)

    def _save(self, sessions: Dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"version": 1, "sessions": sessions},
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
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self.path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    def get_or_create(self, key: str) -> str:
        sessions = self._load()
        existing = sessions.get(key)
        if existing:
            return existing
        session_id = str(uuid4())
        self._save({**sessions, key: session_id})
        return session_id

    def reset(self, key: str) -> str:
        sessions = self._load()
        session_id = str(uuid4())
        self._save({**sessions, key: session_id})
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
