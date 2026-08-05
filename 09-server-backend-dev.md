# 服务器后端开发文档（NestJS）：报价 Agent 接口

- **文档编号**：BE-DEV-001
- **版本**：0.1.0-draft
- **技术栈**：NestJS + TypeScript + MySQL/PostgreSQL + 腾讯云 COS
- **关联文档**：[API 契约](06-api-contract.md)、[Agent 工作流](05-agent-workflow.md)、[报价规则规范](04-quote-rule-specification.md)、[测试验收](08-test-and-evaluation-plan.md)

> **特别注意 1：本后端是报价架构中的"云端"。** 本地 Worker 只会主动出站请求本服务；本服务**永远不反向连接本地工作站**，不需要 FRP。
>
> **特别注意 2：服务端不能信任 Worker 上传的"已批准"状态。** 人工审核只能由后台人员操作；金额、完整度、交期等强制审核条件在落库时必须由服务端按规则再校验一次。
>
> **特别注意 3：本文档只覆盖报价 Agent 接口及其配套能力。** 客户页面、审核后台、客服 Agent（预留 WebSocket 通道，见第 12 节）不在本期范围内。

---

## 1. 本期范围与目标

实现 `06-api-contract.md` 中本地 Worker 需要的全部接口，让"客户提交 -> 任务排队 -> 本地 Worker 处理 -> 报价落库 -> 强制审核 -> 人工批准"这条链路在服务端跑通。

### 1.1 本期交付

- 报价 Agent 四接口：`claim` / `heartbeat` / `complete` / `fail`；
- 项目、任务、报价、审计等基础数据模型；
- Agent Token 鉴权与任务租约；
- COS 私有桶签名 URL 下发与上传限制；
- 报价强制审核的服务端二次校验；
- 幂等回传（同一任务不产生两份报价）。

### 1.2 本期不做

- 客户注册/登录、销售跟进、管理后台页面；
- 正式报价单 PDF、通知推送；
- 客服 Agent（本文档仅预留接口位置）；
- CAD 解析、云端模型推理、自动支付/ERP。

---

## 2. 部署形态（免 FRP）

```text
客户浏览器 ──HTTPS──> 服务器(NestJS + DB + COS) <──HTTPS 出站── 本地 Worker
```

- NestJS 部署在具有公网 IP/域名的服务器上，HTTPS 由 Caddy / Nginx / 云负载均衡终止；
- 本地 Worker 通过 `CLOUD_API_BASE_URL` 访问服务器公网域名；
- 服务器防火墙无需向本机开放任何入站端口；原 FRP 隧道可整体停用；
- GitHub 仓库仅作源码管理，部署方式见第 10 节。

---

## 3. 技术选型建议

| 组件 | 建议 | 说明 |
|---|---|---|
| 框架 | NestJS 10+ | 模块化、自带 Guards/Interceptors |
| 语言 | TypeScript 5.x | strict 模式 |
| ORM | TypeORM 或 Prisma | 本文以 TypeORM 为例，字段兼容两者 |
| 数据库 | PostgreSQL 15+ 或 MySQL 8 | 需要 `FOR UPDATE SKIP LOCKED` 或等价原子更新 |
| COS SDK | `cos-nodejs-sdk-v5` | 私有桶签名、STS 临时密钥 |
| 密码学 | `bcryptjs` | Token 只存哈希 |
| 校验 | `class-validator` + `class-transformer` | DTO 校验 |
| 日志 | NestJS Logger + `pino`（可选） | 脱敏后落盘 |
| 测试 | Jest（NestJS 内置） | 接口与租约/幂等单测 |

---

## 4. 目录结构建议

```text
src/
  main.ts                        # 全局前缀、ValidationPipe、异常过滤器
  app.module.ts
  common/
    decorators/                  # @AgentAuth() 等
    guards/agent-auth.guard.ts   # Bearer Token 校验
    filters/http-exception.filter.ts  # 统一错误格式
  modules/
    agents/                      # Agent 凭证与鉴权
      agent.entity.ts
      agent-token.service.ts
    projects/                    # 客户项目
      project.entity.ts
      project-file.entity.ts
    tasks/                       # 任务、租约、心跳、幂等、重试
      task.entity.ts
      tasks.service.ts
    quotes/                      # 报价版本与审核
      quote.entity.ts
      quote-version.service.ts
      quote-review.service.ts    # 强制审核二次校验
    cos/                         # 私有桶、签名 URL、STS
      cos.service.ts
      cos-upload.controller.ts   # 客户上传（STS 直传）
    audit/                       # 审计日志
      audit-log.entity.ts
      audit.service.ts
    agent-api/                   # 06 契约控制器（仅内部路径）
      agent.controller.ts
      agent.dto.ts
      result.schema.ts           # complete 结果 JSON Schema
```

---

## 5. 数据模型

### 5.1 `projects`（项目）

| 字段 | 类型 | 说明 |
|---|---|---|
| id | varchar(32) | `prj_` 前缀主键 |
| name | varchar(200) | 项目名称 |
| description | text | 需求描述 |
| deadline_date | date | 期望交期 |
| requested_deliverables | json | 交付物数组 |
| currency | varchar(8) | 默认 CNY |
| status | varchar(32) | `draft / submitted / processing / pending_review / quoted / rejected` |
| customer_id | varchar(32) | 客户（本期可空，先留字段） |
| created_at / updated_at | datetime | |

### 5.2 `project_files`（附件元数据，文件本体在 COS）

| 字段 | 类型 | 说明 |
|---|---|---|
| id | varchar(32) | `file_` 前缀 |
| project_id | varchar(32) | 外键 |
| cos_key | varchar(500) | 私有桶对象键 |
| original_name | varchar(255) | 原始文件名 |
| mime_type | varchar(100) | |
| size_bytes | bigint | 单文件 ≤ 100MB |
| sha256 | varchar(64) | 可选 |
| created_at | datetime | |

> 上传限制必须在三处同时执行：前端、后端 DTO、COS 策略（单文件 100MB、每项目 20 个、总 500MB）。

### 5.3 `tasks`（报价任务）

| 字段 | 类型 | 说明 |
|---|---|---|
| id | varchar(32) | `qt_` 前缀 |
| project_id | varchar(32) | 外键 |
| status | varchar(32) | `queued / claimed / completed / failed / retryable_failed` |
| lease_token | varchar(64) | 领取时生成，哈希存储 |
| lease_expires_at | datetime | 默认领取后 30 分钟 |
| agent_id | varchar(64) | 处理 Agent |
| attempts | int | 重试次数，上限 3 |
| quote_id | varchar(32) | 完成后的报价外键 |
| result_json | json | complete 回传的完整结果 |
| error_code / error_message | varchar / text | fail 信息（脱敏） |
| idempotency_key | varchar(64) | 唯一索引；用 task_id |
| created_at / updated_at | datetime | |

唯一约束：`UNIQUE (idempotency_key)`。

### 5.4 `quotes`（报价版本）

| 字段 | 类型 | 说明 |
|---|---|---|
| id | varchar(32) | `quote_` 前缀 |
| project_id / task_id | varchar(32) | |
| version | int | 从 1 递增 |
| quote_type | varchar(32) | `budget_range / preliminary_research / formal_quote` |
| currency | varchar(8) | CNY |
| price_min / price_rec / price_max | decimal(12,2) | 可空（预研报价为空） |
| estimated_hours_total | decimal(8,1) | |
| completeness_score | decimal(4,4) | |
| manual_review_required | boolean | 服务端二次校验结果 |
| review_reasons | json | 审核原因数组 |
| snapshot | json | 规则版本、输入、计算明细（不可变） |
| status | varchar(32) | `pending_review / approved / rejected / sent` |
| approved_by / approved_at | varchar(32) / datetime | 人工批准信息 |
| created_at | datetime | |

### 5.5 `audit_logs`（审计）

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 自增 |
| project_id / task_id / agent_id | varchar | 可空 |
| action | varchar(64) | `task_queued / task_claimed / heartbeat / quote_created / review_required / approved / failed` |
| detail | json / text | 关键字段，不含 Token 与签名 URL |
| created_at | datetime | 保留 365 天 |

### 5.6 `agent_credentials`（Worker 凭证）

| 字段 | 类型 | 说明 |
|---|---|---|
| agent_id | varchar(64) | `office-4070super-01` |
| token_hash | varchar(100) | bcrypt |
| name | varchar(100) | |
| capabilities | json | 能力列表 |
| enabled | boolean | |
| created_at | datetime | |

### 5.7 `quote_reviews`（人工审核记录）

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | |
| quote_id | varchar(32) | |
| reviewer_id | varchar(64) | 后台用户 |
| action | varchar(32) | `approved / rejected / modified` |
| changes | json | 修改前/后差异 |
| reason | text | 必填（留痕） |
| created_at | datetime | |

---

## 6. Agent 接口规范（06 契约的服务端实现）

### 6.1 全局约定

- 路径前缀：`/api/internal/agent`；
- 认证：`Authorization: Bearer <token>`，校验 `agent_credentials`；
- 请求头：`X-Agent-Id`、`X-Agent-Version`、`X-Request-Id`；
- 统一错误格式：

```json
{
  "error": {
    "code": "TASK_NOT_FOUND",
    "message": "Task does not exist.",
    "request_id": "uuid"
  }
}
```

### 6.2 `POST /tasks/claim`

流程：

1. 校验 Token 与 `X-Agent-Id` 匹配、Agent 已启用；
2. 在事务中取一个可处理任务：
   - `status = queued`，或
   - `status = claimed` 且 `lease_expires_at < now()`（租约过期重新投递）；
   - 跳过 `attempts >= 3` 的失败任务；
   - 跳过文件总大小超过 Agent `max_file_size_mb` 的任务；
3. 置 `status = claimed`，生成 `lease_token`（随机 64 位，存哈希），`lease_expires_at = now + 30min`；
4. 为每个附件生成 **15 分钟有效** 的 COS 私有签名 URL；
5. 无任务返回 `204 No Content`。

> PostgreSQL 建议 `SELECT ... FOR UPDATE SKIP LOCKED`；MySQL 用 `UPDATE ... WHERE status='queued' AND id IN (SELECT ...) LIMIT 1` 保证原子性。

### 6.3 `POST /tasks/{task_id}/heartbeat`

- 校验 `lease_token` 与任务一致；不一致返回 `409 TASK_LEASE_CONFLICT`；
- `progress_percent` 必须为 0-100 整数；
- 当剩余租约 < 10 分钟时续租 30 分钟；否则仅记录心跳；
- 返回新的 `lease_expires_at`。

### 6.4 `POST /tasks/{task_id}/complete`

流程：

1. 用 `Idempotency-Key`（应为 task_id）查询：若已存在报价，直接返回原 `quote_id`（幂等）；
2. 校验 `lease_token`；
3. 校验 `result` 通过 JSON Schema（见 6.6）；
4. 落库报价版本：计算快照、工时、价格区间、审核原因；
5. **服务端强制审核二次校验**（见第 7 节），得出 `manual_review_required`，不信任客户端值；
6. 任务置 `completed`，`project.status = pending_review / quoted`；
7. 返回 `quote_id` 与 `project_status`。

响应示例：

```json
{
  "task_id": "qt_01JABCDEF",
  "status": "completed",
  "quote_id": "quote_01JABCDEF",
  "project_status": "pending_review"
}
```

### 6.5 `POST /tasks/{task_id}/fail`

```json
{
  "lease_token": "opaque-secret",
  "stage": "parsing_files",
  "retryable": true,
  "error_code": "OCR_SERVICE_UNAVAILABLE",
  "message": "OCR service did not respond within 180 seconds."
}
```

- `retryable = true`：`attempts + 1`，置回 `queued` 并按指数退避排期重试（30s / 120s / 300s，上限 3 次）；
- `retryable = false`：置 `failed`，通知内部人员；
- `message` 只允许脱敏后的内容（服务端可再做一次敏感词/Token 清洗），不得原样展示给客户。

### 6.6 `complete` 结果 Schema 要点

必须放行以下字段（Worker 已实现）：

```json
{
  "result_schema_version": "1.0",
  "agent": { "agent_id": "...", "agent_version": "..." },
  "runtime": {
    "ollama_model": "...",
    "rule_set_version": "...",
    "prompt_versions": { "requirement_extraction": "v1", "classification": "v1", "review": "v1" }
  },
  "result": {
    "project_type": "pneumatic_press_fixture",
    "completeness_score": 0.5714,
    "classification_confidence": 0.9,
    "estimated_hours": { "total": 231.15 },
    "price": { "currency": "CNY", "minimum": null, "recommended": null, "maximum": null },
    "manual_review_required": true,
    "manual_review_reasons": ["..."],
    "missing_information": ["..."],
    "assumptions": [],
    "exclusions": [],
    "clarification_questions": [],
    "reviewer_notes": ["..."],
    "calculation_snapshot": { "...": "..." }
  }
}
```

> 相对 06 文档新增两个字段：`result.reviewer_notes`（审核助手意见）与 `result.calculation_snapshot.rag_cases`（RAG 参考案例）。Schema 校验时必须放行，否则 Worker 回传会被 422 拒绝。

### 6.7 错误码对照

| HTTP | 错误码 | 服务端场景 |
|---:|---|---|
| 400 | `VALIDATION_ERROR` | DTO/Schema 校验失败 |
| 401 | `UNAUTHORIZED` | Token 缺失/无效 |
| 403 | `AGENT_FORBIDDEN` | Token 有效但 Agent 被禁用 |
| 404 | `TASK_NOT_FOUND` | 任务不存在 |
| 409 | `TASK_LEASE_CONFLICT` | lease_token 不匹配或任务已处理 |
| 410 | `SIGNED_URL_EXPIRED` | 附件签名 URL 过期（Worker 重新 claim） |
| 422 | `RESULT_SCHEMA_INVALID` | result 不符合 Schema |
| 429 | `RATE_LIMITED` | 限流 |
| 500/502/503 | `SERVER_ERROR` | 服务端异常 |

---

## 7. 服务端强制审核二次校验（不能信任 Worker）

报价落库时，服务端必须按当前规则版本重算以下条件（与 QRS §12.1 对齐）：

- 需求完整度 < 0.80；
- 报价建议金额 ≥ ¥50,000（有价格区间时）；
- 交期比例 < 0.70（`deadline_workdays / 标准周期`，标准周期由 `rule_set_version` 对应规则计算或取 Worker 回传的 `estimated_standard_cycle_days`）；
- 精度要求 ≤ ±0.05mm；
- 项目类型属于强制审核集合（`welding_fixture / pneumatic_press_fixture / loading_module / single_station_machine`）；
- 关键接口/载荷/工件尺寸/验收标准缺失；
- 医疗、航空、压力容器、防爆等高责任行业；
- `complexity_factor × risk_factor × rush_factor > 2.00`。

任一命中 → `manual_review_required = true`、`quote.status = pending_review`、写 `audit_logs`。人工在后台批准后才允许转 `formal_quote` 发送客户。

> 规则版本号随 `calculation_snapshot` 保存；服务端二次校验的规则集与 Worker 规则集必须来自同一版本（建议服务端持有 `rule_set_version -> 阈值` 配置）。

---

## 8. COS 集成

### 8.1 上传（客户提交附件）

- 建议 **STS 临时密钥直传**：前端向后端申请临时密钥 → 直传 COS 私有桶 → 回传文件元数据；
- 后端落库前校验 MIME、大小与数量限制；
- 桶策略禁止公共读；管理端下载用签名 URL。

### 8.2 下发（Worker 下载）

- `claim` 时为每个文件生成 `download_url`（有效期 15 分钟）+ `sha256`；
- Worker 仅凭签名 URL 下载，不需要任何 COS 密钥；
- 过期后 Worker 重新 `claim` 或服务端提供刷新接口（MVP：重新 claim）。

### 8.3 清理

- 按合同/客户政策保留；提供管理端清理任务；
- 本地 Worker 侧原始文件任务结束即删（Worker 责任，服务端无需干预）。

---

## 9. 鉴权与安全

- Agent 接口：Bearer Token，`agent_credentials.token_hash`（bcrypt）比对，启动时支持多 Agent；
- 内部路径与客户路径分离：`/api/internal/agent/*` 只允许 Agent Token；
- 全局 `ValidationPipe` + 统一异常过滤器；
- HTTPS only；CORS 只开放客户前端域名；
- 限流：`@nestjs/throttler`（Agent 接口按 X-Agent-Id 限流）；
- 审计：所有状态变更写 `audit_logs`；
- 日志脱敏：Token、签名 URL、客户文件正文不得入日志。

---

## 10. 环境变量（.env.example）

```env
# 服务
PORT=3000
NODE_ENV=production

# 数据库
DB_HOST=127.0.0.1
DB_PORT=5432
DB_USER=quote
DB_PASSWORD=change-me
DB_NAME=quote_agent

# COS（腾讯云私有桶）
COS_SECRET_ID=your-secret-id
COS_SECRET_KEY=your-secret-key
COS_BUCKET=your-private-bucket
COS_REGION=ap-shanghai
COS_PUBLIC_BASE_URL=https://<你的域名>/files   # 代理下载入口（可选）

# 规则版本与审核阈值（与本地 quote_rules.yaml 同一版本）
RULE_SET_VERSION=0.1.0-draft
REVIEW_AMOUNT_THRESHOLD_CNY=50000
REVIEW_COMPLETENESS_BELOW=0.80
REVIEW_RUSH_RATIO_BELOW=0.70

# Agent 接入
AGENT_TOKEN_PLAIN=init-only   # 首次初始化后只存哈希
```

> 禁止把 `COS_SECRET_KEY`、`AGENT_TOKEN_PLAIN` 提交到 Git；CI/CD 通过 Secret 注入。

---

## 11. 部署（服务器，免 FRP）

```bash
# 服务器：拉取代码并构建
git pull origin main
docker compose up -d --build          # 推荐 Docker Compose 运行 NestJS + DB
# 或 PM2
# npm ci && npm run build && pm2 start dist/main.js
```

- HTTPS：Caddy（自动证书）或 Nginx / 云负载均衡终止 TLS，443 → NestJS 端口；
- 数据库与 NestJS 同机或使用云数据库；
- 防火墙：只开放 443（和 SSH）；无需向本地工作站开放任何端口；
- 升级：保留上一个镜像/Tag，先跑迁移再切流。

---

## 12. 扩展预留：客服 Agent（本期不实现）

报价 Agent 是"任务制轮询"；客服 Agent 是"会话制实时"，本期只预留设计，不实现：

- **通道**：Worker 主动维持到服务器的 WebSocket 长连接（出站），服务器推送客户消息、Worker 流式回传 token（Ollama `stream: true`）；
- **表预留**：`conversations`、`chat_messages`（建议 M4/M5 建表）；
- **模块位置**：`src/modules/chat/`，与 `agent-api` 平级，互不干扰；
- **鉴权**：客户侧走用户 Token，Worker 侧仍走 Agent Token；
- 关键约束不变：服务器不反向连接本机；原始客户文件不进云模型。

---

## 13. 验收清单（对应 08 测试文档的服务端部分）

```text
□ F-01  同一任务只能被一个 Agent 领取；租约过期后重新投递
□ F-06  每份报价含规则计算快照，且快照不可变
□ F-07  任一强制审核条件命中即 pending_review（服务端二次校验）
□ F-09  同一 Idempotency-Key 重复 complete 返回同一 quote_id
□ F-10  客户接口无法读取其他项目与内部字段
□ Agent Token 不在日志/Git；COS 桶非公共读写；HTTPS only
□ 失败任务按指数退避重试，最多 3 次
□ 上传限制（100MB / 20 个 / 500MB）在前后端与 COS 策略同时生效
□ 与本地 Worker 联调通过：claim -> heartbeat -> complete -> pending_review
```

---

## 14. 里程碑建议

| 阶段 | 内容 | 验收 |
|---|---|---|
| B1 | NestJS 骨架 + Agent 鉴权 + 数据模型迁移 | Agent Token 可鉴权，表结构就绪 |
| B2 | claim / heartbeat / complete / fail + 租约 + 幂等 | 单测通过，与 mock Worker 联调 |
| B3 | COS 上传/签名 URL + 强制审核二次校验 + 审计 | 端到端：上传 -> 报价 -> pending_review |
| B4 | 真实 Worker 联调 + 发布到服务器（HTTPS） | 全链路跑通，FRP 停用 |

---

## 15. 版本记录

| 版本 | 日期 | 修改说明 | 审批人 |
|---|---|---|---|
| `0.1.0-draft` | 2026-08-05 | 创建 NestJS 后端开发文档（报价 Agent 接口） | 待指定 |
