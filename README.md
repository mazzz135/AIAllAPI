# AIAll API

把 Trae 国服账号、任意 OpenAI/Anthropic 兼容端点聚合成本地 **OpenAI 兼容 API**。FastAPI + SQLite，自带管理台，支持多平台分组、加权轮询、故障转移、虚拟模型、账号登录、自动续期、每日签到、积分计量与使用日志，目标运行在宝塔面板 Python 项目：

```text
项目名：AIAll API（目录名 AIAllAPI）
根目录：/www/wwwroot/AIAllAPI
端口：2999
```

> 已有部署继续使用 `/www/wwwroot/AIAllAPI` 也能正常运行，目录名不做强制迁移。

## 命名与兼容

项目已更名为 **AIAll API**，旧名 Traeapi 仍保留在少量兼容点，升级不需要改数据或重配：

| 项目 | 现状 | 说明 |
|---|---|---|
| 环境变量前缀 | `AIALL_*` 优先，`TRAEAPI_*` 仍生效 | 老 `.env` 不用改 |
| 数据库文件 | 仍为 `data/traeapi.db` | 数据保留；可用 `AIALL_DB_FILE` 改名 |
| 管理台会话 Cookie | `aiall_session`，兼容读取 `traeapi_session` | 升级后需重新登录一次 |
| 强制路由请求头 | `X-AIAll-Provider`，兼容 `X-Traeapi-Provider` | 老客户端不用改 |
| Prometheus 指标 | `aiall_*`，默认同时输出 `traeapi_*` / `wb2api_*` 别名 | 设置页可关闭别名 |
| 部署目录 | 新装默认 `/www/wwwroot/AIAllAPI` | 已有部署可继续用旧目录 |

## 部署前自检

```bash
python scripts/preflight.py              # 编译 + ruff F/E9 + 测试函数重名
python scripts/preflight.py --with-tests # 再加上 pytest
```

自检失败会返回非 0，建议接入部署脚本，防止未定义名、死代码、重复测试回归。

部署后重启（先自检再重启，失败即中止）：

```bash
bash deploy/restart.sh          # 宝塔：自动调用 Traeapi_cmd.sh；systemd：systemctl restart aiall
SKIP_PREFLIGHT=1 bash deploy/restart.sh   # 紧急跳过自检
```

## 功能

- OpenAI 兼容：`/v1/chat/completions`、`/v1/models`、`/v1/responses`、`/v1/status`
- 流式 SSE / 非流式 JSON
- 原生工具调用（function calling）、`reasoning_content` 思考内容
- 多 Trae 账号：积分降序调度、每账号并发限制、失败自动换号重试
- 登录方式：
  - Trae 国内版：官方网页登录回调链接（一次性 state 校验 + 回调限流）
  - Trae 国际版：`X-Cloudide-Session` Cookie 登录（Refresh Token 作为备用）
- Refresh Token 自动轮换与续期；网页会话自动换取 JWT
- 每日自动签到（签到记录页可设时间，默认 10:05，覆盖全部平台与账号）、手动一键全部签到
- Qoder 签到与其它平台统一使用每日签到时间，支持国内版 / 国际版与 PAT 自动换 job token
- LobsterAI 签到：`slot → context → check_in`，Token 临近过期自动 refresh；无签到槽/今日已签到/动作不可用分别记录，跟随全局签到时间
- 系统提示词三模式：`passthrough` / `custom` / `append`，支持 `prompt.file` 自定义文件
- 出站请求体指纹字段清洗可开关，默认开启
- 签到任务支持确定性抖动、JSONL 任务台账、`/admin/tasks/{name}/run` 异步补跑、当日全灭自动重试
- LobsterAI 客户端版本从官方更新接口动态解析（`client_version` 可覆盖），版本过旧会导致上游不下发签到活动槽
- WorkBuddy/CodeBuddy 成长任务：活跃地图、猫猫旅行、开学季、夜猫子，集中在「管理台 → 任务设置」页，独立开关与时间
- DeepSeek 请求注入 `thinking.type=enabled` 与默认 reasoning_effort，并回填 assistant reasoning_content
- 官方客户端会话头族：X-Conversation-* / X-Root-Request-ID / X-Trace-ID / X-B3-*
- API Key 独立页面：`管理台 → API Key`，支持重新生成 / 新增 / 停用
- 每个平台每天自动同步一次模型列表（Trae + 各平台），设置页可开关，默认开启
- 积分计量：优先使用上游余额差值，附 Token 估算
- 使用日志：模型、账号、Token、缓存命中、积分、耗时、状态、IP，可导出 CSV
- 管理台登录、会话、CSRF、密码 PBKDF2、凭据 Fernet 加密
- 模型目录从上游拉取并缓存 1 小时，失败回退静态模型
- 内置平台聚合：WorkBuddy 国际版、CodeBuddy 国内版、LobsterAI、Trae 国内/国际、Qoder 国内/国际
- 每个平台多账号，账号池按「积分比例 ×10 + 快过期积分占比 ×8 + 闲置补偿」三因子加权，Top-5 短名单内加权抽签
- 失败状态机：429 软冷却指数退避、404 浅冷却、402/余额耗尽硬冷却至次日 04:00、401/403 冷却、连续失败熔断（封顶 6h）
- 模型级限流独立冷却：6004 类错误只冷却该模型，`/v1/status` 透出 `rate_limited_models`
- 当日积分预算闸：中间件在读请求体之前判定，超限直接 429 `daily_budget_exceeded`，`/v1/status` 透出 `daily_budget`
- 账号临时摘除/恢复：`manual_disabled` 与自动 `enabled` 独立，摘除只影响对话选号，签到与保活照常
- 池状态落盘：`data/pool_state.json` 原子写入，可选镜像到 Upstash Redis（`upstash_redis_rest_url/token`）
- 运维 CLI：`python scripts/acct.py list | disable <id> [原因] | enable <id> | revive <id>`
- 会话粘性：同一对话固定同一账号（conversation_id / prompt_cache_key / 首条 user 消息哈希），默认 30 分钟
- 在途租约：单账号最大在途请求数限制，占满的账号不参与选号
- 成本账本：按 (账号, 模型) 记录实测积分/1K tokens，EMA 平滑，6 小时过期；平台维度聚合展示
- 成本分层：免费 > 未知 > 收费，只选最优层；条件探索每 30 分钟最多一次，把真实请求改道给未知账号（≤48 次/天/模型），成功即毕业
- 快过期积分：`pool_expiring_soon_days`（默认 7 天）窗口内的余额单独加权优先消耗
- `/v1/status` 透出 `model_costs`、`cost_explore` 与池参数
- 当日积分预算闸：达到上限后直接 429 `daily_budget_exceeded`
- 额度查询与每日签到
- 平台详情页展示账号剩余/已用/总积分、每个积分包的余额与有效期、快过期积分、冷却状态与在途请求，可一键刷新全部积分
- Trae 国际版按美元额度接口 `user_current_entitlement_list` 解析，积分明细显示 `$`；Qoder 国际版无可用额度时显示 0/无积分包
- 平台模型可手动添加/移除，自动同步失败时不再空白
- 概览页按平台拆分账号、模型、剩余/已用/总积分、今日请求与消耗
- 虚拟模型组页显示各平台积分概览与成员实测单价/1K
- 刷新积分为 AJAX 原地更新，不整页跳转、不改变滚动位置
- 模型映射：虚拟模型组即映射入口，组名是请求模型名，成员从平台已有模型中选择
- 单个成员 = 固定映射；多个成员 = 加权分摊 + 故障切换
- 模型路由：虚拟模型组、`provider:model`、`X-AIAll-Provider`（旧 `X-Traeapi-Provider` 仍兼容）、`?provider=` 强制路由
- Anthropic Messages ⇄ OpenAI Chat 请求与 SSE 流式翻译

## 内置平台

| 平台 ID | 平台 | 区域 | 协议 |
|---|---|---|---|
| `workbuddy` | WorkBuddy 国际版 | global | `/v2/plugin/auth/*` + `/v2/chat/completions` |
| `codebuddy` | CodeBuddy 国内版 | cn | 同上（`copilot.tencent.com`） |
| `lobster` | LobsterAI（网易有道） | cn | `/api/auth/*` + `/api/proxy/v1/chat/completions` |
| `trae-cn` | Trae 国内版 | cn | PKCE OAuth + `llm_utils_chat` |
| `trae-global` | Trae 国际版 | global | 同上（`trae.ai` 域名） |
| `qoder-cn` | Qoder 国内版 | cn | COSY 签名 + `/algo/api/v2/service/pro/sse/agent_chat_generation` |
| `qoder-global` | Qoder 国际版 | global | 同上（`api1.qoder.sh`） |

账号接入方式：

- **WorkBuddy / CodeBuddy**：管理台「平台聚合」→「网页登录」，浏览器授权后自动轮询保存
- **LobsterAI**：生成授权链接，登录后把 `127.0.0.1` 回调地址粘贴回来
- **Trae**：管理台「平台聚合」→ Trae 卡片 → 登录，官方授权后复制 `127.0.0.1` 回调地址粘贴回来
- **Qoder**：支持网页登录（官方设备码授权，自动轮询保存）；也可导入 Qoder CLI user JSON，或 PAT + UID。每日签到走 `/sash/api/v1/me/campaigns`，领取 100 Credits（30 天有效）
- 所有平台都支持直接粘贴 JSON 凭据导入

## 模型映射（虚拟模型组）

管理台「虚拟模型组」就是模型映射入口：

```text
请求模型名（组名）：deepseek-flash
成员：Trae 国内版 / cn:deepseek-v4.1-flash
```

- 只添加一个成员：固定映射到该平台模型
- 添加多个成员：按权重分摊，某个成员失败自动切换
- 成员模型从平台已有模型下拉选择，不需要手写上游模型 ID
- 非 Trae 平台先在「平台聚合」同步模型；Trae 模型实时读取

客户端请求：

```json
{"model":"deepseek-flash","messages":[{"role":"user","content":"你好"}]}
```

### 路由规则

```text
# 裸模型名：自动匹配服务商
{"model":"deepseek-v4.1-flash"}

# 显式指定平台
{"model":"trae-cn:deepseek-v4.1-flash"}
{"model":"workbuddy:deepseek-v4.1-flash"}

# 请求头 / 查询参数
X-AIAll-Provider: trae-cn
?provider=trae-cn
```

### 虚拟模型组

在管理台「虚拟模型」创建组名（如 `fast`），把不同服务商的具体模型加入成员并设置权重：

```json
{"model":"fast","messages":[{"role":"user","content":"你好"}]}
```

代理会按权重在成员间分摊；某个成员不可用时自动跳过。虚拟模型名优先于同名真实模型。

## 架构

```text
OpenAI 客户端
   │  /v1/chat/completions
   ▼
AIAll API (FastAPI :2999)
   ├─ API Key 鉴权
   ├─ Platform Router（WorkBuddy / CodeBuddy / LobsterAI / Trae / Qoder / Custom）
   ├─ 模型注册表（别名 → 平台 + 上游模型 + 默认参数）
   ├─ 多账号加权轮询 + 组内故障跳过
   ├─ OpenAI → 平台协议转换
   ├─ SSE 流式转换与聚合
   ├─ 积分回填 + 使用日志
   └─ 管理台 / 签到调度
   │
   ├──▶ WorkBuddy / CodeBuddy
   ├──▶ LobsterAI
   ├──▶ Trae 国内 / 国际
   ├──▶ Qoder 国内 / 国际
   ├──▶ OpenAI / Anthropic 兼容端点
   └──▶ 虚拟模型组
   ├─ /api/agent/v3/llm_utils_chat
   ├─ /api/ide/v1/get_detail_param
   ├─ /cloudide/api/v3/trae/oauth/ExchangeToken
   ├─ /trae/api/v2/ug/checkin_credits/*
   ├─ /trae/api/v2/pay/ide_user_ent_usage
   └─ /sash/api/v1/me/campaigns/*（Qoder 签到）
```

## 本地运行

要求 Python 3.10+。

```bash
cd "F:/AI/workbuddy/TRAE API"
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python scripts/init_db.py
.venv/Scripts/python -m uvicorn app.main:app --host 127.0.0.1 --port 2999
```

打开：

```text
http://127.0.0.1:2999/admin
```

首次管理员凭据在 `data/initial_admin.txt`，API Key 在 `data/initial_api_key.txt`。

## 宝塔部署

完整步骤见 [deploy/btpanel.md](deploy/btpanel.md)。

最短流程：

```bash
cd /www/wwwroot/AIAllAPI
bash deploy/install.sh
```

宝塔 Python 项目启动命令：

```bash
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 2999 --proxy-headers
```

或：

```bash
.venv/bin/python run.py
```

Nginx 必须关闭代理缓冲，否则流式会变成一次性返回：

```nginx
proxy_buffering off;
proxy_cache off;
proxy_read_timeout 600s;
add_header X-Accel-Buffering no;
```

## 客户端接入

| 配置 | 值 |
|---|---|
| Base URL | `http://127.0.0.1:2999/v1` 或 `https://你的域名/v1` |
| API Key | 管理台「设置」中的 `sk-trae-...` |
| 模型 | `GET /v1/models` 返回的 `id` |

示例：

```bash
curl http://127.0.0.1:2999/v1/chat/completions \
  -H "Authorization: Bearer sk-trae-..." \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-v4.1-flash",
    "messages": [{"role": "user", "content": "你好"}],
    "stream": false
  }'
```

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查，无需鉴权 |
| GET | `/v1/models` | 模型列表 |
| POST | `/v1/chat/completions` | Chat Completions，支持流式 |
| POST | `/v1/responses` | Responses 兼容（基础文本/工具调用） |
| GET | `/v1/status` | 账号与服务状态（含 `pool_health` / `alerting`） |
| GET | `/v1/usage` | 当前 API Key 今日用量与最近记录 |
| GET | `/metrics` | Prometheus 文本格式 0.0.4，默认关闭，开启后需 API Key |
| GET | `/docs` | FastAPI 自动文档 |

管理台：

| 路径 | 说明 |
|---|---|
| `/admin` | 概览 |
| `/admin/platforms` | 平台聚合（账号数/模型数统计，旧 `/admin/accounts` 自动跳转） |
| `/admin/platforms/{id}` | 平台详情：账号列表、剩余/已用/总积分、模型列表、摘除/恢复流量 |
| `/admin/virtuals` | 虚拟模型组（模型映射）与权重成员 |
| `/admin/checkins` | 自动签到设置与全平台签到记录 |
| `/admin/trae/token` | Trae 国际版 Cookie 登录（Refresh Token 备用） |
| `/admin/logs` | 使用日志与 CSV 导出 |
| `/admin/settings` | 设置、API Key、密码 |

## 配置

配置来自 `.env`，也可在管理台修改部分运行参数。

| 变量 | 默认 | 说明 |
|---|---|---|
| `TRAEAPI_HOST` | `0.0.0.0` | 监听地址 |
| `TRAEAPI_PORT` | `2999` | 端口 |
| `TRAEAPI_DATA_DIR` | `./data` | 数据目录 |
| `TRAEAPI_ADMIN_USERNAME` | `admin` | 管理员用户名 |
| `TRAEAPI_ADMIN_PASSWORD` | 空 | 首次初始化密码；空则随机生成 |
| `TRAEAPI_SECRET_KEY` | 空 | 凭据加密密钥；空则生成 `data/secret.key` |
| `TRAEAPI_DEFAULT_MODEL` | `deepseek-v4.1-flash` | 默认模型 |
| `TRAEAPI_CHECKIN_TIME` | `10:05` | 每日签到时间（北京时间，所有平台统一） |
| `TRAEAPI_AUTO_CHECKIN` | `1` | 自动签到开关 |
| `TRAEAPI_LOG_RETENTION_DAYS` | `30` | 使用日志保留天数 |
| `TRAEAPI_MAX_CONCURRENT_PER_ACCOUNT` | `2` | 每账号并发 |
| `TRAEAPI_UPSTREAM_TIMEOUT` | `180` | 上游读超时秒 |
| `TRAEAPI_FIRST_EVENT_TIMEOUT` | `60` | 首事件超时秒 |

管理台可改：默认模型、每日签到时间、自动签到、日志保留、并发、超时、上游 function、公网地址、任意 Key 开关。

## 可观测与告警

三项能力默认关闭，均在「设置 → 可观测与告警」中开启。

### Prometheus `/metrics`

- `metrics.enabled=1` 后暴露 `GET /metrics`，使用与 `/v1/status` 相同的 API Key 鉴权。
- 文本格式为 Prometheus 0.0.4；标签顺序、序列顺序固定，连续抓取可直接 diff。
- 池指标按 `realm="cn|global|all"` 拆分：账号规模/状态分布、在途请求、会话粘性绑定、成本探索累计触发、WAF/IP 拦截信号。
- 模型指标（当日，北京时间）：成功/失败请求、流式请求、prompt/completion/total token、缓存 hit/miss/write、积分消耗、TTFT/端到端平均耗时、生成吞吐。
- `wb2api_task_*` 输出已运行定时任务的最近一轮：时间戳、总数、成功、失败、跳过、是否全灭；未运行过的任务不输出。
- 只暴露聚合量，不含账号 uid、token、cookie、API Key 等敏感维度。

### 可用性阈值告警

- `alerting.enabled=1` 后每 `alerting.interval_seconds`（默认 30s）评估一次。
- 规则：CN/Global 健康账号不足（`min_healthy_cn=1`、`min_healthy_global=0` 表示不评估）、熔断账号超限（`breaker_threshold=0` 表示不评估）、WAF/IP 拦截信号。
- 边沿触发 + 迟滞：连续 `for_ticks` 拍才投递一次；连续 `clear_ticks` 拍才恢复，避免临界点抖动。
- `startup_grace_seconds` 默认 30s，避开启动初期空池假告警；`send_resolve=1` 时发送恢复通知。
- 投递到 `alerting.webhook_url`，超时 `alerting.timeout_seconds`（默认 5s）；配置 `alerting.secret` 后对请求体做 HMAC-SHA256，请求头 `X-WB2A-Signature: sha256=<hex>`。
- 通知内容只含服务名、规则、realm、计数、阈值、时间，不含任何凭证。

### 管理操作审计

- `admin.audit_enabled=1` 且 `admin.enabled=1` 时，`/admin` 下通过鉴权的写操作追加 JSONL 到 `admin.audit_file`（默认 `./data/admin_audit.log`）。
- 字段：`ts`、`time`、`action`、`object`、`status`、`ok`、`ip`、`api_key_fp`、`reason`、`method`、`path`。
- 只记录已通过管理员会话鉴权的请求；`api_key_fp` 是会话/Key 的 SHA-256 前缀，不可反推原文。
- 每条记录独立开-写-关，外部 logrotate 后自动写新文件；写失败只告警，不影响管理操作。
- 审计路径不可写时启动直接报错，避免上线后才发现。

## 积分计量说明

每次请求记录两类积分：

- `credits_delta`：请求前后查询 Trae 余额差值，**权威值**；并发时可能合并或无法拆分，显示 `—`
- `credits_estimated`：按模型目录 `consumption_rate` 与 Token 数估算，**仅参考**

Token 来自上游 `token_usage` 事件；若上游未返回，日志中为 0。

## 安全

- 管理员密码 PBKDF2-SHA256（260,000 次）
- 管理台会话随机 Token，仅存 SHA-256
- 所有管理台 POST 校验 CSRF
- Trae access/refresh/session 凭据使用 Fernet 加密后落库
- API Key 同时保存哈希与加密值，便于管理台展示；数据库泄露风险仍需自行控制
- 公网部署请开启 HTTPS，建议加 IP 白名单
- 不要公开 `data/secret.key`、`data/traeapi.db`、`refreshToken`、会话 Cookie

## 已知限制

- Trae 官方授权页强制要求回调为 `http://127.0.0.1:<port>/authorize`，因此使用“授权后复制回调地址”的方式，不依赖本机监听
- Trae 国际版使用网页 Cookie 登录，Cookie 过期后重新复制；Refresh Token 仅作为备用方式
- Qoder 已内置设备码网页登录；COSY 签名与 SSE 解包已实现；签到使用官方 campaigns 活动接口，国内版通常直接可用
- Qoder 国际版首次签到要求官方客户端真实设备指纹；未在官方客户端领取过且未提供 `checkin_fingerprint` 时会返回无可用活动，先在官方客户端签到一次即可转为正常下发
- WorkBuddy 与 CodeBuddy 共用协议，仅 Origin / 域名不同；两者网页登录均已验证能取到官方授权链接
- 各平台上游协议可能随客户端版本变化，平台地址、UA 与路径均可在管理台配置
- Anthropic 自定义端点当前支持文本、工具调用、thinking 与 SSE；图片输入会提示省略，需扩展 base64 图片转换
- 自定义 OpenAI 端点按 OpenAI Chat Completions 协议转发，不保证所有厂商的非标准字段
- 企业 SaaS 账号可导入，但签到、积分展示按个人账号逻辑处理，可能显示不可用
- `temperature`、`top_p`、`seed`、`max_tokens` 等采样参数不会透传到 Trae 上游
- `stop` 在本地截断正文，上游仍可能继续消耗额度
- Responses 接口为兼容层，复杂托管工具（web search / file search / code interpreter）不提供
- 上游协议可能变化，模型目录与聊天接口需随 Trae 客户端版本更新
- 本项目仅供个人学习与研究，使用风险自负

## 参考项目

协议与交互参考：

- [jiunianyyds/traework2api-android](https://github.com/jiunianyyds/traework2api-android)
- [Santonin205/Trae-Relay](https://github.com/Santonin205/Trae-Relay)
- [star620/TraeTools](https://github.com/star620/TraeTools)
- [RapidAI/RapidProxy](https://github.com/RapidAI/RapidProxy)（分组、加权、虚拟模型思路）
- [caigee-cmd/cli2api](https://github.com/caigee-cmd/cli2api)（WorkBuddy / CodeBuddy / Qoder 协议参考）
- [51v/lobsterai2api](https://github.com/51v/lobsterai2api)（LobsterAI 协议参考）
- [HanawaBanana/workbuddy2api](https://github.com/HanawaBanana/workbuddy2api)（账号池三因子选号、冷却熔断、会话粘性、成本账本、积分预算闸）

本项目为独立 Python 实现，未直接复制上述项目的 UI 或二进制代码。

## 目录

```text
app/
  api/          OpenAI 兼容接口、管理台路由
  services/     账号池、聊天转换、日志、签到调度、鉴权
  upstream/     Trae 客户端、SSE 解析、设备指纹
  templates/    管理台页面
  static/       CSS / JS
scripts/        初始化、冒烟测试
deploy/         宝塔安装脚本、systemd、Nginx 示例
tests/          单元与接口测试
data/           SQLite、加密密钥、初始凭据（运行时生成）
```
