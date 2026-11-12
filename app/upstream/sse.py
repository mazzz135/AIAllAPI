from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass
class SSEFrame:
    event: str
    data: str


class SSEDecoder:
    """Minimal SSE decoder that works with httpx aiter_lines()."""

    def __init__(self) -> None:
        self.event = ""
        self.data: list[str] = []

    def reset(self) -> None:
        self.event = ""
        self.data = []

    def feed_line(self, line: str) -> SSEFrame | None:
        if line == "":
            if not self.event and not self.data:
                return None
            frame = SSEFrame(self.event or "message", "\n".join(self.data))
            self.reset()
            return frame
        if line.startswith(":"):
            return None
        if line.startswith("event:"):
            self.event = line[6:].strip()
            return None
        if line.startswith("data:"):
            self.data.append(line[5:].lstrip())
        return None

    def finish(self) -> SSEFrame | None:
        if not self.event and not self.data:
            return None
        frame = SSEFrame(self.event or "message", "\n".join(self.data))
        self.reset()
        return frame


def parse_json(data: str) -> dict[str, Any] | None:
    try:
        value = json.loads(data)
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def extract_usage(data: dict[str, Any]) -> dict[str, int]:
    """Trae usage events are sometimes nested and sometimes flat."""
    nested = data.get("usage")
    source = nested if isinstance(nested, dict) else data
    prompt = int(source.get("prompt_tokens") or source.get("input_tokens") or 0)
    completion = int(source.get("completion_tokens") or source.get("output_tokens") or 0)
    total = int(source.get("total_tokens") or 0) or prompt + completion
    details = source.get("prompt_tokens_details") or {}
    completion_details = source.get("completion_tokens_details") or {}
    cached = int(
        (details.get("cached_tokens") if isinstance(details, dict) else 0)
        or source.get("cache_read_input_tokens")
        or source.get("cache_read_token")
        or source.get("cache_read_tokens")
        or 0
    )
    cache_write = int(
        source.get("cache_creation_input_tokens")
        or source.get("cache_write_input_tokens")
        or source.get("cache_write_tokens")
        or 0
    )
    reasoning = int(
        (completion_details.get("reasoning_tokens") if isinstance(completion_details, dict) else 0)
        or source.get("reasoning_tokens")
        or 0
    )
    cache_miss = int(source.get("cache_miss_tokens") or 0)
    if not cache_miss:
        cache_miss = max(0, prompt - cached - cache_write)
    return {
        "prompt_tokens": max(0, prompt),
        "completion_tokens": max(0, completion),
        "total_tokens": max(0, total),
        "cached_tokens": max(0, cached),
        "cache_read_tokens": max(0, cached),
        "cache_write_tokens": max(0, cache_write),
        "cache_miss_tokens": max(0, cache_miss),
        "reasoning_tokens": max(0, reasoning),
    }
