# ruff: noqa: UP006, UP045 -- keep the pure helper importable on Python 3.8.

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

IMAGE_TOKEN_ESTIMATE = 765
AUDIO_TOKEN_ESTIMATE = 500


@dataclass(frozen=True)
class CompactResult:
    contexts: List[Dict]
    prompt: str
    system_prompt: str
    compacted: bool
    estimated_tokens: int


def _estimate_text(text: str) -> int:
    chinese_count = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    other_count = len(text) - chinese_count
    return int(chinese_count * 0.6 + other_count * 0.3)


def _estimate_content(content: object) -> int:
    if isinstance(content, str):
        return _estimate_text(content)
    if not isinstance(content, list):
        return 0
    total = 0
    for part in content:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type == "text":
            total += _estimate_text(str(part.get("text") or ""))
        elif part_type == "think":
            total += _estimate_text(str(part.get("think") or ""))
        elif part_type == "image_url":
            total += IMAGE_TOKEN_ESTIMATE
        elif part_type == "audio_url":
            total += AUDIO_TOKEN_ESTIMATE
    return total


def estimate_request_tokens(
    contexts: Sequence[Dict],
    *,
    prompt: str,
    system_prompt: str,
) -> int:
    total = _estimate_text(prompt) + _estimate_text(system_prompt)
    for message in contexts:
        total += _estimate_content(message.get("content"))
        tool_calls = message.get("tool_calls")
        if tool_calls:
            total += _estimate_text(
                json.dumps(tool_calls, ensure_ascii=False, default=str)
            )
    return total


def _split_rounds(contexts: Sequence[Dict]) -> List[List[Dict]]:
    rounds: List[List[Dict]] = []
    current: List[Dict] = []
    for message in contexts:
        copied = copy.deepcopy(message)
        if copied.get("role") == "user" and current:
            rounds.append(current)
            current = []
        current.append(copied)
    if current:
        rounds.append(current)
    return rounds


def _flatten(rounds: Sequence[Sequence[Dict]]) -> List[Dict]:
    return [
        copy.deepcopy(message)
        for round_messages in rounds
        for message in round_messages
    ]


def _trim_to_token_budget(
    text: str,
    budget: int,
    *,
    keep_suffix: bool,
) -> str:
    if budget <= 0 or not text:
        return ""
    if _estimate_text(text) <= budget:
        return text
    low = 0
    high = len(text)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = text[-middle:] if keep_suffix else text[:middle]
        if _estimate_text(candidate) <= budget:
            low = middle
        else:
            high = middle - 1
    return text[-low:] if keep_suffix and low else text[:low]


def compact_request(
    contexts: Sequence[Dict],
    *,
    prompt: Optional[str],
    system_prompt: Optional[str],
    max_tokens: int,
    target_tokens: int,
) -> CompactResult:
    if max_tokens <= 0:
        raise ValueError("max_tokens 必须大于 0")
    if target_tokens <= 0 or target_tokens > max_tokens:
        raise ValueError("target_tokens 必须在 1 到 max_tokens 之间")

    normalized_prompt = str(prompt or "")
    normalized_system = str(system_prompt or "")
    copied_contexts = [copy.deepcopy(message) for message in contexts]
    initial_tokens = estimate_request_tokens(
        copied_contexts,
        prompt=normalized_prompt,
        system_prompt=normalized_system,
    )
    if initial_tokens <= max_tokens:
        return CompactResult(
            contexts=copied_contexts,
            prompt=normalized_prompt,
            system_prompt=normalized_system,
            compacted=False,
            estimated_tokens=initial_tokens,
        )

    compacted_system = _trim_to_token_budget(
        normalized_system,
        target_tokens,
        keep_suffix=False,
    )
    rounds = _split_rounds(copied_contexts)
    while len(rounds) > 1:
        candidate_rounds = rounds[1:]
        candidate_contexts = _flatten(candidate_rounds)
        candidate_tokens = estimate_request_tokens(
            candidate_contexts,
            prompt=normalized_prompt,
            system_prompt=compacted_system,
        )
        rounds = candidate_rounds
        if candidate_tokens <= target_tokens:
            break

    compacted_contexts = _flatten(rounds)
    fixed_tokens = estimate_request_tokens(
        compacted_contexts,
        prompt="",
        system_prompt=compacted_system,
    )
    prompt_budget = max(0, target_tokens - fixed_tokens)
    compacted_prompt = _trim_to_token_budget(
        normalized_prompt,
        prompt_budget,
        keep_suffix=True,
    )
    estimated_tokens = estimate_request_tokens(
        compacted_contexts,
        prompt=compacted_prompt,
        system_prompt=compacted_system,
    )

    if estimated_tokens > target_tokens and compacted_contexts:
        compacted_contexts = []
        fixed_tokens = _estimate_text(compacted_system)
        compacted_prompt = _trim_to_token_budget(
            normalized_prompt,
            max(0, target_tokens - fixed_tokens),
            keep_suffix=True,
        )
        estimated_tokens = estimate_request_tokens(
            compacted_contexts,
            prompt=compacted_prompt,
            system_prompt=compacted_system,
        )

    if estimated_tokens > target_tokens:
        compacted_prompt = ""
        estimated_tokens = _estimate_text(compacted_system)

    return CompactResult(
        contexts=compacted_contexts,
        prompt=compacted_prompt,
        system_prompt=compacted_system,
        compacted=True,
        estimated_tokens=estimated_tokens,
    )
