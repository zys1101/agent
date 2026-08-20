# 已完成订单数据回传契约（案例库数据同步）

> 文档编号：API-CASE-001　版本：0.1.0-draft　日期：2026-08-20
> 接收方：本地 AI 报价 Worker 开发人员
> 目的：把云端（小程序/网站合并后端）**已完成订单**的脱敏数据，定时同步到本地大模型机器，
> 落成本地报价案例库（`examples/knowledge_cases/`），让 AI 报价基于真实成交案例越来越准。

---

## 1. 背景与目标

本地 AI 报价 Worker 目前使用 `examples/knowledge_cases/*.json`（14 条手工脱敏案例）作为 RAG 参考。
为了让报价贴近真实市场，需要把**平台真实成交订单**沉淀成新案例。

**目标**：订单验收完成后，云端自动生成一条“案例同步任务”；本地 Worker 定时轮询领取，
清洗后写入本地案例库，并回写处理结果。全程**云端不主动连本地**，本地只做**出站轮询**。

## 2. 总体架构与数据流

```text
云端（www.sonsentech.cn，唯一后端）
  1. 订单验收完成（FINISHED）→ 生成案例同步任务入队（case_sync_tasks）
  2. 任务 = { task_id, payload(脱敏订单数据), 状态: queued }
        ▲                                        │
        │ claim（本地每 15~30s 轮询）              │
        │                                        ▼
本地大模型机器（Worker）
  3. POST /internal/case-sync/tasks/claim  → 拿到 payload
  4. 解析 + 校验 + 清洗（字段限制、脱敏兜底、金额范围）
  5. 转成 knowledge_cases 格式 → 去重 → 写入本地案例库
  6. POST /internal/case-sync/tasks/:id/complete 回写 { case_id, status }
     失败则 POST .../fail（retryable=true 自动重试）
```

### 与现有 AI 报价任务的区别

| | AI 报价任务（`internal/agent/tasks`） | 案例同步任务（`internal/case-sync/tasks`，本文档） |
|---|---|---|
| 方向 | 本地算报价 → 回传云端 | 云端给数据 → 本地入库 |
| payload | 客户报价表单 + 附件 URL | 已完成订单脱敏数据 |
| 结果 | 报价结果（价格/审核） | 处理状态 + 本地 case_id |
| 触发 | 用户发起 AI 报价 | 订单验收完成 |

> 两条通道互不影响，协议字段保持同一套风格（claim/heartbeat/complete/fail）。

## 3. 触发时机与数据范围

仅以下节点会入队：

- 客户确认验收：订单 `WAIT_CONFIRM → FINISHED`
- 管理员强制完成 / 结算：订单置为 `FINISHED`
- 售后完结：大改/小改售后 `DONE` 后，若订单仍 `FINISHED` 则**更新**原任务（补售后统计）

**同一订单只保留一条任务**（按 `order_id` 唯一；售后完结时在原任务上打补丁，不重复入队）。
未完成（进行中/取消/退款）订单**不会**入队。

## 4. 接口契约

### 4.1 Base URL

```text
https://www.sonsentech.cn/api/mp/v1/internal/case-sync/tasks
```

> 若本地与服务器同机联调，可用 `http://127.0.0.1:3001/api/mp/v1/internal/case-sync/tasks`。

### 4.2 鉴权（与 AI 报价 Worker 一致）

```http
Authorization: Bearer <AGENT_API_TOKEN>
Content-Type: application/json
X-Agent-Id: office-4070super-01
X-Agent-Version: 0.1.0
X-Request-Id: <uuid>
```

- `AGENT_ID` / `AGENT_API_TOKEN` 与云端 `/var/www/shared-applet/.env` 完全一致
- 鉴权失败返回 `401`

### 4.3 领取任务

```http
POST /claim
```

请求：

```json
{
  "agent_id": "office-4070super-01",
  "agent_version": "0.1.0",
  "max_file_size_mb": 1
}
```

成功 `200`：

```json
{
  "task_id": "cs_42",
  "lease_token": "opaque-secret",
  "lease_expires_at": "2026-08-20T18:30:00+08:00",
  "payload": {
    "task_id": "cs_42",
    "order_no": "WEB0000000042",
    "order_id": 42,
    "finished_at": "2026-08-20T10:00:00+08:00",
    "requirement": {
      "title": "电机壳体压装工装设计",
      "description": "设计一套用于电机壳体压装的工装，定位精度±0.05mm，交付3D装配与2D图纸。",
      "category_id": 3,
      "category_name": "工装夹具设计",
      "budget_min": 2000,
      "budget_max": 5000,
      "expected_days": 10,
      "urgency_level": 1
    },
    "quotation": {
      "price": 3600,
      "delivery_days": 9,
      "designer_level": 1
    },
    "order": {
      "amount": 3600,
      "platform_fee": 360,
      "designer_amount": 3240,
      "paid_at": "2026-08-18T09:00:00+08:00",
      "finished_at": "2026-08-20T10:00:00+08:00",
      "deliver_count": 1,
      "reject_count": 0
    },
    "after_sale": {
      "count": 0,
      "minor_count": 0,
      "major_count": 0
    },
    "feedback": {
      "rating": 5,
      "comment": ""
    }
  }
}
```

无任务时返回 `204 No Content`（本地应等待 15~30s 再领）。

### 4.4 心跳续租（可选）

```http
POST /{task_id}/heartbeat
```

```json
{ "lease_token": "opaque-secret", "stage": "writing_case", "progress_percent": 60, "message": "" }
```

### 4.5 完成回写

```http
POST /{task_id}/complete
Idempotency-Key: cs_42
```

```json
{
  "lease_token": "opaque-secret",
  "result_schema_version": "1.0",
  "agent": { "agent_id": "office-4070super-01", "agent_version": "0.1.0" },
  "result": {
    "status": "stored",
    "case_id": "case_015_press_fixture",
    "case_file": "examples/knowledge_cases/case_015_press_fixture.json",
    "deduplicated": false
  }
}
```

返回 `200`：`{ "task_id": "cs_42", "status": "completed", "result_schema_version": "1.0" }`

**幂等**：云端按 `Idempotency-Key`（= task_id）去重；断网重传不会重复处理。

### 4.6 失败回执

```http
POST /{task_id}/fail
```

```json
{
  "lease_token": "opaque-secret",
  "stage": "parsing",
  "retryable": true,
  "error_code": "PAYLOAD_INVALID",
  "message": "amount missing"
}
```

- `retryable=true`：云端按退避（30s/120s/300s）重新入队，最多 3 次
- `retryable=false`：标记失败，云端留痕供排查

### 4.7 错误码

| HTTP | 含义 | 本地处理 |
|---:|---|---|
| 204 | 无任务 | 等 15~30s 再领 |
| 400 | 请求体校验失败 | 修请求，不重试 |
| 401 | 鉴权失败 | 检查 AGENT_ID/AGENT_API_TOKEN |
| 404/409 | 任务不存在/租约冲突 | 放弃本任务，重新领取 |
| 500/502/503 | 服务端异常 | 指数退避重试 |

## 5. 数据清洗与限制（云端已做，本地兜底再校验）

### 5.1 云端脱敏（出网前已完成）

- 不传任何用户身份：`openid` / `phone` / `nickname` / 真实姓名 / 头像一律**不出现**
- 订单数据只有金额/时间/数量等业务字段
- 描述文本仅保留需求正文，不含联系方式

### 5.2 云端限制（保证数据干净）

| 项 | 限制 |
|---|---|
| 金额 | `0 < amount ≤ 1,000,000`，非法订单不入队 |
| 文本 | 标题 ≤ 200 字符；描述 ≤ 5000 字符（超出截断）；剔除控制字符 |
| 任务大小 | payload 序列化 ≤ 32KB |
| 唯一性 | 同一 `order_id` 只入队一条；售后完结打补丁 |
| 附件 | 不传附件二进制，不传附件 URL（本地无法访问云端私有文件） |
| 频率 | 无额外限流（每单一条） |

### 5.3 本地必须再校验（防脏数据进案例库）

领取后本地需校验，**不合格就 `fail(retryable=false)` 而不是硬写入**：

- `order_id` / `order_no` / `finished_at` 存在且格式正确
- `amount` 是 1~1,000,000 的数字；`platform_fee + designer_amount ≈ amount`
- `title` / `description` 非空且为字符串
- `quotation.price` 存在
- 任一必填字段缺失 → 拒绝

## 6. 本地入库格式（直接可用）

把 payload 转成与现有案例一致的结构，写入 `examples/knowledge_cases/case_XXX.json`：

```json
{
  "case_id": "case_015_press_fixture",
  "source_order_no": "WEB0000000042",
  "project_type": "welding_fixture",
  "category": "tooling_fixture_design",
  "summary": "电机壳体压装工装设计：……（由需求标题+描述清洗压缩，≤200字）",
  "deliverables": ["3D装配", "2D图纸"],
  "risk_notes": ["定位精度±0.05mm需现场夹具验证"],
  "typical_hours": 72,
  "price_range_cny": [3600, 3600],
  "finished_at": "2026-08-20",
  "rating": 5
}
```

> - `project_type` / `category` 取值请复用 `04-quote-rule-specification.md` 中的分类枚举；
>   云端下发的 `category_name` 是中文名，本地需映射到英文枚举，映射不了则归 `mechanical_mechanism_design`
> - `typical_hours` 可从 `quotation.delivery_days × 8` 推算，或用订单实际工期
> - `summary` 建议由 LLM 把 `title+description` 压缩成 ≤200 字，并保留关键参数（尺寸/精度/材质/交付物）
> - 新增案例后如需 RAG 生效：`RAG_ENABLED=true` 时运行 `python scripts/seed_rag_cases.py` 重建向量库

## 7. 本地 Worker 开发指南

### 7.1 环境变量（新增）

```dotenv
CASE_SYNC_ENABLED=true
CASE_DATA_DIR=./examples/knowledge_cases
CASE_SYNC_POLL_SECONDS=20
```

### 7.2 轮询主流程（Python 骨架）

```python
import httpx, time, json, uuid, re

BASE = "https://www.sonsentech.cn/api/mp/v1/internal/case-sync/tasks"
HEADERS = {
    "Authorization": f"Bearer {AGENT_API_TOKEN}",
    "X-Agent-Id": AGENT_ID,
    "Content-Type": "application/json",
}

def claim():
    r = httpx.post(f"{BASE}/claim",
                   json={"agent_id": AGENT_ID, "agent_version": "0.1.0"},
                   headers=HEADERS, timeout=30)
    return None if r.status_code == 204 else r.json()

def complete(task_id, lease_token, result):
    httpx.post(f"{BASE}/{task_id}/complete",
               json={"lease_token": lease_token,
                     "result_schema_version": "1.0",
                     "result": result},
               headers={**HEADERS, "Idempotency-Key": task_id}, timeout=30)

def fail(task_id, lease_token, code, msg, retryable=True):
    httpx.post(f"{BASE}/{task_id}/fail",
               json={"lease_token": lease_token, "retryable": retryable,
                     "error_code": code, "message": msg[:2000]},
               headers=HEADERS, timeout=30)

def handle(task):
    payload = task["payload"]
    # 1. 本地校验（见 5.3），不合格 → fail(retryable=false)
    # 2. 转 knowledge_cases 格式（见 §6）
    # 3. 按 order_no 去重：已存在同源案例则覆盖/跳过
    # 4. 写文件 case_XXX.json
    # 5. complete 回写 { status:"stored", case_id, case_file }

while CASE_SYNC_ENABLED:
    try:
        task = claim()
        if task:
            handle(task)
    except Exception as e:
        time.sleep(5)
    time.sleep(CASE_SYNC_POLL_SECONDS)
```

### 7.3 幂等与断网重传

- 云端按 `Idempotency-Key`（=task_id）去重，重传不会重复处理
- 本地建议：写入案例文件前先按 `source_order_no` 查重（同一订单多次补丁/重传时覆盖旧案例）
- 断网导致 complete 未回执：任务租约到期后云端会重新投递，本地重领后直接重传结果

### 7.4 日志

建议在本地打印：

```text
[case-sync] claimed  cs_42 order=WEB0000000042
[case-sync] stored   cs_42 case_id=case_015_press_fixture
[case-sync] failed   cs_42 code=PAYLOAD_INVALID retryable=false
```

## 8. 验收标准

1. 云端完成一笔订单（验收通过）后 1 分钟内，任务进入 `case_sync_tasks` 队列
2. 本地 Worker 开启后 ≤ 30s 领取到该任务
3. 本地案例库出现新的 `case_XXX.json`，字段完整、无用户隐私信息
4. `complete` 回写后云端任务状态为 `completed`
5. 断开本地网络模拟：任务重新投递后本地幂等重传，案例不重复
6. 开启 `RAG_ENABLED=true` 重建向量库后，`try_ollama`/报价流程能检索到新案例

## 9. 联调步骤

1. 本地 `.env` 增加 `CASE_SYNC_ENABLED=true` + 服务器相同的 `AGENT_ID/AGENT_API_TOKEN`
2. 先 `claim` 一次（无任务应 204）确认鉴权通
3. 云端（我方）完成一笔测试订单 → 观察 claim 能拿到 payload
4. 本地跑通 `handle` → complete → 云端任务 completed
5. 全量联调后开启常驻轮询

## 10. 相关文件

- 云端任务表/接口实现：`mech-design-platform/backend/src/case-sync/`（待开发）
- 现有 AI 报价 Worker 协议：`06-api-contract.md`（本契约风格与其一致）
- 本地案例库样例：`examples/knowledge_cases/`
- 分类枚举/规则：`04-quote-rule-specification.md`
