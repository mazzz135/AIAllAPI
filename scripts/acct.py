"""账号临时停用 / 恢复 CLI。

用法：
    python scripts/acct.py list
    python scripts/acct.py disable <账号ID> [原因]
    python scripts/acct.py enable <账号ID>
    python scripts/acct.py revive <账号ID>

说明：
- 停用（disable）只摘除对话流量，签到、token 保活等任务照常执行。
- 启用（enable）只清除手动停用，不清冷却。
- 复活（revive）同时清除手动停用、冷却、连败计数与错误。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.bootstrap import bootstrap  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.services.pool import AccountPool  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="AIAll API 账号停用 / 恢复")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="列出账号状态")
    for name in ("disable", "enable", "revive"):
        cmd = sub.add_parser(name)
        cmd.add_argument("account_id", type=int)
        if name == "disable":
            cmd.add_argument("reason", nargs="?", default="")
    args = parser.parse_args()

    db = Database(settings.db_path)
    bootstrap(settings, db)
    pool = AccountPool(db)

    if args.command == "list":
        rows = db.query(
            "SELECT id, provider_id, name, enabled, manual_disabled, manual_reason, cooldown_until, "
            "credits, last_error FROM provider_accounts ORDER BY provider_id, id"
        )
        print(f"{'ID':>4}  {'平台':<12} {'账号':<24} {'启用':<4} {'摘除':<4} {'冷却至':<20} 积分")
        for row in rows:
            print(
                f"{row['id']:>4}  {row['provider_id']:<12} {str(row['name'])[:24]:<24} "
                f"{'是' if row['enabled'] else '否':<4} {'是' if row['manual_disabled'] else '否':<4} "
                f"{row['cooldown_until'] or '-':<20} {row['credits']}"
            )
        trae = db.query("SELECT id, name, enabled, manual_disabled, manual_reason FROM accounts ORDER BY id")
        if trae:
            print("\nTrae 账号：")
            for row in trae:
                print(
                    f"{row['id']:>4}  {'trae':<12} {str(row['name'])[:24]:<24} "
                    f"{'是' if row['enabled'] else '否':<4} {'是' if row['manual_disabled'] else '否':<4}"
                )
        return 0

    account_id = int(args.account_id)
    provider = db.query_one("SELECT id FROM provider_accounts WHERE id=?", (account_id,))
    if provider:
        if args.command == "disable":
            pool.set_manual_disabled(account_id, True, getattr(args, "reason", ""))
        elif args.command == "enable":
            pool.set_manual_disabled(account_id, False)
        else:
            pool.clear_cooldown(account_id)
            pool.set_manual_disabled(account_id, False)
        print(f"provider account {account_id}: {args.command} 完成")
        return 0

    trae = db.query_one("SELECT id FROM accounts WHERE id=?", (account_id,))
    if trae:
        now = int(__import__("time").time())
        if args.command == "disable":
            db.execute(
                "UPDATE accounts SET manual_disabled=1, manual_reason=?, updated_at=? WHERE id=?",
                (getattr(args, "reason", "")[:200], now, account_id),
            )
        elif args.command == "enable":
            db.execute(
                "UPDATE accounts SET manual_disabled=0, manual_reason='', updated_at=? WHERE id=?",
                (now, account_id),
            )
        else:
            db.execute(
                "UPDATE accounts SET manual_disabled=0, manual_reason='', last_error='', updated_at=? WHERE id=?",
                (now, account_id),
            )
        pool.persist_state(force=True)
        print(f"trae account {account_id}: {args.command} 完成")
        return 0

    print(f"账号 {account_id} 不存在", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
