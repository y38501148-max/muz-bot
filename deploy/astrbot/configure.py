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
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
PROXY_SCHEMES = frozenset({"http", "https", "socks5", "socks5h"})


def normalize_openai_base_url(value: object) -> str:
    """Turn a host or full OpenAI endpoint into an SDK-compatible base URL."""
    raw_url = str(value or "").strip()
    parsed = urlparse(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("api_base 不是 HTTP(S) 地址")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("api_base 不允许内嵌凭据")
    if parsed.scheme == "http" and parsed.hostname not in LOOPBACK_HOSTS:
        raise ValueError("外部 api_base 必须使用 HTTPS")

    path = parsed.path.rstrip("/")
    for endpoint in ("/chat/completions", "/responses"):
        if path.endswith(endpoint):
            path = path[: -len(endpoint)].rstrip("/")
            break
    if not path.endswith("/v1"):
        path = f"{path}/v1" if path else "/v1"
    return parsed._replace(
        path=path,
        params="",
        query="",
        fragment="",
    ).geturl()


def normalize_proxy_url(value: object) -> str:
    """Validate an optional HTTP(S) or SOCKS proxy URL."""
    proxy_url = str(value or "").strip()
    if not proxy_url:
        return ""
    try:
        parsed = urlparse(proxy_url)
        proxy_port = parsed.port
    except ValueError as error:
        raise ValueError("proxy 包含无效端口或主机") from error
    proxy_hostname = parsed.hostname or ""
    if (
        parsed.scheme.casefold() not in PROXY_SCHEMES
        or not parsed.netloc
        or not proxy_hostname
        or any(character.isspace() for character in proxy_hostname)
    ):
        raise ValueError("proxy 必须是合法的 HTTP(S) 或 SOCKS5 地址")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("proxy 不允许内嵌凭据，避免 AstrBot 日志泄漏")
    if proxy_port is not None and not 1 <= proxy_port <= 65535:
        raise ValueError("proxy 端口必须在 1 到 65535 之间")
    if parsed.query or parsed.fragment:
        raise ValueError("proxy 不允许包含查询参数或片段")
    if parsed.path not in {"", "/"}:
        raise ValueError("proxy 不允许包含路径")
    return parsed._replace(
        scheme=parsed.scheme.casefold(),
        path="",
        params="",
        query="",
        fragment="",
    ).geturl()


def _validate_provider_specs(provider_specs: object) -> List[Dict]:
    if not isinstance(provider_specs, list) or len(provider_specs) != 3:
        raise ValueError("必须提供恰好三个 API Provider 配置")
    normalized = []
    identifiers = set()
    for index, raw in enumerate(provider_specs, start=1):
        if not isinstance(raw, dict):
            raise TypeError(f"第 {index} 个 Provider 必须是 JSON 对象")
        identifier = str(raw.get("id") or "").strip()
        api_base = normalize_openai_base_url(raw.get("api_base"))
        model = str(raw.get("model") or "").strip()
        key_env = str(raw.get("key_env") or "").strip()
        reasoning_effort = str(raw.get("reasoning_effort") or "").strip().casefold()
        proxy = normalize_proxy_url(raw.get("proxy"))
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
        if reasoning_effort and not re.fullmatch(
            r"[a-z][a-z0-9_-]{0,31}",
            reasoning_effort,
        ):
            raise ValueError(f"第 {index} 个 reasoning_effort 格式无效")
        identifiers.add(identifier)
        normalized.append(
            {
                "id": identifier,
                "api_base": api_base,
                "model": model,
                "key_env": key_env,
                "reasoning_effort": reasoning_effort,
                "proxy": proxy,
            }
        )
    if tuple(item["id"] for item in normalized) != MANAGED_PROVIDER_IDS:
        raise ValueError(
            "三个 Provider id 必须依次为 muz-primary、muz-secondary、muz-tertiary"
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
        "proxy": spec["proxy"],
        "custom_headers": {},
        "custom_extra_body": (
            {"reasoning_effort": spec["reasoning_effort"]}
            if spec["reasoning_effort"]
            else {}
        ),
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
        if not (isinstance(provider, dict) and (provider.get("id") in managed_ids))
    ]
    provider_settings = current.get("provider_settings", {})
    if not isinstance(provider_settings, dict):
        raise TypeError("cmd_config.json 的 provider_settings 必须是对象")
    proactive_capability = provider_settings.get("proactive_capability", {})
    if not isinstance(proactive_capability, dict):
        proactive_capability = {}
    file_extract = provider_settings.get("file_extract", {})
    if not isinstance(file_extract, dict):
        file_extract = {}
    subagent_orchestrator = current.get("subagent_orchestrator", {})
    if not isinstance(subagent_orchestrator, dict):
        subagent_orchestrator = {}

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
        "web_search": False,
        "max_agent_step": 1,
        "tool_call_timeout": 30,
        "computer_use_runtime": "none",
        "proactive_capability": {
            **proactive_capability,
            "add_cron_tools": False,
        },
        "file_extract": {
            **file_extract,
            "enable": False,
        },
    }
    return {
        **current,
        "provider": [
            *preserved_providers,
            *[_provider_config(spec) for spec in specs],
        ],
        "provider_settings": updated_settings,
        "subagent_orchestrator": {
            **subagent_orchestrator,
            "main_enable": False,
            "agents": [],
        },
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
    current = json.loads(config_path.read_text(encoding="utf-8-sig"))
    provider_specs = json.loads(providers_path.read_text(encoding="utf-8-sig"))
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
