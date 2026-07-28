from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    FrozenSet,
    Iterable,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

X_SNOWFLAKE_EPOCH_MS = 1_288_834_974_657
BEIJING_TIMEZONE = timezone(timedelta(hours=8))
STATE_VERSION = 1


@dataclass(frozen=True)
class XMonitorConfig:
    username: str = "thsottiaux"
    keywords: Tuple[str, ...] = ("reset",)
    proxy_url: str = "http://127.0.0.1:7890"
    poll_minutes: int = 10
    lookback_hours: int = 24
    page_timeout_seconds: float = 30


@dataclass(frozen=True)
class XPost:
    id: str
    created_at: datetime
    text: str
    url: str
    matched_keywords: Tuple[str, ...] = ()


@dataclass(frozen=True)
class FetchBatch:
    discovered_ids: Tuple[str, ...] = ()
    posts: Tuple[XPost, ...] = ()
    coverage_warning: Optional[str] = None
    errors: Tuple[str, ...] = ()


@dataclass(frozen=True)
class PendingDelivery:
    group_id: str
    post_id: str


@dataclass(frozen=True)
class DeliveryRecord:
    group_id: str
    post_id: str


@dataclass(frozen=True)
class MonitorState:
    enabled_group_ids: Tuple[str, ...] = ()
    posts: Tuple[XPost, ...] = ()
    pending: Tuple[PendingDelivery, ...] = ()
    delivered: Tuple[DeliveryRecord, ...] = ()
    last_success_at: Optional[datetime] = None
    last_error: Optional[str] = None
    last_warning: Optional[str] = None


@dataclass(frozen=True)
class PollReport:
    discovered: int = 0
    new_posts: int = 0
    matched: int = 0
    sent: int = 0
    failed: int = 0
    skipped: bool = False
    coverage_warning: Optional[str] = None
    error: Optional[str] = None


class StateLoadError(RuntimeError):
    """Raised when persisted state cannot be trusted for deduplication."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def snowflake_created_at(post_id: str) -> datetime:
    if not isinstance(post_id, str) or not post_id.isdigit():
        raise ValueError("X post ID must contain only digits")
    timestamp_ms = (int(post_id) >> 22) + X_SNOWFLAKE_EPOCH_MS
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)


def match_keywords(text: str, keywords: Sequence[str]) -> Tuple[str, ...]:
    normalized_text = text.casefold()
    return tuple(
        keyword
        for keyword in keywords
        if keyword and keyword.casefold() in normalized_text
    )


def _post_sort_key(post: XPost) -> Tuple[datetime, str]:
    return post.created_at, post.id


def _pending_sort_key(
    delivery: PendingDelivery,
    posts_by_id: Mapping[str, XPost],
) -> Tuple[datetime, str, str]:
    post = posts_by_id.get(delivery.post_id)
    created_at = post.created_at if post else datetime.min.replace(tzinfo=timezone.utc)
    return created_at, delivery.post_id, delivery.group_id


def enable_group(
    state: MonitorState,
    group_id: str,
    now: datetime,
    lookback_hours: int,
) -> MonitorState:
    normalized_group_id = _validate_group_id(group_id)
    enabled_group_ids = tuple(
        sorted(set(state.enabled_group_ids) | {normalized_group_id})
    )
    delivered_pairs = {
        (item.group_id, item.post_id) for item in state.delivered
    }
    pending_pairs = {
        (item.group_id, item.post_id) for item in state.pending
    }
    cutoff = now - timedelta(hours=lookback_hours)
    additions = tuple(
        PendingDelivery(normalized_group_id, post.id)
        for post in sorted(state.posts, key=_post_sort_key)
        if post.created_at >= cutoff
        and post.matched_keywords
        and (normalized_group_id, post.id) not in delivered_pairs
        and (normalized_group_id, post.id) not in pending_pairs
    )
    if (
        enabled_group_ids == state.enabled_group_ids
        and not additions
    ):
        return state
    posts_by_id = {post.id: post for post in state.posts}
    pending = tuple(
        sorted(
            state.pending + additions,
            key=lambda item: _pending_sort_key(item, posts_by_id),
        )
    )
    return replace(
        state,
        enabled_group_ids=enabled_group_ids,
        pending=pending,
    )


def disable_group(state: MonitorState, group_id: str) -> MonitorState:
    normalized_group_id = _validate_group_id(group_id)
    enabled_group_ids = tuple(
        item for item in state.enabled_group_ids if item != normalized_group_id
    )
    pending = tuple(
        item for item in state.pending if item.group_id != normalized_group_id
    )
    if (
        enabled_group_ids == state.enabled_group_ids
        and pending == state.pending
    ):
        return state
    return replace(
        state,
        enabled_group_ids=enabled_group_ids,
        pending=pending,
    )


def add_posts(
    state: MonitorState,
    posts: Iterable[XPost],
) -> Tuple[MonitorState, int]:
    posts_by_id = {post.id: post for post in state.posts}
    new_posts = tuple(
        post for post in posts if post.id not in posts_by_id
    )
    if not new_posts:
        return state, 0

    updated_posts_by_id = {
        **posts_by_id,
        **{post.id: post for post in new_posts},
    }
    delivered_pairs = {
        (item.group_id, item.post_id) for item in state.delivered
    }
    pending_pairs = {
        (item.group_id, item.post_id) for item in state.pending
    }
    additions = tuple(
        PendingDelivery(group_id, post.id)
        for post in sorted(new_posts, key=_post_sort_key)
        if post.matched_keywords
        for group_id in state.enabled_group_ids
        if (group_id, post.id) not in delivered_pairs
        and (group_id, post.id) not in pending_pairs
    )
    updated_posts = tuple(
        sorted(updated_posts_by_id.values(), key=_post_sort_key)
    )
    pending = tuple(
        sorted(
            state.pending + additions,
            key=lambda item: _pending_sort_key(item, updated_posts_by_id),
        )
    )
    return (
        replace(state, posts=updated_posts, pending=pending),
        len(new_posts),
    )


def mark_delivery_succeeded(
    state: MonitorState,
    delivery: PendingDelivery,
) -> MonitorState:
    pending = tuple(item for item in state.pending if item != delivery)
    delivered_record = DeliveryRecord(delivery.group_id, delivery.post_id)
    delivered = (
        state.delivered
        if delivered_record in state.delivered
        else state.delivered + (delivered_record,)
    )
    return replace(state, pending=pending, delivered=delivered)


def prune_state(
    state: MonitorState,
    now: datetime,
    lookback_hours: int,
    retention_hours: int = 48,
) -> MonitorState:
    retention_cutoff = now - timedelta(hours=retention_hours)
    delivery_cutoff = now - timedelta(hours=lookback_hours)
    posts = tuple(
        post for post in state.posts if post.created_at >= retention_cutoff
    )
    posts_by_id = {post.id: post for post in posts}
    pending = tuple(
        item
        for item in state.pending
        if item.post_id in posts_by_id
        and posts_by_id[item.post_id].created_at >= delivery_cutoff
        and item.group_id in state.enabled_group_ids
    )
    delivered = tuple(
        item for item in state.delivered if item.post_id in posts_by_id
    )
    return replace(
        state,
        posts=posts,
        pending=pending,
        delivered=delivered,
    )


def format_notification(post: XPost) -> str:
    keywords = "、".join(post.matched_keywords)
    time_text = post.created_at.astimezone(BEIJING_TIMEZONE).strftime(
        "%Y-%m-%d %H:%M"
    )
    return (
        f"🔔 Tibo 发布了包含关键词「{keywords}」的新帖\n\n"
        f"时间：{time_text}（北京时间）\n"
        f"内容：{post.text}\n"
        f"链接：{post.url}"
    )


def can_manage_group(
    user_id: str,
    sender_role: str,
    superusers: FrozenSet[str],
) -> bool:
    return (
        str(user_id) in superusers
        or str(sender_role).casefold() in {"owner", "admin"}
    )


def format_status(
    config: XMonitorConfig,
    state: MonitorState,
    group_id: Optional[str] = None,
) -> str:
    keywords = "、".join(config.keywords)
    if group_id is None:
        group_status = "当前会话：非群聊"
    else:
        enabled = str(group_id) in state.enabled_group_ids
        group_status = f"当前群：{'已开启' if enabled else '未开启'}"
    last_success = (
        state.last_success_at.astimezone(BEIJING_TIMEZONE).strftime(
            "%Y-%m-%d %H:%M"
        )
        if state.last_success_at
        else "尚无"
    )
    last_error = state.last_error or "无"
    last_warning = state.last_warning or "无"
    return (
        "📡 Tibo X 监控状态\n"
        f"账号：@{config.username}\n"
        f"关键词：{keywords}\n"
        f"间隔：{config.poll_minutes} 分钟\n"
        f"{group_status}\n"
        f"已启用群数：{len(state.enabled_group_ids)}\n"
        f"最近成功：{last_success}\n"
        f"最近错误：{last_error}\n"
        f"最近警告：{last_warning}"
    )


class JsonStateStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> MonitorState:
        if not self.path.exists():
            return MonitorState()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return _state_from_dict(raw)
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise StateLoadError(
                f"无法安全读取 {self.path}: {error}"
            ) from error

    def save(self, state: MonitorState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Optional[Path] = None
        try:
            file_descriptor, raw_path = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=str(self.path.parent),
            )
            temporary_path = Path(raw_path)
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as file:
                json.dump(
                    _state_to_dict(state),
                    file,
                    ensure_ascii=False,
                    indent=2,
                )
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, self.path)
            temporary_path = None
        finally:
            if temporary_path and temporary_path.exists():
                temporary_path.unlink()


class XMonitorService:
    def __init__(
        self,
        config: XMonitorConfig,
        state_store: JsonStateStore,
        fetcher: Any,
        send_group_message: Callable[[str, str], Awaitable[None]],
    ):
        self.config = config
        self.state_store = state_store
        self.fetcher = fetcher
        self.send_group_message = send_group_message

    def get_state(self) -> MonitorState:
        return self.state_store.load()

    def enable_group(
        self,
        group_id: str,
        now: Optional[datetime] = None,
    ) -> bool:
        current_time = now or utc_now()
        state = self.state_store.load()
        updated = enable_group(
            prune_state(
                state,
                current_time,
                self.config.lookback_hours,
            ),
            group_id,
            current_time,
            self.config.lookback_hours,
        )
        self.state_store.save(updated)
        return updated != state

    def disable_group(self, group_id: str) -> bool:
        state = self.state_store.load()
        updated = disable_group(state, group_id)
        self.state_store.save(updated)
        return updated != state

    async def poll(
        self,
        now: Optional[datetime] = None,
    ) -> PollReport:
        current_time = now or utc_now()
        original_state = self.state_store.load()
        state = prune_state(
            original_state,
            current_time,
            self.config.lookback_hours,
        )
        if not state.enabled_group_ids:
            if state != original_state:
                self.state_store.save(state)
            return PollReport(skipped=True)

        try:
            batch = await self.fetcher.fetch_recent_posts(
                frozenset(post.id for post in state.posts),
                current_time,
                self.config.lookback_hours,
            )
        except Exception as error:
            error_text = (
                f"抓取 X 失败: {error.__class__.__name__}: "
                f"{str(error).splitlines()[0]}"
            )
            self.state_store.save(replace(state, last_error=error_text))
            return PollReport(failed=1, error=error_text)

        updated, new_count = add_posts(state, batch.posts)
        matching_count = sum(
            1 for post in batch.posts if post.matched_keywords
        )
        fetch_error = "; ".join(batch.errors) or None
        updated = replace(
            updated,
            last_success_at=(
                current_time if not batch.errors else updated.last_success_at
            ),
            last_error=fetch_error,
            last_warning=batch.coverage_warning,
        )
        self.state_store.save(updated)

        sent = 0
        failed = len(batch.errors)
        delivery_errors = []
        posts_by_id = {post.id: post for post in updated.posts}
        deliveries = tuple(
            sorted(
                updated.pending,
                key=lambda item: _pending_sort_key(item, posts_by_id),
            )
        )
        for delivery in deliveries:
            post = posts_by_id.get(delivery.post_id)
            if post is None:
                continue
            try:
                await self.send_group_message(
                    delivery.group_id,
                    format_notification(post),
                )
            except Exception as error:
                failed += 1
                delivery_errors.append(
                    f"发送群 {delivery.group_id} 的帖子 "
                    f"{delivery.post_id} 失败: {error}"
                )
                continue
            updated = mark_delivery_succeeded(updated, delivery)
            self.state_store.save(updated)
            sent += 1

        all_errors = tuple(batch.errors) + tuple(delivery_errors)
        final_error = "; ".join(all_errors) or None
        if updated.last_error != final_error:
            updated = replace(updated, last_error=final_error)
            self.state_store.save(updated)
        return PollReport(
            discovered=len(batch.discovered_ids),
            new_posts=new_count,
            matched=matching_count,
            sent=sent,
            failed=failed,
            coverage_warning=batch.coverage_warning,
            error=final_error,
        )


def load_monitor_config(path: Path) -> XMonitorConfig:
    if not path.exists():
        return XMonitorConfig()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"无法读取 X 监控配置 {path}: {error}") from error
    if not isinstance(raw, dict):
        raise ValueError("X 监控配置必须是 JSON 对象")

    username = str(_config_value(raw, "USERNAME", "thsottiaux")).strip()
    raw_keywords = _config_value(raw, "KEYWORDS", ["reset"])
    proxy_url = str(
        _config_value(
            raw,
            "PROXY_URL",
            "http://127.0.0.1:7890",
        )
    ).strip()
    poll_minutes = _positive_int(
        _config_value(raw, "POLL_MINUTES", 10),
        "POLL_MINUTES",
    )
    lookback_hours = _positive_int(
        _config_value(raw, "LOOKBACK_HOURS", 24),
        "LOOKBACK_HOURS",
    )
    page_timeout_seconds = _positive_float(
        _config_value(raw, "PAGE_TIMEOUT_SECONDS", 30),
        "PAGE_TIMEOUT_SECONDS",
    )

    if (
        not username
        or len(username) > 15
        or not all(character.isalnum() or character == "_" for character in username)
    ):
        raise ValueError("USERNAME 必须是合法的 X 用户名")
    if not isinstance(raw_keywords, list):
        raise ValueError("KEYWORDS 必须是字符串数组")
    if not all(isinstance(keyword, str) for keyword in raw_keywords):
        raise ValueError("KEYWORDS 必须只包含字符串")
    keywords = tuple(
        keyword.strip()
        for keyword in raw_keywords
        if keyword.strip()
    )
    if not keywords:
        raise ValueError("KEYWORDS 至少需要一个非空关键词")
    if proxy_url and not proxy_url.startswith(
        ("http://", "https://", "socks5://")
    ):
        raise ValueError("PROXY_URL 必须使用 http、https 或 socks5")
    return XMonitorConfig(
        username=username,
        keywords=keywords,
        proxy_url=proxy_url,
        poll_minutes=poll_minutes,
        lookback_hours=lookback_hours,
        page_timeout_seconds=page_timeout_seconds,
    )


def _config_value(
    raw: Mapping[str, Any],
    key: str,
    default: Any,
) -> Any:
    if key in raw:
        return raw[key]
    return raw.get(key.lower(), default)


def _positive_int(value: Any, key: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{key} 必须是正整数") from error
    if parsed <= 0:
        raise ValueError(f"{key} 必须是正整数")
    return parsed


def _positive_float(value: Any, key: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{key} 必须是正数") from error
    if parsed <= 0:
        raise ValueError(f"{key} 必须是正数")
    return parsed


def _validate_group_id(group_id: str) -> str:
    normalized = str(group_id).strip()
    if not normalized.isdigit():
        raise ValueError("QQ群号必须是数字")
    return normalized


def _state_to_dict(state: MonitorState) -> Dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "enabled_group_ids": list(state.enabled_group_ids),
        "posts": [
            {
                "id": post.id,
                "created_at": post.created_at.isoformat(),
                "text": post.text,
                "url": post.url,
                "matched_keywords": list(post.matched_keywords),
            }
            for post in state.posts
        ],
        "pending": [
            {"group_id": item.group_id, "post_id": item.post_id}
            for item in state.pending
        ],
        "delivered": [
            {"group_id": item.group_id, "post_id": item.post_id}
            for item in state.delivered
        ],
        "last_success_at": (
            state.last_success_at.isoformat()
            if state.last_success_at
            else None
        ),
        "last_error": state.last_error,
        "last_warning": state.last_warning,
    }


def _state_from_dict(raw: Any) -> MonitorState:
    if not isinstance(raw, dict):
        raise ValueError("状态根节点必须是 JSON 对象")
    if raw.get("version") != STATE_VERSION:
        raise ValueError("不支持的状态版本")

    enabled_raw = _required_list(raw, "enabled_group_ids")
    posts_raw = _required_list(raw, "posts")
    pending_raw = _required_list(raw, "pending")
    delivered_raw = _required_list(raw, "delivered")

    enabled_group_ids = tuple(
        _validate_group_id(group_id) for group_id in enabled_raw
    )
    posts = tuple(_post_from_dict(item) for item in posts_raw)
    post_ids = {post.id for post in posts}
    if len(post_ids) != len(posts):
        raise ValueError("状态包含重复帖子 ID")
    pending = tuple(
        _pending_from_dict(item, PendingDelivery) for item in pending_raw
    )
    delivered = tuple(
        _pending_from_dict(item, DeliveryRecord) for item in delivered_raw
    )
    pending_pairs = {
        (item.group_id, item.post_id) for item in pending
    }
    delivered_pairs = {
        (item.group_id, item.post_id) for item in delivered
    }
    if len(pending_pairs) != len(pending):
        raise ValueError("状态包含重复待投递记录")
    if len(delivered_pairs) != len(delivered):
        raise ValueError("状态包含重复已投递记录")
    if pending_pairs & delivered_pairs:
        raise ValueError("同一帖子不能同时处于待投递和已投递状态")
    for item in pending + delivered:
        if item.post_id not in post_ids:
            raise ValueError("投递记录引用了不存在的帖子")

    return MonitorState(
        enabled_group_ids=enabled_group_ids,
        posts=posts,
        pending=pending,
        delivered=delivered,
        last_success_at=_optional_datetime(
            raw.get("last_success_at"),
            "last_success_at",
        ),
        last_error=_optional_string(raw.get("last_error"), "last_error"),
        last_warning=_optional_string(
            raw.get("last_warning"),
            "last_warning",
        ),
    )


def _required_list(raw: Mapping[str, Any], key: str) -> Sequence[Any]:
    value = raw.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{key} 必须是数组")
    return value


def _post_from_dict(raw: Any) -> XPost:
    if not isinstance(raw, dict):
        raise ValueError("帖子状态必须是 JSON 对象")
    post_id = str(raw.get("id", ""))
    if not post_id.isdigit():
        raise ValueError("帖子 ID 必须是数字")
    text = raw.get("text")
    url = raw.get("url")
    keywords = raw.get("matched_keywords")
    if not isinstance(text, str) or not isinstance(url, str):
        raise ValueError("帖子正文和 URL 必须是字符串")
    if not isinstance(keywords, list) or not all(
        isinstance(keyword, str) for keyword in keywords
    ):
        raise ValueError("matched_keywords 必须是字符串数组")
    return XPost(
        id=post_id,
        created_at=_required_datetime(raw.get("created_at"), "created_at"),
        text=text,
        url=url,
        matched_keywords=tuple(keywords),
    )


def _pending_from_dict(raw: Any, record_type: Any) -> Any:
    if not isinstance(raw, dict):
        raise ValueError("投递状态必须是 JSON 对象")
    group_id = _validate_group_id(raw.get("group_id", ""))
    post_id = str(raw.get("post_id", ""))
    if not post_id.isdigit():
        raise ValueError("投递帖子 ID 必须是数字")
    return record_type(group_id=group_id, post_id=post_id)


def _required_datetime(value: Any, key: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{key} 必须是 ISO 时间字符串")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{key} 不是合法 ISO 时间") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{key} 必须包含时区")
    return parsed.astimezone(timezone.utc)


def _optional_datetime(
    value: Any,
    key: str,
) -> Optional[datetime]:
    if value is None:
        return None
    return _required_datetime(value, key)


def _optional_string(value: Any, key: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} 必须是字符串或 null")
    return value
