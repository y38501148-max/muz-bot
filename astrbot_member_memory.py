# ruff: noqa: UP006, UP045 -- muz-bot still declares Python 3.8 support.

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

MEMORY_VERSION = 1
DEFAULT_RETENTION_SECONDS = 30 * 24 * 60 * 60
_SENSITIVE_PATTERNS = (
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    re.compile(r"(?<!\d)\d{15}(?:\d{2}[\dXx])?(?!\d)"),
    re.compile(r"(?<!\d)\d{13,19}(?!\d)"),
    re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"),
    re.compile(
        r"(?:(?:密码|口令|验证码|api[_ -]?key|access[_ -]?token|"
        r"secret|cookie|sessionid|authorization)\s*[:：=]\s*\S+|"
        r"(?<![A-Za-z0-9])(?:sk|rk|pk|ghp|github_pat|xox[baprs])"
        r"[-_][A-Za-z0-9_-]{8,})",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}|"
        r"Bearer\s+[A-Za-z0-9._~+/-]{16,}={0,2}|"
        r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|"
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:system\s*prompt|系统提示词|"
        r"(?:忽略|无视).{0,12}(?:规则|指令|提示词)|"
        r"调用.{0,8}(?:工具|函数))",
        re.IGNORECASE,
    ),
)


class MemberMemoryStore:
    """Incremental, bounded memories scoped by an HMAC of group and member."""

    def __init__(
        self,
        path: Path,
        *,
        key_path: Optional[Path] = None,
        max_members: int = 2_000,
        max_entries: int = 24,
        max_entry_chars: int = 240,
        prompt_entries: int = 10,
        prompt_chars: int = 1_800,
        retention_seconds: int = DEFAULT_RETENTION_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        limits = (
            max_members,
            max_entries,
            max_entry_chars,
            prompt_entries,
            prompt_chars,
            retention_seconds,
        )
        if any(limit < 1 for limit in limits):
            raise ValueError("成员记忆容量和保留期必须大于 0")
        self.path = path
        self.key_path = key_path or path.with_suffix(".key")
        self.max_members = max_members
        self.max_entries = max_entries
        self.max_entry_chars = max_entry_chars
        self.prompt_entries = min(prompt_entries, max_entries)
        self.prompt_chars = prompt_chars
        self.retention_seconds = retention_seconds
        self.clock = clock
        self._key_lock = threading.Lock()

    @staticmethod
    def _validate_id(value: object, label: str) -> str:
        normalized = str(value).strip()
        if not normalized.isdigit():
            raise ValueError(f"{label}必须是数字")
        return normalized

    @staticmethod
    def _display_name(value: object) -> str:
        normalized = " ".join(str(value or "匿名群友").split())
        return normalized[:80] or "匿名群友"

    def _sample(self, value: object) -> str:
        normalized = " ".join(str(value or "").split())
        if (
            not normalized
            or normalized.startswith("/")
            or any(pattern.search(normalized) for pattern in _SENSITIVE_PATTERNS)
        ):
            return ""
        return normalized[: self.max_entry_chars]

    def _load_or_create_key(self) -> bytes:
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        with self._key_lock:
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
                raise ValueError(f"成员记忆密钥不可用：{error}") from error
        if len(key) != 32:
            raise ValueError("成员记忆密钥长度无效")
        os.chmod(self.key_path, 0o600)
        return key

    def _member_key(self, group_id: object, user_id: object) -> str:
        group = self._validate_id(group_id, "群号")
        user = self._validate_id(user_id, "QQ 号")
        identity = f"{group}:{user}".encode()
        return hmac.new(
            self._load_or_create_key(),
            identity,
            hashlib.sha256,
        ).hexdigest()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        if not self.path.exists():
            try:
                descriptor = os.open(
                    str(self.path),
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                pass
            else:
                os.close(descriptor)
        os.chmod(self.path, 0o600)
        connection = sqlite3.connect(str(self.path), timeout=5)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA secure_delete=ON")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS members (
                    member_key TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    member_key TEXT NOT NULL,
                    sample TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(member_key) REFERENCES members(member_key)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_samples_member
                    ON samples(member_key, id DESC);
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO metadata(key, value) VALUES('version', ?)",
                (str(MEMORY_VERSION),),
            )
            version = connection.execute(
                "SELECT value FROM metadata WHERE key='version'"
            ).fetchone()
            if version != (str(MEMORY_VERSION),):
                raise ValueError("成员记忆数据库版本无效")
            connection.commit()
        except (sqlite3.Error, ValueError):
            connection.close()
            raise
        os.chmod(self.path, 0o600)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{self.path}{suffix}")
            if sidecar.exists():
                os.chmod(sidecar, 0o600)
        return connection

    def snapshot_and_remember(
        self,
        group_id: object,
        user_id: object,
        display_name: object,
        message: object,
    ) -> Tuple[str, List[str]]:
        """Return the prior snapshot, then record this eligible group message."""
        member_key = self._member_key(group_id, user_id)
        now = int(self.clock())
        cutoff = now - self.retention_seconds
        sample = self._sample(message)
        connection = None
        try:
            connection = self._connect()
            with connection:
                connection.execute(
                    "DELETE FROM samples WHERE created_at < ?",
                    (cutoff,),
                )
                connection.execute(
                    "DELETE FROM members WHERE member_key NOT IN "
                    "(SELECT DISTINCT member_key FROM samples)"
                )
                rows = connection.execute(
                    "SELECT sample FROM samples "
                    "WHERE member_key = ? ORDER BY id DESC LIMIT ?",
                    (member_key, self.prompt_entries),
                ).fetchall()
                prior_samples = self._clip_prompt_samples(
                    [str(row[0]) for row in reversed(rows)]
                )
                if sample:
                    latest = connection.execute(
                        "SELECT sample FROM samples WHERE member_key = ? "
                        "ORDER BY id DESC LIMIT 1",
                        (member_key,),
                    ).fetchone()
                    connection.execute(
                        "INSERT INTO members(member_key, display_name, updated_at) "
                        "VALUES(?, ?, ?) "
                        "ON CONFLICT(member_key) DO UPDATE SET "
                        "display_name=excluded.display_name, "
                        "updated_at=excluded.updated_at",
                        (member_key, self._display_name(display_name), now),
                    )
                    if latest != (sample,):
                        connection.execute(
                            "INSERT INTO samples(member_key, sample, created_at) "
                            "VALUES(?, ?, ?)",
                            (member_key, sample, now),
                        )
                        connection.execute(
                            "DELETE FROM samples WHERE member_key = ? AND id NOT IN "
                            "(SELECT id FROM samples WHERE member_key = ? "
                            "ORDER BY id DESC LIMIT ?)",
                            (member_key, member_key, self.max_entries),
                        )
                    self._evict_oldest_members(connection)
            return self._display_name(display_name), prior_samples
        except sqlite3.Error as error:
            raise ValueError(f"成员记忆数据库不可用：{error}") from error
        finally:
            if connection is not None:
                connection.close()

    def _clip_prompt_samples(self, samples: List[str]) -> List[str]:
        selected: List[str] = []
        selected_chars = 0
        for sample in reversed(samples):
            remaining = self.prompt_chars - selected_chars
            if remaining <= 0:
                break
            selected.append(sample[:remaining])
            selected_chars += len(selected[-1])
        return list(reversed(selected))

    def _evict_oldest_members(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            "DELETE FROM members WHERE member_key IN ("
            "SELECT member_key FROM members ORDER BY updated_at DESC, member_key "
            "LIMIT -1 OFFSET ?)",
            (self.max_members,),
        )

    def clear(self, group_id: object, user_id: object) -> bool:
        member_key = self._member_key(group_id, user_id)
        connection = None
        try:
            connection = self._connect()
            with connection:
                cursor = connection.execute(
                    "DELETE FROM members WHERE member_key = ?",
                    (member_key,),
                )
            if cursor.rowcount > 0:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return cursor.rowcount > 0
        except sqlite3.Error as error:
            raise ValueError(f"成员记忆数据库不可用：{error}") from error
        finally:
            if connection is not None:
                connection.close()

    def purge_expired(self) -> int:
        cutoff = int(self.clock()) - self.retention_seconds
        connection = None
        try:
            connection = self._connect()
            with connection:
                cursor = connection.execute(
                    "DELETE FROM samples WHERE created_at < ?",
                    (cutoff,),
                )
                connection.execute(
                    "DELETE FROM members WHERE member_key NOT IN "
                    "(SELECT DISTINCT member_key FROM samples)"
                )
            if cursor.rowcount > 0:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return max(cursor.rowcount, 0)
        except sqlite3.Error as error:
            raise ValueError(f"成员记忆数据库不可用：{error}") from error
        finally:
            if connection is not None:
                connection.close()
