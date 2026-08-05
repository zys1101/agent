# 云端—本地 API 契约（草案）

- **文档编号**：API-QUOTE-001
- **版本**：0.1.0-draft
- **协议**：HTTPS + JSON UTF-8
- **认证**：Agent Bearer Token
- **关联文档**：[Agent 工作流](05-agent-workflow.md)

> **特别注意 1：云端不得反向连接本地电脑。** 本地 Worker 主动向腾讯云 HTTPS API 领取任务。
>
> **特别注意 2：所有完成回传必须幂等。** 网络重试不能创建多份报价。
>
> **特别注意 3：COS 文件 URL 必须是私有桶短期签名 URL，建议有效期 15 分钟。**

## 1. 全局约定

### 1.1 Base URL

```text
https://<你的腾讯云网站域名>/api/internal/agent
```

> 域名尚未提供，不能虚构。部署时应写入本地 `.env` 的 `CLOUD_API_BASE_URL`。

### 1.2 请求头

```http
Authorization: Bearer <AGENT_API_TOKEN>
Content-Type: application/json
X-Agent-Id: office-4070super-01
X-Agent-Version: 0.1.0
X-Request-Id: <uuid-v4>
```

### 1.3 错误格式

```json
{
  "error": {
    "code": "TASK_NOT_FOUND",
    "message": "Task does not exist.",
    "request_id": "uuid"
  }
}
```

### 1.4 HTTP 错误码

| HTTP | 错误代码示例 | Worker 行为 |
|---:|---|---|
| 400 | `VALIDATION_ERROR` | 不重试，修复请求 |
| 401 | `UNAUTHORIZED` | 停止 Worker，检查 Token |
| 403 | `AGENT_FORBIDDEN` | 停止 Worker，联系管理员 |
| 404 | `TASK_NOT_FOUND` | 不重试，记录告警 |
| 409 | `TASK_LEASE_CONFLICT` | 放弃本任务，重新领取 |
| 410 | `SIGNED_URL_EXPIRED` | 重新领取/刷新文件 URL |
| 422 | `RESULT_SCHEMA_INVALID` | 修复结果后重传 |
| 429 | `RATE_LIMITED` | 按 `Retry-After` 等待 |
| 500/502/503 | `SERVER_ERROR` | 指数退避重试 |

## 2. 领取任务

### `POST /tasks/claim`

**用途**：本地 Worker 请求一个可处理的排队任务。无任务时返回 `204 No Content`。

请求：

```json
{
  "agent_id": "office-4070super-01",
  "agent_version": "0.1.0",
  "capabilities": ["pdf_parser", "image_ocr", "excel_parser", "local_llm", "quote_engine"],
  "max_file_size_mb": 100
}
```

成功响应 `200`：

```json
{
  "task_id": "qt_01JABCDEF",
  "project_id": "prj_01JABCDEF",
  "lease_token": "opaque-secret",
  "lease_expires_at": "2026-08-05T16:30:00+08:00",
  "customer_form": {
    "project_name": "气动压装工装设计",
    "customer_description": "设计一套用于电机壳体压装的工装。",
    "deadline_date": "2026-08-20",
    "requested_deliverables": ["3D模型", "2D工程图", "BOM"],
    "currency": "CNY"
  },
  "files": [
    {
      "file_id": "file_01",
      "original_name": "requirements.pdf",
      "mime_type": "application/pdf",
      "size_bytes": 2456789,
      "download_url": "https://cos.example.com/signed-url",
      "download_url_expires_at": "2026-08-05T16:15:00+08:00",
      "sha256": "optional-sha256"
    }
  ],
  "rule_set_version": "0.1.0-draft"
}
```

规则：

- 云端将任务置为 `claimed`，默认租约 30 分钟；
- 同一任务同一时刻只能被一个 Agent 领取；
- Worker 无任务时应等待 15 秒后再次领取；
- 文件超过 Worker `max_file_size_mb` 时不应分配该任务。

## 3. 心跳与续租

### `POST /tasks/{task_id}/heartbeat`

请求：

```json
{
  "lease_token": "opaque-secret",
  "stage": "extracting_requirements",
  "progress_percent": 55,
  "message": "已完成资料提取，正在生成结构化需求。"
}
```

响应：

```json
{
  "task_id": "qt_01JABCDEF",
  "lease_expires_at": "2026-08-05T16:45:00+08:00"
}
```

规则：

- Worker 每 60 秒发送一次心跳；
- 当租约剩余少于 10 分钟时，云端续租 30 分钟；
- `progress_percent` 仅允许 0–100 的整数；
- 租约失效后 Worker 不得再回传完成结果。

## 4. 回传成功结果

### `POST /tasks/{task_id}/complete`

必需头：

```http
Idempotency-Key: qt_01JABCDEF
```

请求骨架：

```json
{
  "lease_token": "opaque-secret",
  "result_schema_version": "1.0",
  "agent": {
    "agent_id": "office-4070super-01",
    "agent_version": "0.1.0"
  },
  "runtime": {
    "ollama_model": "configured-local-model",
    "rule_set_version": "0.1.0-draft",
    "prompt_versions": {
      "requirement_extraction": "v1",
      "classification": "v1",
      "review": "v1"
    }
  },
  "result": {
    "project_type": "pneumatic_press_fixture",
    "completeness_score": 0.73,
    "classification_confidence": 0.82,
    "estimated_hours": {"total": 124},
    "price": {"currency": "CNY", "minimum": 42000, "recommended": 48000, "maximum": 56000},
    "manual_review_required": true,
    "manual_review_reasons": ["关键接口未确认"],
    "missing_information": [],
    "assumptions": [],
    "exclusions": [],
    "calculation_snapshot": {}
  }
}
```

成功响应：

```json
{
  "task_id": "qt_01JABCDEF",
  "status": "completed",
  "quote_id": "quote_01JABCDEF",
  "project_status": "pending_review"
}
```

规则：

- `result` 必须通过云端 JSON Schema 校验；
- 相同 `Idempotency-Key` 重复提交应返回同一 `quote_id`；
- 云端不得信任客户端提交的“已批准”状态；是否审批只能由云端后台人员决定；
- `calculation_snapshot` 必须保存，但不默认展示给客户。

## 5. 回传失败

### `POST /tasks/{task_id}/fail`

```json
{
  "lease_token": "opaque-secret",
  "stage": "parsing_files",
  "retryable": true,
  "error_code": "OCR_SERVICE_UNAVAILABLE",
  "message": "OCR service did not respond within 180 seconds."
}
```

云端规则：

- 可重试任务：指数退避，最多自动尝试 3 次；
- 不可重试任务：状态为 `failed`，通知内部人员；
- 不得把本地堆栈、Token 或签名 URL 原样返回给客户页面。

## 6. 获取当前规则版本（可选）

### `GET /rules/active`

响应：

```json
{
  "rule_set_version": "0.1.0-draft",
  "sha256": "rule-file-hash",
  "download_url": "https://cos.example.com/signed-rule-url",
  "expires_at": "2026-08-05T16:15:00+08:00"
}
```

> MVP 可先让规则仅在本地维护；一旦云端开始下发规则，必须进行版本签名/哈希校验，避免 Worker 使用被篡改的规则文件。
