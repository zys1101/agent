# 后端合并后：本地 AI 报价 Worker 适配方案

> 背景：2026-08-18 已完成 Web 后端 → 小程序后端合并。旧网站后端（`/api/v1`）已停用，
> 唯一后端为小程序后端（`/api/mp/v1`，服务器 `mech-api` / 端口 3001）。
> 本文回答：**本地 AI 报价 Worker（`E:\Appdata\agent\agent`）是否需要修改、改哪里、怎么验证。**

---

## 1. 结论（先说结果）

**需要修改，改动量很小：**

| 项 | 结论 | 工作量 |
|---|---|---|
| Worker 连接地址（`CLOUD_API_BASE_URL`） | **必改**：`/api/v1` → `/api/mp/v1` | 配置 1 行 |
| Worker `.env`（AGENT_API_TOKEN 等） | **必改**：需按合并后服务端 token 对齐（本地当前无 `.env`） | 配置 |
| 后端 `claim` 响应（`agent.service.ts`） | **必改**：缺 `project_id`、`task_id`/`file_id` 为数字，Worker 领取即解析失败 | 后端 3 行 |
| 后端 `complete` 幂等（可选） | **建议改**：网络重试会 409，与“回传必须幂等”契约不符 | 后端 5 行 |
| Worker 报价/回传业务逻辑 | **不用改** | — |

结论一句话：**改 1 个环境变量 + 后端 `claim` 返回 3 处字段，Worker 即可对接合并后端继续报价。**

---

## 2. 现状核对：Worker 与合并后端的协议差异

### 2.1 Worker 连接谁

- Worker 源码：`E:\Appdata\agent\agent`（Python，Docker 运行）
- 客户端：`src/quote_agent/cloud.py` 的 `CloudClient`
- 配置：`docker-compose.yml` 中 `CLOUD_API_BASE_URL: ${CLOUD_API_BASE_URL:-http://mock-cloud:8000/api/v1/internal/agent}`
- 本地当前**没有 `.env` 文件**（生产连接地址从未落盘，默认值指向本地 Mock）

合并前 Worker 对接的是网站后端：`https://<域名>/api/v1/internal/agent`
合并后唯一后端：`https://www.sonsentech.cn/api/mp/v1/internal/agent`

### 2.2 协议逐项核对表

| 项目 | Worker 发送（契约要求） | 合并后端 | 结论 |
|---|---|---|---|
| 请求头 | `Authorization: Bearer <token>` + `X-Agent-Id` | `AgentAuthGuard` 同样校验这两项 | ✅ 兼容 |
| `POST /tasks/claim` 请求体 | `agent_id`/`agent_version`/`capabilities`/`max_file_size_mb` | `ClaimDto` 未声明 `agent_id`，`whitelist` 会剔除该字段，不报错 | ✅ 兼容 |
| `claim` 响应 | `task_id: str`、`project_id: str`、`lease_token`、`lease_expires_at`、`customer_form`、`files[]`、`rule_set_version` | 后端返回 `task_id: Number`、**无 `project_id`**、`file_id: Number` | ❌ **不兼容**，Worker Pydantic 校验会失败 |
| `POST /tasks/:id/heartbeat` | `lease_token`/`stage`/`progress_percent`/`message` | `HeartbeatDto` 字段一致 | ✅ 兼容 |
| `POST /tasks/:id/complete` | `lease_token`/`result_schema_version`/`agent`/`runtime`/`result` | `CompleteDto` 字段一致 | ✅ 兼容 |
| `complete` 幂等（`Idempotency-Key`） | 契约要求重试不产生重复报价 | 后端忽略该头，重复回传会 409 | ⚠️ 建议补幂等 |
| `POST /tasks/:id/fail` | `lease_token`/`stage`/`retryable`/`error_code`/`message` | `FailDto` 字段一致 | ✅ 兼容 |
| 结果 schema | `price.{currency,minimum,recommended,maximum}`、`estimated_hours.total`、`project_type` 等 | 后端 `mapWorkerResult` 按此读取 | ✅ 兼容 |

### 2.3 后端对 Worker 结果的消费路径（已确认无需改动）

1. 小程序/网站前端调 `POST /api/mp/v1/ai/quote`
2. 后端 `AiService.quoteViaQueue`：创建 DRAFT 需求 + `ai_quotes` 记录 + 入队 `quote_tasks`
3. 本地 Worker `claim` → 处理 → `complete`
4. 后端轮询到 `completed` 后，用 `mapWorkerResult` 把 `result` 映射为 `price_min/price_max` 回填 `ai_quotes`

Worker 的 `price` 结构与 `mapWorkerResult` 期望完全一致（`minimum/recommended/maximum`），**回传链路不需要改业务代码**。

---

## 3. 修改清单

### 3.1 必改：Worker 配置（本地 `E:\Appdata\agent\agent\.env`）

当前本地没有 `.env`，生产运行需新建（或按现有部署方式注入环境变量）：

```dotenv
# ---- 云端（合并后唯一后端）----
CLOUD_API_BASE_URL=https://www.sonsentech.cn/api/mp/v1/internal/agent
AGENT_ID=office-4070super-01
AGENT_API_TOKEN=<与服务器 /var/www/shared-applet/.env 的 AGENT_API_TOKEN 一致>

# ---- Ollama（不变）----
OLLAMA_HOST=host.docker.internal
OLLAMA_PORT=11434
OLLAMA_MODEL=qwen3-vl:8b
TEXT_MODEL=qwen2.5:7b-instruct
EMBEDDING_MODEL=bge-m3:latest

# ---- Worker（不变）----
PRICING_MODE=ai_quote
RAG_ENABLED=false
LLM_CACHE_ENABLED=true
LLM_THINK=false
LLM_MAX_TOKENS=4096
WORK_DIR=./data/incoming
SQLITE_PATH=./data/agent.db
LOG_LEVEL=INFO
```

> 注意：
> - `AGENT_API_TOKEN` 必须与服务器 `/var/www/shared-applet/.env` 的 `AGENT_API_TOKEN` 完全一致（合并后以后者为准）。
> - 如 Worker 部署在别的机器，把域名换成实际可达的公网地址即可（`/api/mp/v1/internal/agent` 前缀保持不变）。
> - 服务器端对应配置键：`AGENT_ID`、`AGENT_API_TOKEN`、`LEASE_MINUTES`、`MAX_RETRY_ATTEMPTS`、`RETRY_BACKOFF_SECONDS`、`RULE_SET_VERSION`（已在 `/var/www/shared-applet/.env` 中）。

### 3.2 必改：后端 `claim` 响应协议兼容（`backend/src/ai/agent/agent.service.ts`）

**问题**：Worker 的 `ClaimedTask` 要求 `task_id: str`、`project_id: str`、`files[].file_id: str`；
后端当前返回数字 id 且缺少 `project_id`，Worker 领取时 Pydantic 校验失败。

**改动**（`AgentService.claim` 的返回值）：

```ts
return {
  task_id: String(candidate.id),                    // 原为 Number
  project_id: String(candidate.quote.requirementId), // 新增（ai_quotes.requirement_id）
  quote_id: Number(candidate.quote.id),
  lease_token: leaseToken,
  lease_expires_at: leaseExpiresAt.toISOString(),
  customer_form: { ... },                            // 不变
  files: images.map((url, i) => ({
    file_id: String(i + 1),                          // 原为 Number
    original_name: ...,
    mime_type: ...,
    size_bytes: 0,
    download_url: url,
    download_url_expires_at: null,
  })),
  rule_set_version: this.str('ruleSetVersion', '1.0.0-ai-quote'),
};
```

**备选方案**（不想动后端时，改 Worker 的 `src/quote_agent/cloud.py`）：

```python
class ClaimedTask(BaseModel):
    task_id: str | int
    project_id: str = ""          # 缺省兼容
    lease_token: str
    lease_expires_at: str
    customer_form: dict[str, Any]
    files: list[TaskFile] = Field(default_factory=list)
    rule_set_version: str = ""

class TaskFile(BaseModel):
    file_id: str | int
    ...
```

> 推荐改后端：后端代码注释本就声明“与 Web 项目协议兼容”，且 Web 契约明确 `task_id/project_id` 为字符串，改后端对两端 Worker 都更稳。

### 3.3 建议改：后端 `complete` 幂等（`agent.service.ts`）

契约要求“所有完成回传必须幂等”，Web 后端用 `Idempotency-Key` 去重；合并后端的 `AgentService.complete` 未处理，Worker 断网重传时会因任务已 `completed` 收到 409。

建议在 `complete` 开头增加：

```ts
const task = await this.prisma.quoteTask.findUnique({ where: { id: BigInt(taskId) } });
if (!task) throw new NotFoundException('Task does not exist');
if (task.status === QUOTE_TASK_STATUS.COMPLETED && task.leaseTokenHash) {
  // 幂等：已完成且租约匹配 → 直接返回成功
  if (verifyLeaseToken(dto.lease_token, task.leaseTokenHash)) {
    return { task_id: Number(task.id), status: QUOTE_TASK_STATUS.COMPLETED, result_schema_version: dto.result_schema_version };
  }
  throw new UnauthorizedException('Invalid lease token');
}
this.assertLease(task, dto.lease_token);
```

### 3.4 不需要修改的部分（确认清单）

- Worker 的 claim/heartbeat/fail 请求头与请求体
- complete 载荷结构（`result_schema_version`/`agent`/`runtime`/`result`）
- Worker 报价业务逻辑（`ai_quote.py`、`worker.py`、规则引擎）
- 后端 `mapWorkerResult` 与 AI 报价结果回填链路
- 小程序/网站前端（AI 报价页已指向 `/api/mp/v1/ai/quote`）

---

## 4. 修改后的验证步骤

### 4.1 后端

```powershell
cd E:\Appdata\mech-design-platform\backend
npm run build
.\..\docs\shell\backend.ps1        # 部署合并后端（冒烟测试 + pm2 切换）
```

### 4.2 Worker 本地

```powershell
cd E:\Appdata\agent\agent
# 1. 创建 .env（见 3.1），再启动 Worker（先 --once 验证单个任务）
docker compose run --rm -e PRICING_MODE=ai_quote quote-agent python scripts/run_worker.py --once
# 2. 确认日志：claimed → parsed → ai_quote → completed
# 3. 常驻运行
docker compose run --rm -d quote-agent python scripts/run_worker.py
```

### 4.3 端到端

1. 小程序或网站发起一次 AI 报价（`POST /api/mp/v1/ai/quote`）
2. 观察 Worker 日志出现任务处理；后端 `quote_tasks` 状态流转 `queued → claimed → completed`
3. 前端拿到 `price_min/price_max`，`ai_quotes` 记录已回填

### 4.4 快速自检（无需前端）

```bash
# 服务器上确认任务队列与 Worker 鉴权配置
grep -E '^(AGENT_ID|AGENT_API_TOKEN|LEASE_MINUTES|RULE_SET_VERSION)' /var/www/shared-applet/.env
# 模拟 Worker 领取（返回 204 表示无任务；返回 200 即协议打通）
curl -s -X POST https://www.sonsentech.cn/api/mp/v1/internal/agent/tasks/claim \
  -H "Authorization: Bearer <AGENT_API_TOKEN>" -H "X-Agent-Id: office-4070super-01" \
  -H "Content-Type: application/json" \
  -d '{"agent_id":"office-4070super-01","agent_version":"0.1.0","max_file_size_mb":100}'
```

---

## 5. 回滚与注意事项

- **回滚**：后端改动仅 2 处（claim 字段 + complete 幂等），保留上一版 release 即可 `backend.ps1` 无参重部署回退；Worker 仅改环境变量，无代码回滚负担。
- **旧 Web 报价数据**：`quote_project` / `quote_task` / `quote` 等表已原样迁入合并库（386 条），但合并后 Worker 只消费小程序 `quote_tasks`（AI 报价队列），不再处理旧 Web 报价任务；如需保留旧数据仅作归档，无需额外操作。
- **`AI_QUOTE_MODE`**：服务器 `.env` 当前为 `queue`（本地 Worker 任务制）。若暂时不想依赖本地 Worker，可临时改为 `mock`（规则引擎直出价格）或 `direct`（后端直连 Ollama/OpenAI 兼容接口），改后重启 `mech-api` 即可，无需部署。
- **安全**：`AGENT_API_TOKEN` 属于敏感配置，本文不落明文；以服务器 `/var/www/shared-applet/.env` 实际值为准。

---

## 附录：协议文档对照

- Worker 契约：`E:\Appdata\agent\agent\06-api-contract.md`
- Worker 结果 schema 说明：`E:\Appdata\agent\agent\09-server-backend-dev.md`、`14-ai-quote-method-handoff.md`
- 合并后端 Agent 模块：`E:\Appdata\mech-design-platform\backend\src\ai\agent\`（`agent.controller.ts` / `agent.service.ts` / `agent.dto.ts` / `result.mapper.ts`）
- 合并后端 AI 报价入口：`E:\Appdata\mech-design-platform\backend\src\ai\ai.service.ts`（`quoteViaQueue`）
