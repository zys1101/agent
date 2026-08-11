# RAG 案例批量导入规范（Qdrant）

- **文档编号**：RAG-001
- **版本**：0.1.0-draft
- **关联**：[业务大类分类规范](12-case-category-taxonomy.md)、`scripts/seed_rag_cases.py`

## 1. 数据格式

每个案例是一条 `CaseRecord`，支持三种文件形态（同一目录可混用）：

1. **JSON 单条**：一个文件一条记录；
2. **JSON 数组**：一个文件 `[...]` 多条记录；
3. **JSONL**：每行一条记录，`#` 开头的行会被忽略。

字段说明：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `case_id` | string | 是 | 全局唯一、稳定；重复导入同一 id 会覆盖而不是新增（幂等） |
| `project_type` | string | 是 | 内部报价类型，来自 `quote_rules.yaml` 的 `project_types`；官网案例无对应类型时可用大类 key |
| `category` | string | 推荐 | 业务大类 key，来自 `case_categories`（如 `tooling_fixture`、`machine_design`） |
| `summary` | string | 是 | 案例摘要，**向量检索的主要匹配文本**；建议 50-200 字，写清结构、材料、精度、交付物等关键词 |
| `deliverables` | string[] | 否 | 交付物，可用内部 code（如 `three_d_assembly`）或中文描述 |
| `risk_notes` | string[] | 否 | 风险提示，作为审核上下文 |
| `typical_hours` | number | 否 | 典型工时，不确定就省略或写 `null` |
| `price_range_cny` | [min, max] | 否 | 成交价区间（CNY），不确定就省略或写 `null` |

模板文件：[template.json](examples/knowledge_case_templates/template.json)、[template.jsonl](examples/knowledge_case_templates/template.jsonl)

## 2. 安全与脱敏要求

- **只允许存脱敏字段**：不得写入客户名称、联系方式、原始图纸、报价单原文等敏感信息；
- `summary` 请改写为可公开检索的描述；
- 建议在入库前先跑 `--dry-run` 校验格式。

## 3. 导入步骤

前提：Qdrant 已启动（`docker compose up -d qdrant`），Ollama 已拉取嵌入模型
（`.env` 中 `EMBEDDING_MODEL`，默认 `bge-m3:latest`，`ollama pull bge-m3`）。

### 3.1 先把数据放好

把案例文件放进任意目录，例如：

```text
data/my_cases/
  batch_01.json      # 单条或数组
  batch_02.jsonl     # 每行一条
```

### 3.2 校验（不连 Qdrant/Ollama）

```powershell
docker compose run --rm quote-agent python scripts/seed_rag_cases.py --data-dir data/my_cases --dry-run
```

输出格式正确后，会打印条数与“按业务大类/按 project_type”的统计。

### 3.3 正式导入

```powershell
docker compose run --rm quote-agent python scripts/seed_rag_cases.py --data-dir data/my_cases
```

默认导入 `examples/knowledge_cases`（项目内置案例），常用参数：

| 参数 | 说明 |
|---|---|
| `--data-dir` | 数据目录 |
| `--glob` | 文件匹配模式，可多次指定（默认 `*.json` + `*.jsonl`） |
| `--collection` | Qdrant collection 名（默认 `quote_cases`，也可用 `QDRANT_COLLECTION` 环境变量） |
| `--dim` | 向量维度（默认 1024=bge-m3） |
| `--batch-size` | 每批嵌入/写入条数（默认 64） |
| `--dry-run` | 只校验不写入 |

## 4. 验证结果

- Qdrant 控制台：http://localhost:6333/dashboard （本机 Docker 映射端口）
- 命令行统计：

```powershell
docker compose run --rm quote-agent python -c "from quote_agent.rag import RagStore; s=RagStore.from_env(); print(s.client.count(s.collection))"
```

> 注：在容器内执行时 Qdrant 主机为 `qdrant`（compose 服务名），`.env` 的
> `QDRANT_HOST` 会被 compose 覆盖；在宿主机直连 Python 时用 `127.0.0.1:6333`。
