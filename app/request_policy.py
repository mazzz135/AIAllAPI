from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any

from .config import BASE_DIR

log = logging.getLogger("aiall.request_policy")

DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."
FINGERPRINT_KEYS = {
    "device_id",
    "deviceid",
    "machine_id",
    "machineid",
    "machine_token",
    "machinetoken",
    "machine_type",
    "machinetype",
    "machine_code",
    "machinecode",
    "machine_hostname",
    "machinehostname",
    "machine_os",
    "machineos",
    "fingerprint",
    "client_fingerprint",
    "x_device_id",
    "x_machine_id",
    "x-device-id",
    "x-machine-id",
}


def _setting(db, key: str, default: str = "") -> str:
    try:
        return str(db.get_setting(key, default) or default)
    except Exception:
        return default


def _prompt_text(db) -> str:
    path = _setting(db, "prompt.file", "").strip()
    if not path:
        return DEFAULT_SYSTEM_PROMPT
    file_path = Path(path)
    if not file_path.is_absolute():
        file_path = BASE_DIR / file_path
    try:
        text = file_path.read_text(encoding="utf-8").strip()
        if text:
            return text[:200_000]
        log.warning("提示词文件为空: %s", file_path)
    except Exception as exc:
        log.warning("读取提示词文件失败 %s: %s", file_path, exc)
    return DEFAULT_SYSTEM_PROMPT


def _is_system(message: Any) -> bool:
    return isinstance(message, dict) and str(message.get("role") or "").lower() in ("system", "developer")


def _system_message(text: str) -> dict[str, Any]:
    return {"role": "system", "content": text}


def apply_prompt(payload: dict[str, Any], db) -> dict[str, Any]:
    mode = _setting(db, "prompt.mode", "passthrough").strip().lower() or "passthrough"
    if mode not in ("custom", "append"):
        return payload
    body = copy.deepcopy(payload)
    messages = body.get("messages")
    if not isinstance(messages, list):
        messages = []
    prompt = _prompt_text(db)
    if mode == "custom":
        messages = [message for message in messages if not _is_system(message)]
        messages.insert(0, _system_message(prompt))
    else:
        index = 0
        while index < len(messages) and _is_system(messages[index]):
            index += 1
        messages.insert(index, _system_message(prompt))
    body["messages"] = messages
    return body


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if str(key).lower() in FINGERPRINT_KEYS:
                continue
            result[key] = _clean(item)
        return result
    if isinstance(value, list):
        return [_clean(item) for item in value]
    return value


def sanitize_fingerprint(payload: dict[str, Any], db) -> dict[str, Any]:
    enabled = _setting(db, "privacy.sanitize_fingerprint", "1").strip().lower()
    if enabled not in ("1", "true", "yes", "on"):
        return payload
    return _clean(payload)


def inject_deepseek_thinking(payload: dict[str, Any]) -> dict[str, Any]:
    model = str(payload.get("model") or "").lower()
    if "deepseek" not in model:
        return payload
    body = copy.deepcopy(payload)
    thinking = body.get("thinking")
    if not isinstance(thinking, dict):
        thinking = {}
    if not thinking.get("type"):
        thinking["type"] = "enabled"
    body["thinking"] = thinking
    if not body.get("reasoning_effort"):
        body["reasoning_effort"] = "high"
    messages = body.get("messages")
    if isinstance(messages, list):
        has_trace = any(
            isinstance(message, dict)
            and str(message.get("role") or "").lower() == "assistant"
            and (message.get("reasoning") or message.get("reasoning_content"))
            for message in messages
        )
        if str(thinking.get("type")).lower() == "enabled" or has_trace:
            for message in messages:
                if not isinstance(message, dict) or str(message.get("role") or "").lower() != "assistant":
                    continue
                if not isinstance(message.get("reasoning_content"), str):
                    message["reasoning_content"] = str(message.get("reasoning") or "")
    return body


def apply_request_policy(payload: dict[str, Any], db) -> dict[str, Any]:
    body = inject_deepseek_thinking(payload)
    body = apply_prompt(body, db)
    return sanitize_fingerprint(body, db)
