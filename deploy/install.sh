#!/usr/bin/env bash
set -euo pipefail

ROOT="${1:-/www/wwwroot/AIAllAPI}"
cd "$ROOT"

echo "== AIAll API 安装 =="
echo "目录: $ROOT"

if ! command -v python3 >/dev/null 2>&1; then
  echo "未找到 python3，请先在宝塔「软件商店」安装 Python 3.10+。" >&2
  exit 1
fi

PYVER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
echo "Python: $PYVER"
python3 - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit("需要 Python 3.10 或更高版本")
PY

if [ ! -d .venv ]; then
  echo "创建虚拟环境..."
  python3 -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip -q
.venv/bin/python -m pip install -r requirements.txt -q

mkdir -p data
chmod 700 data 2>/dev/null || true

if [ ! -f .env ]; then
  echo "生成 .env ..."
  SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
  ADMIN_PASS="$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))')"
  cat > .env <<EOF
TRAEAPI_HOST=0.0.0.0
TRAEAPI_PORT=2999
TRAEAPI_DATA_DIR=$ROOT/data
TRAEAPI_ADMIN_USERNAME=admin
TRAEAPI_ADMIN_PASSWORD=$ADMIN_PASS
TRAEAPI_SECRET_KEY=$SECRET
TRAEAPI_SESSION_HOURS=72
TRAEAPI_TRUST_PROXY=1
TRAEAPI_DEFAULT_MODEL=DeepSeek-V4-Flash
TRAEAPI_IDE_VERSION=3.4.0
TRAEAPI_IDE_VERSION_CODE=20260716
TRAEAPI_CHECKIN_TIME=09:00
TRAEAPI_AUTO_CHECKIN=1
TRAEAPI_LOG_RETENTION_DAYS=30
TRAEAPI_MAX_CONCURRENT_PER_ACCOUNT=2
TRAEAPI_UPSTREAM_TIMEOUT=180
TRAEAPI_FIRST_EVENT_TIMEOUT=60
EOF
  chmod 600 .env
else
  echo ".env 已存在，保留原配置"
fi

echo "初始化数据库与管理员..."
.venv/bin/python scripts/init_db.py

chmod 600 data/secret.key data/initial_admin.txt data/initial_api_key.txt 2>/dev/null || true

# 宝塔 Python 项目通常以 www 用户运行，确保运行用户可读写数据与配置。
if [ "$(id -u)" = "0" ] && id www >/dev/null 2>&1; then
  chown -R www:www "$ROOT"
  chmod 600 "$ROOT/.env" "$ROOT/data/secret.key" 2>/dev/null || true
  chmod 600 "$ROOT/data/initial_admin.txt" "$ROOT/data/initial_api_key.txt" 2>/dev/null || true
fi

cat <<EOF

安装完成。

启动命令（宝塔 Python 项目中使用）：
  cd $ROOT && .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 2999

或：
  cd $ROOT && .venv/bin/python run.py

首次登录信息：
  cat $ROOT/data/initial_admin.txt

API Key：
  cat $ROOT/data/initial_api_key.txt

请登录管理台后：
  1. 修改管理员密码
  2. 在「Trae 账号」添加账号
  3. 在「设置」确认公网地址与签到时间
  4. 删除 data/initial_admin.txt 与 data/initial_api_key.txt
EOF
