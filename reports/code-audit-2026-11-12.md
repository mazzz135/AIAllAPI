# AIAll API 代码审计报告

- 审计时间：2026-11-12 15:20（Asia/Shanghai）
- 审计范围：`app/`、`tests/`、`scripts/`（不含第三方脚本内部逻辑）
- 工具：ruff 0.17.4（F/E/B/W 规则）、compileall、自研 AST 死代码扫描、路由/模板/配置项一致性扫描、pytest

## 一、结论

| 类别 | 发现 | 已修复 |
|---|---|---|
| 运行时 Bug | 4 | 4 |
| 逻辑缺陷（校验失效 / 重复请求） | 2 | 2 |
| 死代码（未被调用函数） | 15 | 15 |
| 未使用导入 | 21 | 21 |
| 未使用变量 | 4 | 4 |
| 重复定义（测试函数） | 10 | 10 |
| 重复字典键 | 2 | 2 |
| 废弃数据库表 | 1 | 未删除（仅停止使用，保留数据） |

修复后：`ruff check --select F,E9` 由 29 条告警降为 0；`pytest` 78 passed。

## 二、运行时 Bug

### B1 `_oauth_error` 未定义（P0，会导致 NameError）

- 位置：`app/upstream/client.py:294`、`:347`
- 影响：Trae 网页授权换取令牌 / 获取用户信息时，若所有候选主机都失败，代码调用不存在的 `_oauth_error()`，抛 `NameError` 而不是可读错误。用户看到的是 500，日志里是 NameError。
- 修复：新增 `_oauth_error(detail)`，把「未知后端路径 / 401 / 超时 / 其他」映射成中文提示。
- 回归：`test_trae_oauth_failure_returns_friendly_error`

### B2 前端 `js-toast` 事件监听重复注册（P1，导致重复提交）

- 位置：`app/static/app.js`（两个完全相同的 `form.js-toast` 监听块）
- 影响：签到、摘除流量、恢复流量等按钮每点一次会发**两次** POST 请求，上游被重复写、签到记录重复触发。
- 修复：删除重复监听块。
- 回归：`grep -c form.js-toast app/static/app.js` = 1

### B3 虚拟模型重名校验查的是废弃表（P1，校验恒不生效）

- 位置：`app/services/virtuals.py` `create()`
- 影响：原实现 `SELECT 1 FROM models WHERE lower(name)=?`，而 `models` 表没有任何写入（模型实际存于 `providers.models_json`），因此该校验永远通过；创建与真实模型同名的虚拟模型会污染路由。
- 修复：改为校验 `virtual_models` + 各平台 `models_json` 的 `id` / `display_name`（忽略大小写）。
- 回归：`test_virtual_model_name_collision_check`

### B4 人工补跑模型同步无效（P2）

- 位置：`app/services/scheduler.py`
- 影响：`POST /admin/tasks/model_refresh/run` 时，`_refresh_models` 仍受 `models_auto_refresh` 开关和「今天已同步」限制，开关关闭或当天已跑过时，接口返回 202 但实际什么都没做。
- 修复：新增 `force=True` 参数，人工补跑绕过开关与当日去重。

### B5 模型同步的自动重试永远不触发（P2）

- 位置：`app/services/scheduler.py` `_refresh_models`
- 影响：任务全灭时 `record_task_run` 会排重试，但 `_refresh_models` 在「今天已同步」处直接 return，重试到点也不会执行。
- 修复：当日已执行时先检查 `_retry_due("model_refresh")`，到期则允许再跑一次。

### B6 平台模型同步只尝试第一个账号（P2，健壮性）

- 位置：`app/services/providers.py` `sync_models`
- 影响：`accounts[0]` 失效时整平台同步失败，即使其他账号可用。
- 修复：按账号顺序依次尝试，全部失败才记录 `last_error` 并抛错。

## 三、无效代码

### 3.1 删除的未调用函数（15 个）

| 文件 | 函数 |
|---|---|
| `app/upstream/client.py` | `_credits_from_usage` |
| `app/services/usage.py` | `delete_key`、`update_credit_result` |
| `app/services/accounts.py` | `parse_callback_url`、`device_preview` |
| `app/services/pool.py` | `explore_stats` |
| `app/services/virtuals.py` | `get_group` |
| `app/bootstrap.py` | `get_setting_int`、`get_setting_bool` |
| `app/providers/qoder.py` | `models_url` |
| `app/services/providers.py` | `add_account`、`delete_provider`、`record_error` |
| `app/providers/platforms.py` | `require_adapter` |
| `app/db.py` | `transaction` |

判定方式：AST 提取全部函数定义，统计全仓库（含模板）标识符引用数，仅定义处出现 1 次即判定未使用，并逐个 grep 复核。

### 3.2 未使用导入（21 处）

主要涉及 `csv`、`io`、`StreamingResponse`、`JSONResponse`、`RedirectResponse`、`uuid`、`Path`、`extract_usage`、`typing.Any/Optional`、`asyncio`、`timedelta` 等，均由 ruff `F401` 自动清理。

### 3.3 未使用变量（4 处）

- `app/api/admin.py` `_manual_action`：`table`（拼接后未使用）
- `scripts/workbuddy_tasks/school_open_day_2026.py`：`by_code`
- `tests/test_core.py`：2 处赋值未使用

### 3.4 重复定义

- `app/bootstrap.py`、`app/api/admin.py`：`"metrics.legacy_prefix"` 字典键重复（后者覆盖前者，值相同，无功能影响）
- `tests/test_core.py`：10 个测试函数重复定义（同名后者覆盖前者），已去重

### 3.5 废弃数据库表（未删除）

- `models` 表：schema 保留，但全仓库无任何 `INSERT/UPDATE/DELETE`，仅原 `virtuals.create()` 误用其做校验（已改为查 `models_json`）。
- 建议：确认无历史数据依赖后再 `DROP TABLE models`，本次不动数据。

## 四、检查后确认无问题的项

| 检查项 | 结果 |
|---|---|
| 重复路由 | 仅 `/models` 在 `admin`（`/admin/models`）与 `openai`（`/v1/models`）各注册一次，前缀不同，非冲突 |
| 未鉴权管理接口 | 仅 `login`、`trae_callback`、`health`、`metrics`（内部校验 Key），符合预期 |
| SQL 注入 | 无字符串拼接 SQL；仅 `db._ensure_column` 使用内部常量表名 |
| 模板缺失 / 多余 | 模板文件与 `_render` 引用完全对应 |
| 模板重复 id | 无 |
| 配置项未使用 | 65 个默认设置项全部有读取点 |
| 数据库表引用 | 除 `models` 外均有读写 |
| 表单字段错配 | 设置页 / 签到页 / 任务页字段与后端处理一致 |
| 异步警告 | `pytest -W error::RuntimeWarning` 通过，无未 await 协程 |

## 五、验证

```text
ruff check --select F,E9      修复前 29 条 → 修复后 0 条
pytest tests/test_core.py     78 passed, 1 warning
服务器部署后                 health=200，78 passed
```

新增回归测试：

- `test_trae_oauth_failure_returns_friendly_error`
- `test_manual_model_refresh_runs_even_when_switch_off`
- `test_all_admin_pages_render`（14 个管理页全部 200）
- `test_virtual_model_name_collision_check`

## 六、后续加固（已完成）

| 项 | 处理 |
|---|---|
| 部署前自检 | 新增 `scripts/preflight.py`：compileall + ruff F/E9 + 测试函数重名，`--with-tests` 追加 pytest |
| 公开回调加固 | Trae 登录回调新增一次性 `state`（`login_sessions.state`），回调按 state 精确匹配；无 state 且存在多个 pending 会话时拒绝；同 IP 5 分钟 20 次限流 |
| 废弃表清理 | 启动时把 `models` 表映射迁移成虚拟模型（保留老别名），全部迁移成功后删除旧表；服务器已执行，`models` 表已删除 |

## 七、后续建议

1. `scripts/workbuddy_tasks/*` 为第三方 MIT 脚本，保留其原始风格（E401/W292 等），不建议改动。
2. 若后续再引入活动类任务，复用 `task_runs` 台账 + `scripts/preflight.py` 自检。
