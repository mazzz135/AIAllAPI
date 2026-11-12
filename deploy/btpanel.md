# 宝塔面板部署 AIAll API

目标环境：宝塔 Linux 面板 + Python 项目，项目名 `AIAllAPI`，根目录 `/www/wwwroot/AIAllAPI`，端口 `2999`。

## 重启服务（带自检）

```bash
cd /www/wwwroot/AIAllAPI
bash deploy/restart.sh
```

脚本会先跑 `scripts/preflight.py`（编译 + 静态检查 + 测试重名），通过后才重启；紧急情况可 `SKIP_PREFLIGHT=1` 跳过。

## 1. 上传代码

把本项目全部文件上传到：

```text
/www/wwwroot/AIAllAPI
```

目录应至少包含：

```text
app/
scripts/
deploy/
requirements.txt
run.py
.env.example
```

不要上传本地 `.venv/`、`data/*.db`、`.env`、`data/secret.key`。

## 2. 安装依赖

SSH 执行：

```bash
cd /www/wwwroot/AIAllAPI
bash deploy/install.sh
```

脚本会：

- 创建 `.venv`
- 安装 `requirements.txt`
- 生成 `.env`（随机管理员密码、随机加密密钥）
- 初始化 SQLite 数据库
- 输出首次登录信息路径

首次登录：

```bash
cat /www/wwwroot/AIAllAPI/data/initial_admin.txt
cat /www/wwwroot/AIAllAPI/data/initial_api_key.txt
```

## 3. 宝塔创建 Python 项目

宝塔面板 → 网站 → Python 项目 → 添加项目：

| 字段 | 值 |
|---|---|
| 项目名称 | `AIAllAPI` |
| 项目路径 | `/www/wwwroot/AIAllAPI` |
| Python 版本 | 3.10 及以上（推荐 3.12/3.13） |
| 启动方式 | 自定义命令 |
| 启动命令 | `.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 2999 --proxy-headers` |
| 项目端口 | `2999` |
| 运行用户 | `www` |

如果面板要求填写入口文件，使用：

```text
run.py
```

并确保启动命令为：

```bash
.venv/bin/python run.py
```

## 4. 放行端口

宝塔 → 安全 → 放行 `2999`。

如果只通过 Nginx 反向代理访问，可以不向公网直接放行 2999，仅保留本机 `127.0.0.1:2999`。

## 5. 绑定域名与 HTTPS（推荐）

宝塔 → 网站 → 添加站点 → 反向代理：

- 代理目标：`http://127.0.0.1:2999`
- 发送域名：`$host`
- 开启 WebSocket/长连接支持（如有）
- 关闭代理缓冲（流式 SSE 必须）

Nginx 关键配置参考 `deploy/nginx.conf.example`，核心是：

```nginx
proxy_buffering off;
proxy_cache off;
proxy_read_timeout 600s;
add_header X-Accel-Buffering no;
```

然后在宝塔申请 Let's Encrypt 证书并开启强制 HTTPS。

## 6. 设置公网地址

管理台 → 设置 → 公网地址，填写实际访问地址，例如：

```text
https://trae.example.com
```

这样「生成登录链接」里的回调地址才会正确。

## 7. 添加 Trae 账号

管理台 →「平台聚合」→ Trae 国内版 / 国际版卡片 →「登录 Trae」。

1. 打开生成的官方授权链接（建议无痕窗口）
2. 完成 Trae 登录并授权
3. 浏览器会跳转到 `http://127.0.0.1:57321/authorize?...`，页面打不开是正常的
4. 复制地址栏完整 URL，粘贴回管理台提交

官方授权页强制要求回调为 `http://127.0.0.1:<port>/authorize`，所以必须使用“复制回调地址”的方式。

## 8. 平台聚合与模型映射

「平台聚合」页管理各平台账号与模型同步。

「虚拟模型组」就是模型映射入口：

```text
请求模型名（组名）：deepseek-flash
成员：Trae 国内版 / cn:deepseek-v4.1-flash
```

- 一个成员 = 固定映射
- 多个成员 = 按权重分摊、失败自动切换
- 成员从平台已有模型下拉选择
- 非 Trae 平台先同步模型；Trae 实时读取

## 9. 客户端接入

Base URL：

```text
https://你的域名/v1
```

API Key：管理台「设置」中的 `sk-trae-...`。

模型：先请求：

```bash
curl https://你的域名/v1/models \
  -H "Authorization: Bearer sk-trae-..."
```

然后选择返回的模型 `id`。

## 10. 日志与备份

- 使用日志：管理台 → 使用日志，可筛选、导出 CSV
- 签到记录：管理台 → 签到记录
  - 可设置自动签到时间（例如 01:00）
  - 到点后自动对全部平台和启用账号签到
  - 支持「立即全部签到」
- 数据文件：`/www/wwwroot/AIAllAPI/data/traeapi.db`
- 加密密钥：`/www/wwwroot/AIAllAPI/data/secret.key`

备份时同时备份 `traeapi.db` 和 `secret.key`，否则账号凭据无法解密。

## 11. 常见问题

| 现象 | 处理 |
|---|---|
| 启动失败端口占用 | `ss -lntp | grep 2999`，停止占用进程或改端口 |
| 管理台 502 | 检查宝塔 Python 项目是否运行、启动命令路径是否正确 |
| 流式无输出/一次性返回 | Nginx 关闭 `proxy_buffering`，保留 `X-Accel-Buffering: no` |
| 模型列表为空 | 先在账号页刷新积分，确认账号未失效；查看 `last_error` |
| 401 | 客户端 API Key 与管理台不一致，或 Key 已停用 |
| 503 | 没有启用账号，或账号并发已满 |
| 签到 9074 | 上游设备风控，稍后重试；本项目已按 UID 派生独立设备指纹 |
| 登录回调打不开 | 远程服务器场景直接复制回调链接粘贴 |
| 账号积分显示 — | 点击账号「刷新」，或检查 refreshToken/会话是否失效 |

## 12. 安全建议

- 首次登录后立即修改管理员密码
- 删除 `data/initial_admin.txt` 与 `data/initial_api_key.txt`
- 不要公开 `refreshToken`、`X-Cloudide-Session`、`secret.key`
- 公网部署必须开启 HTTPS
- 如只自己使用，建议在 Nginx 增加 IP 白名单或 Basic Auth
- 定期备份 `data/`，但不要把备份上传到公开仓库
