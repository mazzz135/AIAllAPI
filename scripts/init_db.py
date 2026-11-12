#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.bootstrap import bootstrap  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import Database  # noqa: E402


def main() -> int:
    settings.ensure_dirs()
    db = Database(settings.db_path)
    bootstrap(settings, db)
    print(f"数据库: {settings.db_path}")
    print(f"数据目录: {settings.data_dir}")
    print(f"管理员凭据: {settings.initial_admin_path}")
    print(f"API Key: {settings.initial_api_key_path}")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
