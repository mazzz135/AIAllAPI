from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Optional

from ..config import Settings
from ..credit_packs import loads as packs_loads
from ..db import Database
from ..security import SecretBox
from ..providers.anthropic_compat import AnthropicCompatProvider
from ..providers.base import ProviderError, normalize_base_url
from ..providers.openai_compat import OpenAICompatProvider
from ..providers.platforms import adapter_for

BUILTIN_IDS = {
    "trae-cn",
    "trae-global",
    "workbuddy",
    "codebuddy",
    "lobster",
    "qoder-cn",
    "qoder-global",
}


def _now() -> int:
    return int(time.time())


def _split_models(text: str) -> list[str]:
    import re

    return [x for x in re.split(r"[,;\s]+", text or "") if x]


class ProviderService:
    def __init__(self, settings: Settings, db: Database, box: SecretBox, http):
        self.settings = settings
        self.db = db
        self.box = box
        self.http = http
        self.openai = OpenAICompatProvider(settings, http)
        self.anthropic = AnthropicCompatProvider(settings, http)
        self._rr_index: dict[str, int] = {}

    # ------------------------------------------------------------------ providers
    def _row_provider(self, row) -> dict[str, Any]:
        item = dict(row)
        try:
            item["models"] = json.loads(item.get("models_json") or "[]")
        except Exception:
            item["models"] = []
        try:
            item["config"] = json.loads(item.get("config_json") or "{}")
        except Exception:
            item["config"] = {}
        item["builtin"] = item["id"] in BUILTIN_IDS
        item["accounts"] = self.accounts(item["id"], include_disabled=True)
        item["account_count"] = len([a for a in item["accounts"] if a["enabled"]])
        item["model_count"] = len(item["models"])
        return item

    def list_providers(self, include_disabled: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM providers"
        if not include_disabled:
            sql += " WHERE enabled=1"
        sql += (
            " ORDER BY CASE id"
            " WHEN 'trae-cn' THEN 0 WHEN 'trae-global' THEN 1 WHEN 'workbuddy' THEN 2"
            " WHEN 'codebuddy' THEN 3 WHEN 'lobster' THEN 4 WHEN 'qoder-cn' THEN 5"
            " WHEN 'qoder-global' THEN 6 ELSE 20 END, id"
        )
        return [self._row_provider(row) for row in self.db.query(sql)]

    def get_provider(self, provider_id: str) -> Optional[dict[str, Any]]:
        row = self.db.query_one("SELECT * FROM providers WHERE id=?", (provider_id,))
        return self._row_provider(row) if row else None

    def create_custom(
        self,
        *,
        name: str,
        base_url: str,
        protocol: str,
        api_key: str,
        models_text: str = "",
        weight: int = 1,
    ) -> dict[str, Any]:
        base = normalize_base_url(base_url)
        if not base.startswith(("http://", "https://")):
            raise ValueError("base_url 必须是 http(s):// 地址")
        protocol = protocol if protocol in ("openai", "anthropic") else "openai"
        api_key = (api_key or "").strip()
        if api_key.lower().startswith("bearer "):
            api_key = api_key[7:].strip()
        if not api_key:
            raise ValueError("API Key 不能为空")
        digest = hashlib.sha256(f"{protocol}|{base}|{api_key}".encode("utf-8")).hexdigest()[:12]
        provider_id = f"custom-{digest}"
        now = _now()
        existing = self.db.query_one("SELECT * FROM providers WHERE id=?", (provider_id,))
        if not existing:
            models = [
                {"id": m, "object": "model", "owned_by": name or "custom", "context_length": 131072}
                for m in _split_models(models_text)
            ]
            self.db.execute(
                "INSERT INTO providers(id, name, kind, protocol, base_url, region, config_json, models_json, "
                "enabled, weight, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    provider_id,
                    name.strip() or base,
                    "custom",
                    protocol,
                    base,
                    "",
                    "{}",
                    json.dumps(models, ensure_ascii=False),
                    1,
                    max(1, min(100, int(weight or 1))),
                    now,
                    now,
                ),
            )
        if not any(a.get("api_key") == api_key for a in self.accounts(provider_id, include_disabled=True)):
            self.db.execute(
                "INSERT INTO provider_accounts(provider_id, name, api_key, auth_type, weight, enabled, created_at) "
                "VALUES(?,?,?,?,?,1,?)",
                (provider_id, name.strip() or "default", self.box.encrypt(api_key), "api_key",
                 max(1, min(100, int(weight or 1))), now),
            )
        return self.get_provider(provider_id) or {}

    def add_credential_account(
        self,
        provider_id: str,
        name: str,
        credential: dict[str, Any],
        *,
        uid: str = "",
        region: str = "",
        expires_at: int = 0,
        weight: int = 1,
        extra: dict[str, Any] | None = None,
    ) -> int:
        provider = self.get_provider(provider_id)
        if not provider:
            raise ValueError("平台不存在")
        now = _now()
        return self.db.execute(
            "INSERT INTO provider_accounts(provider_id, name, credential_json, auth_type, uid, region, "
            "expires_at, status, weight, enabled, extra_json, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,1,?,?)",
            (
                provider_id,
                name.strip() or (uid[-6:] if uid else "account"),
                self.box.encrypt(json.dumps(credential, ensure_ascii=False)),
                "oauth",
                uid,
                region or provider.get("region", ""),
                int(expires_at or 0),
                "ready",
                max(1, min(100, int(weight or 1))),
                json.dumps(extra or {}, ensure_ascii=False),
                now,
            ),
        )

    def update_credential(self, account_id: int, credential: dict[str, Any], expires_at: int = 0) -> None:
        self.db.execute(
            "UPDATE provider_accounts SET credential_json=?, expires_at=?, updated_at=? WHERE id=?",
            (
                self.box.encrypt(json.dumps(credential, ensure_ascii=False)),
                int(expires_at or 0),
                _now(),
                account_id,
            ),
        )

    def delete_account(self, account_id: int) -> None:
        self.db.execute("DELETE FROM provider_accounts WHERE id=?", (account_id,))

    def toggle_account(self, account_id: int) -> None:
        row = self.db.query_one("SELECT enabled FROM provider_accounts WHERE id=?", (account_id,))
        if row:
            self.db.execute("UPDATE provider_accounts SET enabled=? WHERE id=?", (0 if row["enabled"] else 1, account_id))

    def toggle_provider(self, provider_id: str) -> None:
        row = self.db.query_one("SELECT enabled FROM providers WHERE id=?", (provider_id,))
        if row:
            self.db.execute(
                "UPDATE providers SET enabled=?, updated_at=? WHERE id=?", (0 if row["enabled"] else 1, _now(), provider_id)
            )

    def accounts(self, provider_id: str, include_disabled: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM provider_accounts WHERE provider_id=?"
        if not include_disabled:
            sql += " AND enabled=1"
        sql += " ORDER BY id"
        result = []
        for row in self.db.query(sql, (provider_id,)):
            item = dict(row)
            item["api_key"] = self.box.decrypt(item.get("api_key", ""))
            item["credential"] = {}
            raw = item.get("credential_json") or ""
            if raw:
                try:
                    item["credential"] = json.loads(self.box.decrypt(raw))
                except Exception:
                    item["credential"] = {}
            try:
                item["extra"] = json.loads(item.get("extra_json") or "{}")
            except Exception:
                item["extra"] = {}
            item["packs"] = packs_loads(item.get("credit_packs_json"))
            result.append(item)
        return result

    def get_account(self, account_id: int) -> Optional[dict[str, Any]]:
        row = self.db.query_one("SELECT provider_id FROM provider_accounts WHERE id=?", (account_id,))
        if not row:
            return None
        for account in self.accounts(row["provider_id"]):
            if int(account["id"]) == int(account_id):
                return account
        return None

    def ordered_accounts(self, provider_id: str) -> list[dict[str, Any]]:
        accounts = [
            a
            for a in self.accounts(provider_id, include_disabled=False)
            if a.get("api_key") or a.get("credential")
        ]
        if not accounts:
            return []
        expanded = []
        for account in accounts:
            expanded.extend([account] * max(1, min(20, int(account.get("weight") or 1))))
        index = self._rr_index.get(provider_id, 0)
        ordered = [expanded[(index + i) % len(expanded)] for i in range(len(expanded))]
        self._rr_index[provider_id] = (index + 1) % len(expanded)
        seen = set()
        result = []
        for account in ordered:
            if account["id"] not in seen:
                seen.add(account["id"])
                result.append(account)
        return result

    def adapter(self, provider: dict[str, Any]):
        kind = provider.get("kind") or ""
        if kind == "lobster":
            from ..providers.lobster import LobsterProvider

            return LobsterProvider(self.settings, self.http, credential_saver=self.update_credential)
        if kind.startswith("qoder"):
            from ..providers.qoder import QoderProvider

            # Qoder 需要把随机生成的 machine_token 持久化回账号凭据。
            return QoderProvider(self.settings, self.http, credential_saver=self.update_credential)
        return adapter_for(self.settings, self.http, kind)

    # ------------------------------------------------------------------ models
    def add_model(
        self,
        provider_id: str,
        model_id: str,
        display_name: str = "",
        context_length: int = 131072,
    ) -> bool:
        provider = self.get_provider(provider_id)
        if not provider:
            raise ValueError("平台不存在")
        model_id = (model_id or "").strip()
        if not model_id:
            raise ValueError("模型 ID 不能为空")
        models = list(provider.get("models") or [])
        if any(str(item.get("id")) == model_id for item in models):
            return False
        models.append(
            {
                "id": model_id,
                "object": "model",
                "owned_by": provider_id,
                "display_name": display_name or model_id,
                "context_length": int(context_length or 131072),
                "manual": True,
            }
        )
        self.db.execute(
            "UPDATE providers SET models_json=?, updated_at=? WHERE id=?",
            (json.dumps(models, ensure_ascii=False), _now(), provider_id),
        )
        return True

    def remove_model(self, provider_id: str, model_id: str) -> bool:
        provider = self.get_provider(provider_id)
        if not provider:
            raise ValueError("平台不存在")
        models = [m for m in (provider.get("models") or []) if str(m.get("id")) != model_id]
        if len(models) == len(provider.get("models") or []):
            return False
        self.db.execute(
            "UPDATE providers SET models_json=?, updated_at=? WHERE id=?",
            (json.dumps(models, ensure_ascii=False), _now(), provider_id),
        )
        return True

    async def sync_models(self, provider_id: str) -> list[dict[str, Any]]:
        provider = self.get_provider(provider_id)
        if not provider:
            raise ValueError("服务商不存在")
        if provider["kind"] == "trae":
            raise ValueError("Trae 模型由账号服务自动同步")
        accounts = self.ordered_accounts(provider_id)
        if not accounts:
            raise ValueError("该平台没有启用账号")
        adapter = self.adapter(provider)
        models: list[dict[str, Any]] = []
        last_error: ProviderError | None = None
        for account in accounts:
            try:
                if adapter is not None:
                    models = await adapter.list_models(provider, account)
                elif provider["protocol"] == "anthropic":
                    models = await self.anthropic.list_models(provider, account)
                else:
                    models = await self.openai.list_models(provider, account)
            except ProviderError as exc:
                last_error = exc
                continue
            if models:
                break
        if not models and last_error is not None:
            self.db.execute(
                "UPDATE providers SET last_error=?, updated_at=? WHERE id=?",
                (str(last_error)[:1000], _now(), provider_id),
            )
            raise last_error
        if models:
            self.db.execute(
                "UPDATE providers SET models_json=?, last_synced_at=?, last_error='', updated_at=? WHERE id=?",
                (json.dumps(models, ensure_ascii=False), _now(), _now(), provider_id),
            )
        return models

    def models(self, provider_id: str) -> list[dict[str, Any]]:
        provider = self.get_provider(provider_id)
        return provider.get("models", []) if provider else []

    def resolve_provider_for_model(self, model: str) -> Optional[dict[str, Any]]:
        want = model.lower()
        for provider in self.list_providers(include_disabled=False):
            if provider["kind"] == "trae":
                continue
            for item in provider.get("models", []):
                if str(item.get("id", "")).lower() == want:
                    return provider
        return None

    def update_config(self, provider_id: str, base_url: str, config: dict[str, Any]) -> None:
        self.db.execute(
            "UPDATE providers SET base_url=?, config_json=?, updated_at=? WHERE id=?",
            (base_url.rstrip("/"), json.dumps(config, ensure_ascii=False), _now(), provider_id),
        )
