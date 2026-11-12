from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ProviderError(Exception):
    def __init__(self, message: str, *, status_code: int = 0, retryable: bool = True):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.retryable = retryable


@dataclass
class StreamChunk:
    line: str
    usage: dict[str, int] = field(default_factory=dict)


def normalize_base_url(url: str, default: str = "") -> str:
    base = (url or default).strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = default.rstrip("/")
    if base and not base.endswith("/v1"):
        base += "/v1"
    return base


def empty_usage() -> dict[str, int]:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cache_miss_tokens": 0,
        "reasoning_tokens": 0,
    }


def merge_usage(target: dict[str, int], source: Any) -> None:
    if not isinstance(source, dict):
        return
    prompt = int(source.get("prompt_tokens") or source.get("input_tokens") or 0)
    completion = int(source.get("completion_tokens") or source.get("output_tokens") or 0)
    total = int(source.get("total_tokens") or 0) or prompt + completion
    if prompt:
        target["prompt_tokens"] = prompt
    if completion:
        target["completion_tokens"] = completion
    if total:
        target["total_tokens"] = total
    details = source.get("prompt_tokens_details")
    if isinstance(details, dict):
        target["cached_tokens"] = int(details.get("cached_tokens") or target["cached_tokens"])
    read = int(
        target.get("cached_tokens")
        or source.get("cache_read_input_tokens")
        or source.get("cache_read_tokens")
        or 0
    )
    write = int(
        source.get("cache_creation_input_tokens")
        or source.get("cache_write_input_tokens")
        or source.get("cache_write_tokens")
        or 0
    )
    target["cache_read_tokens"] = read
    target["cache_write_tokens"] = write
    miss = int(source.get("cache_miss_tokens") or 0)
    if not miss:
        miss = max(0, int(target.get("prompt_tokens") or 0) - read - write)
    target["cache_miss_tokens"] = miss
    cdetails = source.get("completion_tokens_details")
    if isinstance(cdetails, dict):
        target["reasoning_tokens"] = int(cdetails.get("reasoning_tokens") or target["reasoning_tokens"])
