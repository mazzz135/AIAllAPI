from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any


MAX_SANE_EXPIRY = 4102444800  # 2100-01-01


def to_epoch(value: Any) -> int:
    """Normalize seconds/milliseconds/ISO datetime to Unix seconds."""
    if value in (None, "", 0, "0"):
        return 0
    if isinstance(value, (int, float)):
        number = float(value)
        if number <= 0:
            return 0
        if number > 1_000_000_000_000:
            number /= 1000.0
        if number > MAX_SANE_EXPIRY:
            return 0
        return int(number)
    text = str(value).strip()
    if not text:
        return 0
    try:
        number = float(text)
        return to_epoch(number)
    except Exception:
        pass
    candidate = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S"):
        try:
            return int(datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).timestamp())
        except Exception:
            continue
    return 0


def make_pack(
    name: Any,
    remain: Any,
    total: Any = 0,
    used: Any = 0,
    expire_at: Any = 0,
    source: str = "quota",
    unit: str = "credits",
) -> dict[str, Any]:
    def number(value: Any) -> float:
        try:
            return max(0.0, float(value or 0))
        except Exception:
            return 0.0

    remain_value = number(remain)
    total_value = number(total)
    used_value = number(used)
    if total_value <= 0 and (remain_value or used_value):
        total_value = remain_value + used_value
    return {
        "name": str(name or "积分").strip()[:80] or "积分",
        "remain": round(remain_value, 6),
        "total": round(total_value, 6),
        "used": round(used_value, 6),
        "expire_at": to_epoch(expire_at),
        "source": str(source or "quota")[:32],
        "unit": str(unit or "credits")[:16],
    }


def normalize_packs(items: Any, now: int | None = None) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    current = int(now or time.time())
    result: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        pack = make_pack(
            item.get("name") or item.get("label") or item.get("package_name"),
            item.get("remain", item.get("remaining", item.get("creditsRemaining", 0))),
            item.get("total", item.get("limit", 0)),
            item.get("used", item.get("consumed", 0)),
            item.get("expire_at", item.get("expiresAt", item.get("expires_at", 0))),
            str(item.get("source") or "quota"),
            str(item.get("unit") or "credits"),
        )
        if pack["remain"] <= 0 and pack["total"] <= 0:
            continue
        if pack["expire_at"] and pack["expire_at"] <= current:
            continue
        result.append(pack)
    result.sort(
        key=lambda item: (
            float(item.get("remain") or 0) <= 0,
            int(item.get("expire_at") or 0) == 0,
            int(item.get("expire_at") or 0),
            -float(item.get("remain") or 0),
            str(item.get("name") or ""),
        )
    )
    return result


def dumps(packs: Any) -> str:
    return json.dumps(normalize_packs(packs), ensure_ascii=False, separators=(",", ":"))


def loads(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return normalize_packs(raw)
    if not raw:
        return []
    try:
        return normalize_packs(json.loads(str(raw)))
    except Exception:
        return []


def totals(packs: Any) -> dict[str, float]:
    items = normalize_packs(packs)
    return {
        "remain": round(sum(float(p.get("remain") or 0) for p in items), 6),
        "total": round(sum(float(p.get("total") or 0) for p in items), 6),
        "used": round(sum(float(p.get("used") or 0) for p in items), 6),
    }


def unit_of(packs: Any) -> str:
    """推断一组积分包的展示单位：含美元包则按 USD 展示。"""
    items = packs if isinstance(packs, list) else loads(packs)
    for pack in items:
        if isinstance(pack, dict) and str(pack.get("unit") or "") == "USD":
            return "USD"
    return "credits"


def expiring(packs: Any, days: int = 7, now: int | None = None) -> float:
    current = int(now or time.time())
    horizon = current + max(0, int(days)) * 86400
    return round(
        sum(
            float(p.get("remain") or 0)
            for p in normalize_packs(packs)
            if p.get("expire_at") and current < int(p["expire_at"]) <= horizon
        ),
        6,
    )
