# Quote Agent（机械设计 AI 报价系统 MVP）

依据 `04-quote-rule-specification.md` 与 `05-agent-workflow.md` 搭建的本地报价 Agent。
设计原则：**LLM 只负责理解资料与分类，价格与人工审核判定完全由确定性规则引擎计算**。

## 当前里程碑（M1）

- `config/quote_rules.yaml`：可配置、带版本号的规则集（工时、费率、系数、审核阈值）。
- `src/quote_agent/quote_engine.py`：确定性报价引擎（工时明细、价格区间、人工审核判定、规则快照）。
- `src/quote_agent/models.py`：Pydantic 数据模型（需求 / 分类 / 报价 / 审核）。
- `tests/`：边界值测试（完整度 0.80、交期 0.70、金额阈值、精度、零件数、确定性）。
- Docker 开发环境：宿主机无 Python，一切在容器内运行。

## 快速开始（Windows + Docker Desktop）

```powershell
docker compose build
docker compose run --rm quote-agent python scripts/validate_rules.py
docker compose run --rm quote-agent python scripts/test_quote.py
docker compose run --rm quote-agent pytest -q
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
