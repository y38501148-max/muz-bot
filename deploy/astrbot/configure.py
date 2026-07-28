# ruff: noqa: UP006 -- this script may be run by muz-bot's Python 3.8.

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Dict, List
from urllib.parse import urlparse

TECHNICAL_CONTEXT_WINDOW = 60_975
EFFECTIVE_CONTEXT_LIMIT = 50_000
MANAGED_PROVIDER_IDS = (
    "muz-primary",
    "muz-secondary",
    "muz-tertiary",
)
ENV_NAME_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def _validate_provider_specs(provider_specs: object) -> List[Dict]:
    if not isinstance(provider_specs, list) or len(provider_specs) != 3:
        raise ValueError("必须提供恰好三个 API Provider 配置")
    normalized = []
    identifiers = set()
    for index, raw in enumerate(provider_specs, start=1):
        if not isinstance(raw, dict):
            raise TypeError(f"第 {index} 个 Provider 必须是 JSON 对象")
        identifier = str(raw.get("id") or "").strip()
        api_base = str(raw.get("api_base") or "").strip().rstrip("/")
        model = str(raw.get("model") or "").strip()
        key_env = str(raw.get("key_env") or "").strip()
        parsed_url = urlparse(api_base)
        if not identifier or not model:
            raise ValueError(f"第 {index} 个 Provider 缺少 id 或 model")
        if identifier in identifiers:
            raise ValueError("三个 Provider 的 id 必须唯一")
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError(f"第 {index} 个 api_base 不是 HTTP(S) 地址")
        if parsed_url.username is not None or parsed_url.password is not None:
            raise ValueError(f"第 {index} 个 api_base 不允许内嵌凭据")
        if not ENV_NAME_PATTERN.fullmatch(key_env):
            raise ValueError(f"第 {index} 个 key_env 不是合法环境变量名")
        identifiers.add(identifier)
        normalized.append(
            {
                "id": identifier,
                "api_base": api_base,
                "model": model,
                "key_env": key_env,
            }
        )
    if tuple(item["id"] for item in normalized) != MANAGED_PROVIDER_IDS:
        raise ValueError(
            "三个 Provider id 必须依次为 "
            "muz-primary、muz-secondary、muz-tertiary"
        )
    return normalized


def _provider_config(spec: Dict) -> Dict:
    return {
        "id": spec["id"],
        "provider": "openai",
        "type": "openai_chat_completion",
        "provider_type": "chat_completion",
        "enable": True,
        "key": [f"${spec['key_env']}"],
        "api_base": spec["api_base"],
        "model": spec["model"],
        "timeout": 120,
        "proxy": "",
        "custom_headers": {},
        # AstrBot's built-in local compressor triggers at 82%. This technical
        # window makes its trigger 49,999.5 tokens. The gateway plugin also
        # enforces the explicit 50k cap before the runner is reset.
        "max_context_tokens": TECHNICAL_CONTEXT_WINDOW,
    }


def build_astrbot_config(
    current: object,
    provider_specs: object,
) -> Dict:
    if not isinstance(current, dict):
        raise TypeError("cmd_config.json 顶层必须是 JSON 对象")
    specs = _validate_provider_specs(provider_specs)
    existing_providers = current.get("provider", [])
    if not isinstance(existing_providers, list):
        raise TypeError("cmd_config.json 的 provider 必须是数组")
    managed_ids = set(MANAGED_PROVIDER_IDS)
    preserved_providers = [
        provider
        for provider in existing_providers
        if not (
            isinstance(provider, dict)
            and (
                provider.get("id") in managed_ids
            )
        )
    ]
    provider_settings = current.get("provider_settings", {})
    if not isinstance(provider_settings, dict):
        raise TypeError("cmd_config.json 的 provider_settings 必须是对象")

    updated_settings = {
        **provider_settings,
        "enable": True,
        "default_provider_id": specs[0]["id"],
        "fallback_chat_models": [
            specs[1]["id"],
            specs[2]["id"],
        ],
        "request_max_retries": 1,
        "context_limit_reached_strategy": "truncate_by_turns",
        "max_context_length": -1,
        "dequeue_context_length": 8,
        "fallback_max_context_tokens": TECHNICAL_CONTEXT_WINDOW,
    }
    return {
        **current,
        "provider": [
            *preserved_providers,
            *[_provider_config(spec) for spec in specs],
        ],
        "provider_settings": updated_settings,
    }


def _atomic_write_json(path: Path, value: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                value,
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def configure(config_path: Path, providers_path: Path) -> None:
    current = json.loads(config_path.read_text(encoding="utf-8"))
    provider_specs = json.loads(providers_path.read_text(encoding="utf-8"))
    updated = build_astrbot_config(current, provider_specs)
    _atomic_write_json(config_path, updated)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="配置 AstrBot 三 Provider 降级链和 50k 本地上下文限制",
    )
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="AstrBot data/cmd_config.json 路径",
    )
    parser.add_argument(
        "--providers",
        type=Path,
        required=True,
        help="三个 Provider 的私有 JSON 配置路径",
    )
    arguments = parser.parse_args()
    configure(arguments.config, arguments.providers)
    print(
        "AstrBot Provider 已配置：primary -> secondary -> tertiary；"
        f"有效本地上下文上限 {EFFECTIVE_CONTEXT_LIMIT} tokens。"
    )


if __name__ == "__main__":
    main()
