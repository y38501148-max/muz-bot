from __future__ import annotations

import json
import sys
from pathlib import Path

MAX_ADDRESS_SPACE_BYTES = 512 * 1024 * 1024
MAX_CPU_SECONDS = 10
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_OPEN_FILES = 32


def _set_limit(resource_module, name: str, value: int) -> None:
    limit = getattr(resource_module, name, None)
    if limit is None:
        return
    _, hard_limit = resource_module.getrlimit(limit)
    bounded = value if hard_limit < 0 else min(value, hard_limit)
    resource_module.setrlimit(limit, (bounded, bounded))


def _apply_resource_limits() -> None:
    try:
        import resource
    except ImportError:
        return
    # RLIMIT_AS is reliable for the Linux production container. macOS applies
    # it differently and can reject routine interpreter allocations in tests.
    if sys.platform.startswith("linux"):
        _set_limit(resource, "RLIMIT_AS", MAX_ADDRESS_SPACE_BYTES)
    _set_limit(resource, "RLIMIT_CPU", MAX_CPU_SECONDS)
    _set_limit(resource, "RLIMIT_FSIZE", MAX_FILE_BYTES)
    _set_limit(resource, "RLIMIT_NOFILE", MAX_OPEN_FILES)


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    _apply_resource_limits()
    try:
        from document_extract import extract_document_text

        text = extract_document_text(Path(sys.argv[1]), sys.argv[2])
        payload = {"ok": True, "text": text}
    except (OSError, ValueError):
        payload = {"ok": False}
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > 64 * 1024:
        return 3
    sys.stdout.buffer.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
