# Quote Agent（机械设计 AI 报价系统 MVP）

依据 `04-quote-rule-specification.md` 与 `05-agent-workflow.md` 搭建的本地报价 Agent。
设计原则：**LLM 只负责理解资料与分类，价格与人工审核判定完全由确定性规则引擎计算**。

## 当前里程碑（M1）

- `config/quote_rules.yaml`：可配置、带版本号的规则集（工时、费率、系数、审核阈值）。
- `src/quote_agent/quote_engine.py`：确定性报价引擎（工时明细、价格区间、人工审核判定、规则快照）。
- `src/quote_agent/models.py`：Pydantic 数据模型（需求 / 分类 / 报价 / 审核）。
- `tests/`：边界值测试（完整度 0.80、交期 0.70、金额阈值、精度、零件数、确定性）。
- Docker 开发环境：宿主机无 Python，一切在容器内运行。

## 当前里程碑（M2）

- `src/quote_agent/llm.py`：Ollama 客户端（temperature=0、JSON 输出、失败自动修复一次）。
- `src/quote_agent/extraction.py` / `classification.py`：需求提取与项目分类 Agent（只出结构化需求，不出价格）。
- `src/quote_agent/completeness.py`：需求完整度**确定性重算**（QRS 9.1/9.2），不信任模型自报分值。
- `src/quote_agent/file_parser.py`：文本型 PDF / Excel / TXT 解析（图片 OCR 由视觉模型在 M3 接入）。
- `src/quote_agent/cloud.py`：云端 API 客户端 + 内存版 Mock 云端（HTTP，支持租约/心跳/幂等回传）。
- `src/quote_agent/worker.py`：按 AWF-001 主流程执行任务：领取→下载→解析→提取→校验→分类→报价→回传→清理。
- `scripts/run_worker.py`：Worker 入口（`--demo` 可用本地 mock 云端全链路跑一个样例任务）。
- `scripts/mock_cloud.py`：独立 Mock 云端 HTTP 服务。
- `scripts/try_ollama.py`：验证 Ollama 连通性与 JSON 输出路径。

## 快速开始（Windows + Docker Desktop）

```powershell
docker compose build
docker compose run --rm quote-agent python scripts/validate_rules.py
docker compose run --rm quote-agent python scripts/test_quote.py
docker compose run --rm quote-agent pytest -q
docker compose run --rm quote-agent python scripts/try_ollama.py
docker compose run --rm quote-agent python scripts/run_worker.py --demo --once
docker compose up -d mock-cloud          # 独立 Mock 云端（可选）
```

## 目录结构

```text
config/quote_rules.yaml    # 规则配置（唯一事实来源）
src/quote_agent/
  config.py                # 规则配置的 Pydantic 模型与加载/校验
  models.py                # 需求/分类/报价/审核数据结构
  quote_engine.py          # 确定性报价引擎
scripts/
  validate_rules.py        # 规则配置校验
  test_quote.py            # 报价引擎演示
  run_worker.py            # Worker 骨架（下一里程碑）
tests/                     # pytest 测试
```

## 说明

- 所有工时、费率、系数均为 QRS 文档中的示例占位值，上线前需业务负责人确认（见 QRS 第 18 节）。
- `quote_engine` 不依赖 LLM；需求提取、分类、审查由后续里程碑接入 Ollama。
