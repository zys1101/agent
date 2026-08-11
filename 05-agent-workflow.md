# 本地 Agent 工作流与工具规范（草案）

- **文档编号**：AWF-001
- **版本**：0.1.0-draft
- **适用运行环境**：Windows 11 本地工作站、RTX 4070 Super 12GB、Ollama、Docker Desktop
- **关联文档**：[报价规则规范](04-quote-rule-specification.md)、[API 契约](06-api-contract.md)

> **特别注意 1：价格控制权**：LLM 只提取需求、分类、发现风险、生成文字；`quote_engine` 是唯一可计算价格的模块。LLM 不得修改规则引擎金额。
>
> **特别注意 2：文件安全**：原始客户文件仅下载到本地临时目录；任务完成、失败或超过 24 小时后必须清除。不得把客户原始附件写入 RAG。
>
> **特别注意 3：任务可靠性**：Worker 必须使用“领取租约 + 心跳 + 幂等回传”。本地电脑意外关机后，云端可在租约到期后重新投递任务。

---

## 1. 工作流目标

本地 Quote Agent Worker 从腾讯云后端主动领取报价任务，下载 PDF、图片、Excel 文件，在本地完成解析、需求抽取、分类、规则报价和审查，并把结构化结果回传云端。

### 1.1 主流程

```text
CLAIM_TASK
  → DOWNLOAD_FILES
  → PARSE_FILES
  → EXTRACT_REQUIREMENTS
  → VALIDATE_REQUIREMENTS
  → CLASSIFY_PROJECT
  → RETRIEVE_CASES（MVP 默认关闭）
  → CALCULATE_QUOTE
  → REVIEW_QUOTE
  → BUILD_RESPONSE
  → UPLOAD_RESULT
  → CLEANUP
```

### 1.2 状态定义

| 状态 | 含义 | 最大建议耗时 | 失败后动作 |
|---|---|---:|---|
| `claimed` | 已领取、获得云端租约 | 1 分钟 | 续租或释放 |
| `downloading_files` | 正在下载 COS 临时文件 | 10 分钟 | 重试 3 次 |
| `parsing_files` | PDF/OCR/Excel 解析 | 15 分钟 | 标记文件错误并继续可用文件 |
| `extracting_requirements` | LLM 提取结构化需求 | 8 分钟 | 重试一次，仍失败转人工 |
| `validating_requirements` | Pydantic/业务字段校验 | 1 分钟 | 转人工 |
| `classifying_project` | 分类与复杂度判断 | 3 分钟 | 转人工 |
| `calculating_quote` | 规则报价计算 | 1 分钟 | 任务失败，需排查规则 |
| `reviewing_quote` | 一致性/审核判断 | 3 分钟 | 强制人工审核 |
| `uploading_result` | 回传云端 | 5 分钟 | 指数退避重试 |
| `completed` | 任务完成 | - | - |

## 2. 任务执行伪代码

```python
def run_task(task):
    lease = cloud.claim(task)
    workdir = file_manager.create_task_dir(task.id)
    try:
        files = cloud.download_signed_files(task.files, workdir)
        parsed = parser.parse_all(files)
        requirement = requirement_agent.extract(task.form, parsed)
        validation = validator.validate(requirement)
        classification = classification_agent.classify(requirement, parsed)
        cases = rag.retrieve(requirement, classification) if settings.rag_enabled else []
        calculation = quote_engine.calculate(requirement, classification, cases)
        review = review_agent.review(requirement, classification, calculation)
        result = result_builder.build(requirement, classification, calculation, review)
        cloud.complete(task.id, result, idempotency_key=task.id)
    except RetryableError as err:
        cloud.fail(task.id, retryable=True, message=str(err))
    except Exception as err:
        cloud.fail(task.id, retryable=False, message=sanitize(err))
    finally:
        file_manager.remove_task_dir(workdir)
```

## 3. 工具契约

| 工具 | 输入 | 输出 | 关键限制 |
|---|---|---|---|
| `file_classifier` | 文件路径、MIME | 文件类型、是否可处理 | 仅 PDF/JPG/PNG/XLS/XLSX |
| `pdf_parser` | PDF | 每页文本、表格、OCR 需求 | 不得将 OCR 数字视为已确认参数 |
| `ocr_tool` | 图片/PDF页 | 文本块、坐标、置信度 | 置信度 < 0.80 的参数须标识不确定 |
| `excel_parser` | Excel | 工作表、BOM 摘要、原始表格 | 不可假定列名固定 |
| `ollama_client` | Prompt、Schema | 合法 JSON | 温度为 0；超时 180 秒 |
| `requirement_agent` | 表单+解析资料 | `ProjectRequirement` | 不得输出价格 |
| `classification_agent` | 需求 | `ProjectClassification` | 只能使用预定义类别 |
| `rag_retriever` | 查询+元数据 | 脱敏历史案例 | MVP 默认关闭 |
| `quote_engine` | 结构化输入+规则 | `QuoteCalculation` | 确定性、可复现 |
| `review_agent` | 需求+计算结果 | `QuoteReview` | 只能增加审核，不可改价 |
| `cloud_client` | HTTP 请求 | API 响应 | 仅 HTTPS，使用 Token |

## 4. 输入与输出数据模型

### 4.1 `ProjectRequirement` 必填核心字段

```json
{
  "project_type_candidate": "pneumatic_press_fixture",
  "requested_deliverables": ["three_d_assembly", "two_d_part_drawing", "bom"],
  "deadline_workdays": 10,
  "unknowns": ["maximum_press_force"],
  "risks": ["missing_installation_interface"],
  "completeness_score": 0.73,
  "evidence": [{"field": "deadline_workdays", "source_file": "form", "value": "10"}]
}
```

### 4.2 模型输出校验规则

1. 所有模型输出必须经 Pydantic Schema 校验。
2. JSON 无效时，允许一次“修复 JSON”调用；第二次失败则进入人工审核。
3. 精度、载荷、交期、工件尺寸等关键数据必须包含来源；没有来源则为 `unknown`。
4. `completeness_score` 必须由本地确定性算法重算，不能直接相信模型值。
5. 任何 `unknown` 的关键字段都必须转化为客户澄清问题或人工审核原因。

> **提速说明**：LLM 审核（REVIEW_QUOTE）仅在规则引擎判定 `manual_review_required=true` 时执行；
> 引擎未判定审核的任务直接跳过 LLM 审核，避免一次不必要的慢调用。

## 5. LLM 调用策略

| 参数 | MVP 值 | 原因 |
|---|---:|---|
| 模型 | Ollama 中已部署的中文指令模型 | 由部署配置决定 |
| `temperature` | 0 | 保持结构化结果稳定 |
| `top_p` | 0.9 | 保留必要语义理解能力 |
| 单次请求超时 | 180 秒 | 适配本地 4070 Super |
| JSON 修复次数 | 1 次 | 防止循环调用 |
| 单任务 LLM 总调用上限 | 6 次 | 控制时延和异常成本 |
| OCR 低置信度阈值 | 0.80 | 低于此值不能视为确认事实 |

> **特别注意**：4070 Super 为 12GB 显存。MVP 优先使用 7B/8B 的 4-bit 量化中文指令模型；14B 模型可能可运行但更容易产生显存/内存压力和较长处理时间。先以稳定 JSON 输出为选型标准。

## 6. 错误、重试与降级

| 情况 | 处理 |
|---|---|
| COS 下载失败 | 30 秒、120 秒、300 秒后重试，共 3 次 |
| 单个 PDF 无法解析 | 记录文件错误；其余文件继续；降低完整度 |
| OCR 失败 | 标识该页不可读，生成补充资料问题 |
| Ollama 不可用 | 任务标记 `retryable`，5 分钟后重试 |
| 模型返回错误 JSON | 修复一次；失败后转人工审核 |
| 规则文件无效 | 立即停止计算，回传不可重试错误 |
| 云端回传失败 | 保存本地结果快照，指数退避重试 5 次 |
| 本地电脑重启 | 启动时扫描未完成任务；仅继续未回传的本地快照 |

## 7. 本地目录与留存策略

```text
data/incoming/{task_id}/       # 原始文件临时目录，最长保留24小时
data/processed/{task_id}/      # 解析结果，默认保留30天
data/audit_logs/               # 审计日志，保留365天
data/quote_snapshots/          # 规则和报价快照，保留365天
data/knowledge/                # 经批准、脱敏的内部知识
```

- 原始客户文件：任务结束后立即删除；删除失败时由每日清理任务在 24 小时内处理。
- 本地日志中不得记录 API Token、COS 签名 URL、完整客户附件正文。
- 报价快照必须保存规则版本、Prompt 版本、模型标签、输入字段及计算结果。

## 8. MVP 验收标准

- Worker 可以连续运行 24 小时；
- 成功领取、处理和回传一个 PDF/图片/Excel 混合任务；
- 100% 的价格来自 `quote_engine`；
- 100% 的 LLM 输出经过 Schema 校验；
- 任务完成后原始文件目录为空；
- 对完整度低于 0.80 的项目，100% 标记人工审核；
- 云端网络中断后，恢复网络可自动回传本地已完成结果。
