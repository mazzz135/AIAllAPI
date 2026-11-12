from __future__ import annotations

import json
import time
from typing import Any, Optional

from ..db import Database


def _now() -> int:
    return int(time.time())


class VirtualService:
    def __init__(self, db: Database):
        self.db = db
        self._rr_index: dict[int, int] = {}

    def list_groups(self, include_disabled: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM virtual_models"
        if not include_disabled:
            sql += " WHERE enabled=1"
        sql += " ORDER BY id"
        result = []
        for row in self.db.query(sql):
            item = dict(row)
            item["members"] = self.members(int(row["id"]))
            item["member_count"] = len(item["members"])
            result.append(item)
        return result

    def get_by_name(self, name: str) -> Optional[dict[str, Any]]:
        row = self.db.query_one("SELECT * FROM virtual_models WHERE name=? AND enabled=1", (name,))
        if not row:
            return None
        item = dict(row)
        item["members"] = self.members(int(row["id"]))
        return item

    def create(self, name: str, display_name: str = "") -> int:
        name = name.strip()
        if not name or any(ch in name for ch in ":/ "):
            raise ValueError("虚拟模型名不能为空，且不能包含 : / 空格")
        if self._name_taken(name):
            raise ValueError("该名称已被模型映射使用，请换一个名称")
        now = _now()
        return self.db.execute(
            "INSERT INTO virtual_models(name, display_name, enabled, created_at, updated_at) VALUES(?,?,1,?,?)",
            (name, display_name.strip() or name, now, now),
        )

    def _name_taken(self, name: str) -> bool:
        """重名校验：只按可路由的模型 id 判重。

        display_name 只是展示名（例如 id=hy4std 的展示名是 Hy4），
        与它重名不影响路由，因此不作为冲突；否则无法用 hy4 这类自然别名
        把 hy4free / hy4std 组合成虚拟模型。
        """
        want = name.lower()
        if self.db.query_one("SELECT 1 FROM virtual_models WHERE lower(name)=lower(?)", (name,)):
            return True
        for row in self.db.query("SELECT models_json FROM providers"):
            try:
                models = json.loads(row["models_json"] or "[]")
            except Exception:
                continue
            if not isinstance(models, list):
                continue
            for model in models:
                if not isinstance(model, dict):
                    continue
                if str(model.get("id") or "").lower() == want:
                    return True
        return False

    def toggle(self, group_id: int) -> None:
        row = self.db.query_one("SELECT enabled FROM virtual_models WHERE id=?", (group_id,))
        if row:
            self.db.execute(
                "UPDATE virtual_models SET enabled=?, updated_at=? WHERE id=?",
                (0 if row["enabled"] else 1, _now(), group_id),
            )

    def delete(self, group_id: int) -> None:
        self.db.execute("DELETE FROM virtual_models WHERE id=?", (group_id,))

    def add_member(self, group_id: int, provider_id: str, model: str, weight: int = 1) -> int:
        model = model.strip()
        if not model:
            raise ValueError("模型不能为空")
        return self.db.execute(
            "INSERT INTO virtual_members(virtual_id, provider_id, model, weight, enabled) VALUES(?,?,?,?,1)",
            (group_id, provider_id, model, max(1, min(100, int(weight or 1)))),
        )

    def delete_member(self, member_id: int) -> None:
        self.db.execute("DELETE FROM virtual_members WHERE id=?", (member_id,))

    def toggle_member(self, member_id: int) -> None:
        row = self.db.query_one("SELECT enabled FROM virtual_members WHERE id=?", (member_id,))
        if row:
            self.db.execute(
                "UPDATE virtual_members SET enabled=? WHERE id=?",
                (0 if row["enabled"] else 1, member_id),
            )

    def members(self, group_id: int, include_disabled: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM virtual_members WHERE virtual_id=?"
        if not include_disabled:
            sql += " AND enabled=1"
        sql += " ORDER BY id"
        return [dict(r) for r in self.db.query(sql, (group_id,))]

    def ordered_members(self, group: dict[str, Any]) -> list[dict[str, Any]]:
        members = [m for m in group.get("members", []) if m.get("enabled")]
        if not members:
            return []
        expanded = []
        for member in members:
            expanded.extend([member] * max(1, min(20, int(member.get("weight") or 1))))
        index = self._rr_index.get(int(group["id"]), 0)
        ordered = [expanded[(index + i) % len(expanded)] for i in range(len(expanded))]
        self._rr_index[int(group["id"])] = (index + 1) % len(expanded)
        seen = set()
        result = []
        for member in ordered:
            if member["id"] not in seen:
                seen.add(member["id"])
                result.append(member)
        return result
