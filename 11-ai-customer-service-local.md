# AI 智能客服接口文档（本地 AI Agent 版）

- **文档编号**：API-AI-CS-002
- **版本**：1.0
- **协议**：HTTPS + JSON UTF-8
- **认证**：Worker 接口使用 `Authorization: Bearer <AGENT_API_TOKEN>` + `X-Agent-Id`；网站侧公开接口无需登录
- **关联代码**：本仓库 `src/cs_agent/`、`scripts/run_cs_worker.py`；后端 `backend-api/src/modules/ai-service/local/`
- **直连 API 版**：见 [10-ai-customer-service-api.md](10-ai-customer-service-api.md)

## 1. 架构

与报价 Agent（quote_agent）同一套「云端任务队列 + 本地 Worker」模式：

```text
用户(官网 /ai-service，选择"本地 AI Agent")
  → POST /api/v1/ai-service/local/chat   （后端创建 AiServiceTask，状态 queued）
  → 本地 Worker 轮询 POST /api/v1/internal/ai-service/tasks/claim  领取任务
  → Worker 调用本地 Ollama 模型生成回复
  → Worker POST /tasks/{id}/complete 回传 reply + suggestions
  → 前端轮询 GET /api/v1/ai-service/local/tasks/{id} 直到 completed，渲染回复
```

## 2. 网站侧接口（公开）

### `POST /api/v1/ai-service/local/chat`

**请求体**

```json
{
  "message": "如何发布设计需求？",
  "sessionId": "cs_1b2m3n4p5q",
  "history": [{ "role": "user", "content": "你们平台是做什么的？" }]
}
```

**响应**

```json
{
  "code": 200,
  "message": "success",
  "data": { "task_id": "cs_xxx", "session_id": "cs_1b2m3n4p5q", "status": "queued" }
}
```

### `GET /api/v1/ai-service/local/tasks/{task_id}`

**响应**

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "task_id": "cs_xxx",
    "status": "completed",
    "reply": "您好，发布设计需求非常简单：……",
    "suggestions": ["AI 智能报价怎么用？", "平台有哪些设计服务？"],
    "error": null
  }
}
```

`status`：`queued` → `claimed` → `completed`；失败为 `failed` / `retryable_failed`，此时 `error` 非空。前端建议每 1.5~2 秒轮询一次。

## 3. Worker 接口（internal，Bearer 鉴权）

Base URL：`https://<你的腾讯云网站域名>/api/v1/internal/ai-service`

请求头：

```http
Authorization: Bearer <AGENT_API_TOKEN>
Content-Type: application/json
X-Agent-Id: office-4070super-01
X-Agent-Version: 0.1.0
X-Request-Id: <uuid-v4>
```

### `POST /tasks/claim`

请求：

```json
{ "agent_id": "office-4070super-01", "agent_version": "0.1.0", "capabilities": ["local_llm", "chat"] }
```

无任务时返回 `204 No Content`；有任务时返回：

```json
{
  "task_id": "cs_xxx",
  "session_id": "cs_1b2m3n4p5q",
  "lease_token": "opaque-secret",
  "lease_expires_at": "2026-08-06T16:30:00+08:00",
  "customer_form": {
    "message": "如何发布设计需求？",
    "history": []
  }
}
```

### `POST /tasks/{task_id}/heartbeat`（可选）

```json
{ "lease_token": "opaque-secret", "stage": "generating", "progress_percent": 50, "message": "正在生成回复" }
```

### `POST /tasks/{task_id}/complete`

必需头：`Idempotency-Key: <task_id>`

```json
{
  "lease_token": "opaque-secret",
  "result_schema_version": "1.0",
  "agent": { "agent_id": "office-4070super-01", "agent_version": "0.1.0" },
  "runtime": {
    "ollama_model": "qwen3-vl:8b",
    "ollama_host": "127.0.0.1:11434",
    "temperature": 0.6,
    "max_reply_tokens": 500
  },
  "result": {
    "reply": "您好，发布设计需求非常简单：……",
    "suggestions": ["AI 智能报价怎么用？"]
  }
}
```

响应：`{ "task_id": "cs_xxx", "status": "completed", "session_id": "cs_1b2m3n4p5q" }`

### `POST /tasks/{task_id}/fail`

```json
{
  "lease_token": "opaque-secret",
  "stage": "generating",
  "retryable": true,
  "error_code": "LLM_UNAVAILABLE",
  "message": "Ollama did not respond"
}
```

## 4. 错误码

| HTTP | `error.code` | Worker 行为 |
|---:|---|---|
| 400 | `VALIDATION_ERROR` | 不重试，修复请求 |
| 401 | `UNAUTHORIZED` | 停止 Worker，检查 Token |
| 403 | `AGENT_FORBIDDEN` | 停止 Worker，联系管理员 |
| 404 | `TASK_NOT_FOUND` | 不重试，记录告警 |
| 409 | `TASK_LEASE_CONFLICT` | 放弃本任务，重新领取 |
| 422 | `RESULT_SCHEMA_INVALID` | 修复结果后重传 |
| 429/500/502/503 | `SERVER_ERROR` | 指数退避重试 |

## 5. 本地运行

### 5.1 依赖

- Python >= 3.11，已安装本仓库依赖（`pip install -r requirements.txt`）。
- 本机/局域网可访问的 Ollama，并已拉取模型（默认 `qwen3-vl:8b`，可通过 `OLLAMA_MODEL` 换成 `qwen3`、`deepseek-r1` 等）。

### 5.2 不连云端，先试模型

```bash
python scripts/run_cs_worker.py --local-test "如何发布设计需求？"
```

### 5.3 全链路演示（mock 云端）

```bash
python scripts/run_cs_worker.py --demo
```

### 5.4 对接真实后端

在 `.env` 配置：

```dotenv
CLOUD_API_BASE_URL=https://<你的腾讯云网站域名>/api/v1/internal/ai-service
AGENT_ID=office-4070super-01
AGENT_API_TOKEN=<与后端 AGENT_TOKEN_PLAIN 一致>
OLLAMA_HOST=127.0.0.1
OLLAMA_PORT=11434
OLLAMA_MODEL=qwen3-vl:8b
CS_POLL_INTERVAL=2
```

然后启动：

```bash
python scripts/run_cs_worker.py --once   # 处理一个任务后退出
python scripts/run_cs_worker.py          # 常驻轮询
```

### 5.5 后端准备（一次性）

```bash
cd E:\Appdata\Web\backend-api
npm run prisma:generate
npx prisma db push          # 或 npx prisma migrate dev --name add_ai_service_task
```

`.env` 中配置 Agent 凭证（服务启动时自动写入 `agent_credential`，幂等）：

```dotenv
AGENT_ID=office-4070super-01
AGENT_TOKEN_PLAIN=<与 Worker 的 AGENT_API_TOKEN 一致>
```

## 6. 前端切换

聊天页 `/ai-service` 顶部「智能模式」切换：

- **DeepSeek API**：走 `POST /ai-service/chat`（直连云端）。
- **本地 AI Agent**：走 `POST /ai-service/local/chat` + 轮询任务状态；需要本地 Worker 正在运行。

前端封装见 `frontend-client/src/api/aiService.ts`。

## 7. 已知限制

- 聊天是任务队列模式，回复延迟 = 本地模型生成耗时 + 轮询间隔（1.5~2s），适合"试跑/自托管"场景；要求实时流式回复时可改为 HTTP/SSE 直连模式。
- 本地模型回答质量依赖所选模型与系统提示词（`src/cs_agent/service.py` 的 `SYSTEM_PROMPT` 与后端直连版一致）。
- 未内置敏感词过滤与频控，上线前建议补充。
