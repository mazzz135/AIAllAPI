# AIAll API

多平台 AI 账号聚合网关与管理台。把 WorkBuddy、CodeBuddy、LobsterAI、Trae、Qoder 以及自定义 OpenAI / Anthropic 兼容端点，统一成一套 **OpenAI 兼容 API**；同时提供账号池调度、自动签到、积分计量、模型路由、使用日志与可观测能力。

- 技术栈：FastAPI + SQLite + httpx + Jinja2
- 管理台：`/admin`
- API Base：`/v1`
- 默认端口：`2999`
- 默认模型：`deepseek-v4.1-flash`
- 默认部署目录：`/www/wwwroot/AIAllAPI`
- 运行要求：Python 3.10+（推荐 3.12 / 3.13）

---

## 目录

- [核心特性](#核心特性)
- [支持平台](#支持平台)
- [快速开始](#快速开始)
- [客户端接入](#客户端接入)
- [API 参考](#api-参考)
- [模型路由与虚拟模型](#模型路由与虚拟模型)
- [账号池与调度](#账号池与调度)
- [自动签到与任务](#自动签到与任务)
- [积分与成本](#积分与成本)
- [管理台](#管理台)
- [配置](#配置)
- [可观测与告警](#可观测与告警)
- [安全](#安全)
- [部署](#部署)
- [兼容与迁移](#兼容与迁移)
- [目录结构](#目录结构)
- [已知限制](#已知限制)
- [参考项目](#参考项目)

---

## 核心特性

### 协议与接口

- OpenAI 兼容：`/v1/chat/completions`、`/v1/models`、`/v1/responses`、`/v1/status`、`/v1/usage`
- 流式 SSE 与非流式 JSON
- 原生工具调用（function calling）、`reasoning_content` 思考内容
- Anthropic Messages 与 OpenAI Chat 的请求、响应、SSE 双向翻译（作为上游协议）
- DeepSeek 请求自动注入 `thinking.type=enabled` 与默认 `reasoning_effort`，并回填 assistant `reasoning_content`
- 出站请求体指纹字段清洗，可按设置开关，默认开启
- 官方客户端会话头族：`X-Conversation-*`、`X-Root-Request-ID`、`X-Trace-ID`、`X-B3-*`

### 平台与账号

- 内置 7 个平台：WorkBuddy、CodeBuddy、LobsterAI、Trae 国内版、Trae 国际版、Qoder 国内版、Qoder 国际版
- 每个平台支持多账号，账号独立启停、冷却、并发与积分
- 网页登录、设备码登录、回调地址粘贴、Cookie 登录、JSON 凭据导入
- 上游 Token 自动刷新与续期；Trae 网页会话自动换取 JWT
- 凭据使用 Fernet 加密后落库，数据库只保存密文

### 调度与容灾

- 三因子选号：积分比例、快过期积分占比、闲置补偿
- Top-5 候选短名单内加权抽签，避免单账号被持续打满
- 失败状态机：429 软冷却、404 浅冷却、402/余额耗尽硬冷却、401/403 冷却、连续失败熔断
- 模型级限流独立冷却，6004 类错误只冷却对应模型
- 会话粘性：同一对话优先固定同一账号
- 在途租约：单账号最大并发限制，占满账号不参与选号
- 成本账本：按 `(账号, 模型)` 记录实测积分/1K tokens，EMA 平滑
- 成本分层：免费 > 未知 > 收费，优先低成本账号
- 条件探索：把少量真实请求改道给未知成本账号，成功即毕业
- 快过期积分优先消耗
- 当日积分预算闸：超限直接 `429 daily_budget_exceeded`
- 账号池状态原子落盘，可选镜像到 Upstash Redis

### 模型与路由

- 虚拟模型组：组名即请求模型名，成员可跨平台、带权重
- 单成员固定映射；多成员加权分摊 + 故障切换
- 路由优先级：虚拟模型 > `provider:model` > `X-AIAll-Provider` 请求头 > `?provider=` 查询参数 > 裸模型名自动匹配
- 每个平台每天自动同步一次模型列表（Trae 实时读取，其他平台走缓存）
- 平台模型支持手动添加 / 移除，同步失败不会清空已有模型

### 运营与管理

- 统一自动签到设置：开关、每日时间、今日是否执行、连续签到天数
- 签到记录按平台、账号、日期、状态、积分、原因留痕
- WorkBuddy / CodeBuddy 成长任务：活跃地图、猫猫旅行、开学季、夜猫子
- 任务台账：数据库 `task_runs` + 可选 JSONL 文件
- 任务确定性抖动、失败自动重试、手动异步补跑
- 积分包与有效期统计，支持美元 / 积分单位
- 使用日志：模型、账号、Token、缓存命中、积分、耗时、状态、IP，可导出 CSV
- 平台连通性测试：一次只读探测，不写库、不消耗额度
- 管理台登录、会话、CSRF、密码 PBKDF2、操作审计
- Prometheus 指标、可用性阈值告警

---

## 支持平台

### 平台总览

| 平台 ID | 平台 | 区域 | 上游地址 | 聊天协议 |
|---|---|---|---|---|
| `workbuddy` | WorkBuddy | 国际 | `https://www.workbuddy.ai` | `/v2/plugin/auth/*` + `/v2/chat/completions` |
| `codebuddy` | CodeBuddy | 国内 | `https://copilot.tencent.com` | 同 WorkBuddy，Origin 为 `https://www.codebuddy.cn` |
| `lobster` | LobsterAI（网易有道） | 国内 | `https://lobsterai-server.youdao.com` | `/api/auth/*` + `/api/proxy/v1/chat/completions` |
| `trae-cn` | Trae 国内版 | 国内 | `https://trae-api-cn.mchost.guru` | PKCE OAuth + `llm_utils_chat` |
| `trae-global` | Trae 国际版 | 国际 | `https://trae-api-sg.mchost.guru` | 同 Trae，国际域名与额度接口 |
| `qoder-cn` | Qoder 国内版 | 国内 | `https://gateway.qoder.com.cn` | COSY 签名 + `/algo/api/v2/service/pro/sse/agent_chat_generation` |
| `qoder-global` | Qoder 国际版 | 国际 | `https://api1.qoder.sh` | 同 Qoder，OpenAPI 为 `https://openapi.qoder.sh` |
| `custom-*` | 自定义端点 | - | 任意 `http(s)://` | OpenAI Chat Completions 或 Anthropic Messages |

> 自定义端点通过服务层 `ProviderService.create_custom(protocol="openai" | "anthropic")` 接入；管理台「自定义端点」入口当前收敛到「平台聚合」页。

### 账号接入方式

| 平台 | 登录方式 |
|---|---|
| WorkBuddy / CodeBuddy | 管理台「平台聚合」→「网页登录」，浏览器授权后自动轮询保存 |
| LobsterAI | 生成授权链接，登录后把 `127.0.0.1` 回调地址粘贴回来 |
| Trae 国内版 | 官方网页授权，复制 `127.0.0.1` 回调地址粘贴回来（一次性 `state` 校验 + 回调限流） |
| Trae 国际版 | `X-Cloudide-Session` Cookie 登录，Refresh Token 作为备用 |
| Qoder 国内 / 国际 | 设备码网页登录（自动轮询）、导入 Qoder CLI user JSON、PAT + UID |
| 全部平台 | 支持直接粘贴 JSON 凭据导入 |

### 签到能力

| 平台 | 签到接口 | 说明 |
|---|---|---|
| WorkBuddy / CodeBuddy | `POST /billing/meter/daily-checkin`（国际）<br>`POST /v2/billing/meter/daily-checkin`（国内） | 区分成功、今日已签到、活动不可用 |
| LobsterAI | `slot → context → check_in` | 客户端版本从官方更新接口动态解析；无签到槽、今日已签到、动作不可用分别记录 |
| Trae 国内 / 国际 | `/trae/api/v2/ug/checkin_credits/*` | 领取每日签到积分，记录有效期 |
| Qoder 国内 / 国际 | `/sash/api/v1/me/campaigns` | 领取 100 Credits，30 天有效；国内版通常直接可用 |

签到统一走一个调度：每日固定时间、全平台、全账号执行，默认 `10:05`。

---

## 快速开始

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python scripts/init_db.py
.venv/Scripts/python -m uvicorn app.main:app --host 127.0.0.1 --port 2999
```

打开管理台：

```text
http://127.0.0.1:2999/admin
```

首次启动会生成：

```text
data/initial_admin.txt    # 管理员账号密码
data/initial_api_key.txt  # 初始 API Key
data/secret.key           # 凭据加密密钥，请备份
data/traeapi.db           # SQLite 数据库
```

部署前自检：

```bash
python scripts/preflight.py              # 编译 + ruff F/E9 + 测试函数重名
python scripts/preflight.py --with-tests # 再加上 pytest
```

---

## 客户端接入

| 配置 | 值 |
|---|---|
| Base URL | `http://127.0.0.1:2999/v1` 或 `https://你的域名/v1` |
| API Key | 管理台「API Key」页生成，形如 `sk-trae-...` |
| 模型 | `GET /v1/models` 返回的 `id`，或虚拟模型组名 |

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

OpenAI SDK 直接指向 `base_url` 即可：

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:2999/v1",
    api_key="sk-trae-...",
)

resp = client.chat.completions.create(
    model="deepseek-v4.1-flash",
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)
```

---

## API 参考

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查，无需鉴权 |
| GET | `/healthz` | 同上 |
| GET | `/v1/models` | 模型列表 |
| GET | `/models` | 兼容别名 |
| POST | `/v1/chat/completions` | Chat Completions，支持流式 |
| POST | `/chat/completions` | 兼容别名 |
| POST | `/v1/responses` | Responses 兼容（基础文本 / 工具调用） |
| GET | `/v1/status` | 账号池、模型成本、预算、限流模型等状态 |
| GET | `/v1/usage` | 当前 API Key 今日用量与最近记录 |
| GET | `/metrics` | Prometheus 文本格式 0.0.4，默认关闭，开启后需 API Key |
| GET | `/` | 跳转 `/admin` |

> 自动文档已关闭：`/docs`、`/redoc`、`/openapi.json` 均返回 404，避免公开部署时泄露接口清单。

---

## 模型路由与虚拟模型

### 路由优先级

```text
虚拟模型组名
  > provider:model（如 trae-cn:deepseek-v4.1-flash）
  > X-AIAll-Provider 请求头（旧 X-Traeapi-Provider 兼容）
  > ?provider= 查询参数
  > 裸模型名自动匹配服务商
```

示例：

```json
{"model": "deepseek-v4.1-flash"}
```

```json
{"model": "workbuddy:deepseek-v4.1-flash"}
```

```bash
curl http://127.0.0.1:2999/v1/chat/completions \
  -H "Authorization: Bearer sk-trae-..." \
  -H "X-AIAll-Provider: qoder-cn" \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-v4.1-flash","messages":[{"role":"user","content":"hi"}]}'
```

### 虚拟模型组

管理台「虚拟模型」就是模型映射入口：

```text
组名（请求模型名）：fast
成员 1：trae-cn / deepseek-v4.1-flash，权重 3
成员 2：workbuddy / deepseek-v4.1-flash，权重 1
```

```json
{"model": "fast", "messages": [{"role": "user", "content": "你好"}]}
```

- 单个成员：固定映射
- 多个成员：按权重分摊，成员失败自动切换
- 虚拟模型名优先于同名真实模型
- 成员模型从平台已有模型下拉选择，不需要手写上游模型 ID

---

## 账号池与调度

### 选号

账号权重由三部分叠加：

```text
积分比例 × 10 + 快过期积分占比 × 8 + 闲置补偿
```

按权重取 Top-5 候选，再在短名单内加权抽签，避免高积分账号被持续打满。

### 失败状态机

| 状态 | 处理 |
|---|---|
| `429` | 软冷却，指数退避 |
| `404` | 浅冷却 |
| `402` / 余额耗尽 | 硬冷却至次日 04:00 |
| `401` / `403` | 冷却，等待重新登录或刷新 |
| 连续失败 | 熔断，封顶 6 小时 |
| `6004` 类限流 | 只冷却该模型，不影响同账号其他模型 |

### 会话与并发

- 会话粘性键：`conversation_id` → `prompt_cache_key` → 首条 user 消息哈希，默认 30 分钟
- 单账号在途请求上限由 `max_concurrent_per_account` 控制（默认 2）
- 占满账号不参与选号；请求结束自动释放

### 成本与预算

- 成本账本按 `(账号, 模型)` 记录实测积分/1K tokens，EMA 平滑，6 小时过期
- 成本分层：免费 > 未知 > 收费，只选最优层
- 条件探索：每 30 分钟最多一次，每天每模型最多 48 次，成功即毕业
- 快过期积分窗口默认 7 天，窗口内余额优先消耗
- 当日积分预算由 `daily_credit_limit` 控制，0 表示不限制；超限返回 `429 daily_budget_exceeded`

### 状态落盘

- 账号池状态写入 `data/pool_state.json`，使用原子替换
- 可选镜像到 Upstash Redis：设置 `upstash_redis_rest_url` 与 `upstash_redis_rest_token`

### 运维 CLI

```bash
python scripts/acct.py list
python scripts/acct.py disable <账号ID> [原因]
python scripts/acct.py enable <账号ID>
python scripts/acct.py revive <账号ID>
```

`disable` 只摘除对话流量，签到与 Token 保活照常；`revive` 同时清除手动停用、冷却、连败计数与错误。

---

## 自动签到与任务

### 统一签到

| 设置 | 默认 | 说明 |
|---|---|---|
| `auto_checkin` | `1` | 自动签到开关 |
| `checkin_time` | `10:05` | 每日签到时间（北京时间） |
| `last_checkin_run` | - | 今日是否已执行 |

签到覆盖全部平台与账号；手动「一键全部签到」与自动任务共用同一套记录。

### 定时任务

| 任务 | 默认时间 | 开关 | 说明 |
|---|---|---|---|
| 全部签到 | `10:05` | `auto_checkin` | Trae + 所有平台签到 |
| 模型同步 | `10:10` | `models_auto_refresh` | 每个平台每天同步一次模型列表 |
| 活跃地图 | `10:00` | `schedule.activity_enabled` | 事件上报点亮地图、连登兑换、抽奖、礼包领取 |
| 猫猫旅行 | `09:00` | `schedule.travel_enabled` | 领养、派出、到站领奖 |
| 开学季 | `12:00` | `schedule.school_enabled` | 任务点亮、claim、抽奖余额，活动下线自动跳过 |
| 夜猫子 | `01:00` | `schedule.cat_enabled` | 23:00–08:00 CST 窗口内补一次 `black_cat` |

### 任务执行

- 确定性抖动：`schedule.jitter_minutes`
- 失败重试：`schedule.retry_delay_minutes`、`schedule.retry_max_per_day`
- 台账：数据库 `task_runs`，可选 JSONL `schedule.ledger_file`
- 手动补跑：`POST /admin/tasks/{name}/run`，后台异步执行
- 任务状态：`CONTINUE` / `DONE` / 失败重试

任务名：`checkin`、`trae_checkin`、`platform_checkin`、`model_refresh`、`activity`、`travel`、`school`、`cat`。

---

## 积分与成本

每次请求记录两类积分：

| 字段 | 含义 |
|---|---|
| `credits_delta` | 请求前后余额差值，**权威值**；并发时可能合并或无法拆分，显示 `—` |
| `credits_estimated` | 按模型目录 `consumption_rate` 与 Token 数估算，**仅参考** |

积分包与有效期：

- 平台详情页展示每个积分包的余额、有效期、快过期积分
- Trae 国际版按 `user_current_entitlement_list` 解析美元额度，明细显示 `$`
- Qoder 国际版无可用额度时显示 `0` / 无积分包
- Qoder 签到积分 30 天有效，纳入快过期统计
- 支持一键刷新全部积分，AJAX 原地更新，不改变滚动位置

Token 来自上游 `token_usage` 事件；上游未返回时日志中为 0。

---

## 管理台

| 路径 | 说明 |
|---|---|
| `/admin` | 概览：按平台拆分账号、模型、剩余/已用/总积分、今日请求与消耗 |
| `/admin/platforms` | 平台聚合：账号数、模型数、连通性测试、登录入口 |
| `/admin/platforms/{id}` | 平台详情：账号列表、积分、模型、签到、摘除/恢复流量 |
| `/admin/virtuals` | 虚拟模型组与权重成员 |
| `/admin/checkins` | 自动签到设置与全平台签到记录 |
| `/admin/tasks` | WorkBuddy 任务开关、时间、台账 |
| `/admin/api-keys` | API Key 新增 / 重新生成 / 停用 |
| `/admin/trae/token` | Trae 国际版 Cookie 登录（Refresh Token 备用） |
| `/admin/logs` | 使用日志与 CSV 导出 |
| `/admin/settings` | 运行参数、提示词、可观测与告警、密码 |
| `/admin/models` | 重定向到 `/admin/virtuals` |
| `/admin/accounts` | 重定向到 `/admin/platforms` |
| `/admin/providers` | 重定向到 `/admin/platforms` |

---

## 配置

配置来自 `.env`，部分运行参数可在管理台修改。

环境变量前缀以 `AIALL_*` 为准；历史 `TRAEAPI_*` 仍然生效，`AIALL_*` 优先。

| 变量 | 默认 | 说明 |
|---|---|---|
| `AIALL_APP_NAME` | `AIAll API` | 应用名 |
| `AIALL_HOST` | `0.0.0.0` | 监听地址 |
| `AIALL_PORT` | `2999` | 端口 |
| `AIALL_DATA_DIR` | `./data` | 数据目录 |
| `AIALL_DB_FILE` | `traeapi.db` | 数据库文件名 |
| `AIALL_ADMIN_USERNAME` | `admin` | 管理员用户名 |
| `AIALL_ADMIN_PASSWORD` | 空 | 首次初始化密码；空则随机生成 |
| `AIALL_SECRET_KEY` | 空 | 凭据加密密钥；空则生成 `data/secret.key` |
| `AIALL_SESSION_HOURS` | `72` | 管理台会话有效期 |
| `AIALL_ALLOW_ANY_KEY` | `0` | 允许任意 API Key（不建议） |
| `AIALL_TRUST_PROXY` | `1` | 信任 `X-Forwarded-For` |
| `AIALL_DISABLE_SCHEDULER` | `0` | 关闭内置调度器 |
| `AIALL_DEFAULT_MODEL` | `deepseek-v4.1-flash` | 默认模型 |
| `AIALL_CHECKIN_TIME` | `10:05` | 每日签到时间（北京时间） |
| `AIALL_AUTO_CHECKIN` | `1` | 自动签到开关 |
| `AIALL_LOG_RETENTION_DAYS` | `30` | 使用日志保留天数 |
| `AIALL_MAX_CONCURRENT_PER_ACCOUNT` | `2` | 每账号最大并发（1–8） |
| `AIALL_UPSTREAM_TIMEOUT` | `180` | 上游读超时秒 |
| `AIALL_FIRST_EVENT_TIMEOUT` | `60` | 首事件超时秒 |
| `AIALL_IDE_VERSION` | `3.4.0` | Trae 客户端版本 |
| `AIALL_IDE_VERSION_CODE` | `20260716` | Trae 客户端版本号 |
| `AIALL_APP_ID` | 内置 | Trae OAuth App ID |
| `AIALL_CLIENT_ID` | 内置 | Trae OAuth Client ID |
| `AIALL_OAUTH_HOST` | `https://api.trae.cn` | Trae OAuth 主机 |
| `AIALL_AGENT_HOST` | `https://trae-api-cn.mchost.guru` | Trae Agent 主机 |
| `AIALL_UG_HOST` | `https://api.trae.cn` | Trae 用户中心主机 |
| `AIALL_CONSOLE_HOST` | `https://www.trae.cn` | Trae 控制台主机 |
| `AIALL_TRAE_CALLBACK_PORT` | `57321` | Trae 回调地址端口 |

管理台可改：默认模型、签到开关与时间、日志保留、并发、超时、系统提示词、公网地址、任意 Key 开关、可观测与告警参数。

---

## 可观测与告警

三项能力默认关闭，均在「设置 → 可观测与告警」中开启。

### Prometheus `/metrics`

- `metrics.enabled=1` 后暴露 `GET /metrics`，使用与 `/v1/status` 相同的 API Key 鉴权
- 文本格式 Prometheus 0.0.4，标签与序列顺序固定，连续抓取可直接 diff
- 池指标按 `realm="cn|global|all"` 拆分：账号规模/状态、在途请求、会话粘性、成本探索、WAF/IP 拦截信号
- 模型指标（当日，北京时间）：成功/失败、流式请求、prompt/completion/total token、缓存命中、积分消耗、TTFT、端到端耗时、生成吞吐
- 任务指标：最近一轮时间戳、总数、成功、失败、跳过、是否全灭
- 只暴露聚合量，不含账号 uid、token、cookie、API Key 等敏感维度
- 默认同时输出 `traeapi_*` / `wb2api_*` 别名，可用 `metrics.legacy_prefix=0` 关闭

### 可用性阈值告警

- `alerting.enabled=1` 后每 `alerting.interval_seconds`（默认 30s）评估一次
- 规则：CN/Global 健康账号不足、熔断账号超限、WAF/IP 拦截信号
- `alerting.min_healthy_cn`、`alerting.min_healthy_global`、`alerting.breaker_threshold`
- 边沿触发 + 迟滞：连续 `alerting.for_ticks` 拍才投递；连续 `alerting.clear_ticks` 拍才恢复
- `alerting.startup_grace_seconds` 默认 30s，避开启动初期空池假告警
- `alerting.send_resolve=1` 时发送恢复通知
- 投递到 `alerting.webhook_url`，超时 `alerting.timeout_seconds`
- 配置 `alerting.secret` 后对请求体做 HMAC-SHA256，请求头 `X-WB2A-Signature: sha256=<hex>`
- 通知内容只含服务名、规则、realm、计数、阈值、时间，不含任何凭证

### 管理操作审计

- `admin.audit_enabled=1` 且 `admin.enabled=1` 时，`/admin` 下通过鉴权的写操作追加 JSONL
- 默认文件 `./data/admin_audit.log`
- 字段：`ts`、`time`、`action`、`object`、`status`、`ok`、`ip`、`api_key_fp`、`reason`、`method`、`path`
- 只记录已通过管理员会话鉴权的请求；`api_key_fp` 为 SHA-256 前缀，不可反推原文
- 每条记录独立开-写-关，外部 logrotate 后自动写新文件

### 使用日志的 `error` 行

- 虚拟模型组按成员顺序 failover，**每个失败成员各写一条 `status=error`**，因此一次最终成功的客户端请求也可能伴随多条 error 行；判断请求是否真正失败要看同一时间窗内是否有对应的 `ok`
- 日志 `model` 列是真实上游模型 ID（如 `dfmodel`），`provider:` / `virtual:` 是平台与虚拟组名；管理台虚拟模型页的 `cn:` / `global:` 前缀只是 UI 展示，不是模型名
- `days=0` 表示不加时间过滤，即显示全部历史
- Qoder 网关会把业务错误包成 **HTTP 200 + `statusCodeValue>=400` 的 SSE 帧**（`code/type/message`，而非 OpenAI 的 `error` 字段），并附带 `event:error` 的 Java 异常帧；适配器会把这类帧解析成 `ProviderError`，在日志中保留 `HTTP <code>` 与上游原始 `message/details`，不会再退化成「全部账号失败」
- Qoder 的 `[DONE]` 包在 wrapper 的 `body` 里，适配器会转换成标准 `data: [DONE]` 终止帧
- 上游返回 200 但完全没有可用内容帧时，适配器抛 `ProviderError("...返回空流...")`，账号计入 `mark_failure` 并进入冷却，避免坏成员被无限重试刷屏
- 排障脚本：`python scripts/diagnose_qoder_stream.py qoder-cn dfmodel --via-adapter`，或加 `--variants` / `--concurrency` / `--payload-chars` 复现上游行为；脚本只读、不写库、不输出任何账号凭据

---

## 安全

- 管理员密码 PBKDF2-SHA256（260,000 次）
- 管理台会话随机 Token，数据库只存 SHA-256
- 所有管理台 POST 校验 CSRF
- 上游 access/refresh/session 凭据使用 Fernet 加密后落库
- API Key 同时保存哈希与加密值，便于管理台展示
- 强制路由头、CORS、代理信任均可配置
- FastAPI 自动文档与 OpenAPI schema 默认关闭（`/docs`、`/redoc`、`/openapi.json` 均 404）
- 公网部署请开启 HTTPS，建议加 IP 白名单
- 不要公开 `data/secret.key`、`data/traeapi.db`、Refresh Token、会话 Cookie

---

## 部署

### systemd

```bash
cd /www/wwwroot/AIAllAPI
bash deploy/install.sh
bash deploy/restart.sh
```

### 宝塔面板

完整步骤见 [deploy/btpanel.md](deploy/btpanel.md)。

启动命令：

```bash
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 2999 --proxy-headers
```

或：

```bash
.venv/bin/python run.py
```

### Nginx

流式必须关闭代理缓冲：

```nginx
proxy_buffering off;
proxy_cache off;
proxy_read_timeout 600s;
add_header X-Accel-Buffering no;
```

---

## 兼容与迁移

项目已更名为 **AIAll API**，旧名 Traeapi 保留在兼容点，升级不需要改数据或重配：

| 项目 | 现状 | 说明 |
|---|---|---|
| 环境变量前缀 | `AIALL_*` 优先，`TRAEAPI_*` 仍生效 | 老 `.env` 不用改 |
| 数据库文件 | 仍为 `data/traeapi.db` | 可用 `AIALL_DB_FILE` 改名 |
| 管理台会话 Cookie | `aiall_session`，兼容读取 `traeapi_session` | 升级后需重新登录一次 |
| 强制路由请求头 | `X-AIAll-Provider`，兼容 `X-Traeapi-Provider` | 老客户端不用改 |
| Prometheus 指标 | `aiall_*`，默认同时输出 `traeapi_*` / `wb2api_*` 别名 | 可关闭别名 |
| 部署目录 | 新装默认 `/www/wwwroot/AIAllAPI` | 已有部署可继续用旧目录 |

---

## 目录结构

```text
app/
  api/          OpenAI 兼容接口、管理台路由
  providers/    WorkBuddy / CodeBuddy / LobsterAI / Qoder / Trae / OpenAI / Anthropic 适配
  services/     账号池、聊天转换、路由、日志、签到调度、任务、可观测、鉴权
  upstream/     Trae 客户端、SSE 解析、设备指纹
  templates/    管理台页面
  static/       CSS / JS
scripts/        初始化、自检、冒烟测试、WorkBuddy 成长任务
deploy/         宝塔安装脚本、systemd、Nginx 示例
tests/          单元与接口测试
data/           SQLite、加密密钥、初始凭据（运行时生成）
```

---

## 已知限制

- Trae 官方授权页强制要求回调为 `http://127.0.0.1:<port>/authorize`，因此使用「授权后复制回调地址」的方式
- Trae 国际版使用网页 Cookie 登录，Cookie 过期后需重新复制；Refresh Token 仅作备用
- Qoder 国际版首次签到要求官方客户端真实设备指纹；未在官方客户端领取过且未提供 `checkin_fingerprint` 时可能返回无可用活动
- WorkBuddy 与 CodeBuddy 共用协议，仅 Origin / 域名不同
- 各平台上游协议可能随客户端版本变化，平台地址、UA 与路径均可在管理台配置
- Anthropic 自定义端点支持文本、工具调用、thinking 与 SSE；图片输入会提示省略
- 自定义 OpenAI 端点按 Chat Completions 协议转发，不保证所有厂商的非标准字段
- 企业 SaaS 账号可导入，但签到、积分展示按个人账号逻辑处理，可能显示不可用
- `temperature`、`top_p`、`seed`、`max_tokens` 等采样参数不会透传到 Trae 上游
- `stop` 在本地截断正文，上游仍可能继续消耗额度
- Responses 接口为兼容层，不提供 web search / file search / code interpreter 等托管工具
- 本项目仅供个人学习与研究，使用风险自负

---

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
