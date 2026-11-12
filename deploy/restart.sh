#!/usr/bin/env bash
# 部署后重启：先跑自检，通过后再重启服务。
# 用法： bash deploy/restart.sh [项目根目录]
# 跳过自检： SKIP_PREFLIGHT=1 bash deploy/restart.sh
set -euo pipefail

ROOT="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$ROOT"

if [ -x "$ROOT/.venv/bin/python" ]; then
  PY="$ROOT/.venv/bin/python"
elif [ -x "/www/server/pyporject_evn/Traeapi/bin/python" ]; then
  PY="/www/server/pyporject_evn/Traeapi/bin/python"
else
  PY="python3"
fi

if [ "${SKIP_PREFLIGHT:-0}" != "1" ]; then
  echo "== 部署前自检"
  if ! "$PY" scripts/preflight.py; then
    echo "自检未通过，已中止重启（可设置 SKIP_PREFLIGHT=1 跳过）" >&2
    exit 1
  fi
fi

echo "== 重启服务"
PIDFILE="${PIDFILE:-/www/server/python_project/vhost/pids/Traeapi.pid}"
BT_CMD="/www/server/python_project/vhost/scripts/Traeapi_cmd.sh"

if [ -x "$BT_CMD" ] || [ -f "$BT_CMD" ]; then
  if [ -f "$PIDFILE" ]; then
    kill "$(cat "$PIDFILE")" 2>/dev/null || true
    sleep 2
  fi
  bash "$BT_CMD"
elif systemctl list-unit-files 2>/dev/null | grep -q '^aiall.service'; then
  systemctl restart aiall
else
  echo "未找到宝塔启动脚本或 systemd 单元，请手动重启：" >&2
  echo "  $PY run.py serve" >&2
  exit 1
fi

sleep 3
if command -v curl >/dev/null 2>&1; then
  curl -sS -o /dev/null -w 'health=%{http_code}\n' http://127.0.0.1:2999/health || true
fi
