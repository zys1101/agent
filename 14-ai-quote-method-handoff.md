# AI 报价方法对接文档（服务器后端改造）

- **文档编号**：AQ-HANDOFF-001
- **版本**：1.0.0
- **状态**：待服务器后端开发执行
- **适用对象**：负责修改服务器后端（NestJS，`backend-api`）的开发
- **参考原型**：`E:\Program Files\AI_quote`（已验证报价准确的本地原型）
- **本仓库参考实现**：`config/ai_quote_rules.yaml`、`src/quote_agent/ai_quote.py`、`src/quote_agent/ai_evaluator.py`、`scripts/run_ai_quote.py`、`tests/test_ai_quote.py`
- **关联文档**：[报价规则规范](04-quote-rule-specification.md)、[Agent 工作流](05-agent-workflow.md)、[API 契约](06-api-contract.md)、[后端开发文档](09-server-backend-dev.md)

---

## 1. 背景与问题

### 1.1 之前为什么准

`E:\Program Files\AI_quote` 原型使用**“AI 定性，代码定量”**的方法：

1. Qwen3-VL 只做定性分析：理解需求图片与文字，输出**预估工时、复杂度档位、交付物数量**等结构化参数；
2. 价格完全由 Python 规则引擎按固定单价计算：`最终价格 = 预估工时 × 50元/h × 复杂度系数 × 加急系数 [+ 交付物固定单价]`；
3. 加急等级由系统按交付天数判定，**不交给大模型判断**；
4. 大模型提示词里附带参考价格表，帮助它把工时估到正确量级。

原型在历史订单上验证的报价量级（节选）：

| 案例 | 预估工时 | 最终价格 |
|---|---:|---:|
| 轴套类零件 | 2h | 100 元 |
| 电动滑板车设计 | 4h | 600 元 |
| 塑封机自动化设计 | 8h | 2400 元 |
| 门板与框架 | 2h | 350 元 |
| 曲柄机构 | 0.5h | 50 元 |

### 1.2 现在为什么离谱

当前规则引擎（QRS-001 路线）引入了角色费率卡（制图员 ¥180/h、机械工程师 ¥320/h、高级工程师 ¥480/h 等）和基础工时模板。以“气动压装工装”为例：

- 原型方法量级：约 60–100h × 50元/h ≈ **3,000–5,000 元**；
- 当前引擎量级：291h × 混合费率 ≈ **48,000–65,000 元**（见 `data/quote_snapshots/`）。

同一项目报价相差约 **10 倍**，这就是“报价错误离谱”的直接来源。另外，如果服务端/客服链路直接让大模型输出价格，大模型不擅长精确乘法，也会产生随机离谱数字。

### 1.3 改造目标

服务器后端把报价计算切换为本文档描述的 **AI 报价方法**：

- 大模型只输出定性参数，**永远不输出价格**；
- 价格由服务端代码用固定参数计算，且与本地参考实现结果完全一致；
- 参数（时薪、系数、交付物单价）集中配置，调价只改配置不改提示词。

---

## 2. 方法核心原则

1. **AI 定性，代码定量**：LLM 负责理解资料、估工时、判断复杂度、提取交付物；价格计算只允许发生在服务端代码中。
2. **加急等级系统判定**：`delivery_days → urgency_level` 的映射由代码完成，LLM 输出里的 `urgency_level` 一律被系统值覆盖。
3. **参考价格表只用于估量级**：参考价格表喂给 LLM 是为了让它理解“示意图 300–500 元 ≈ 6–10h、加工图纸 2000–3000 元 ≈ 40–60h”，**不是计算公式**。
4. **可复现**：相同输入 + 相同规则版本 => 相同价格。规则版本随报价快照保存。

---

## 3. 整体流程

```text
客户文字需求 + 需求图片
        │
        ▼
1. Qwen3-VL 图片理解（有图片时）──► 图片文字总结
        │
        ▼
2. 系统判定加急等级（交付天数映射 0-5）
        │
        ▼
3. Qwen3-VL 综合评估 ──────────► 结构化参数 JSON
   （提示词含：参考价格表 + 需求 + 图片总结 + 相似案例）
        │
        ▼
4. 服务端规则引擎算价 ─────────► 最终价格 + 明细 + 审核标记
   （唯一允许计算价格的模块）
```

相似历史案例（Qdrant RAG）为可选增强：检索到相似案例后以 JSON 形式拼进评估提示词，帮助 LLM 对齐历史成交量级；没有 RAG 时传空数组即可，不影响流程。

---

## 4. LLM 评估提示词（可直接复制）

### 4.1 图片理解提示词（有图片时调用，输出纯文本）

```text
你是一位经验丰富的机械设计师。请仔细分析以下客户上传的需求图片，结合客户的文字描述，总结这个设计任务的核心特点。
【客户文字需求】
{requirement_text}
请输出一段简洁的描述（200字以内），涵盖以下要点：
1. 这是什么类型的零件/产品
2. 主要的结构特点
3. 可能涉及的设计难点
4. 需要的交付物
直接输出描述文本，不需要其他格式。
```

### 4.2 综合评估提示词（输出 JSON）

```text
你是一位经验丰富的机械设计师，正在评估一个新订单的设计工时和报价参数。
{reference_price_table}
【客户文字需求】
{requirement_text}
【图片理解总结】
{image_summary}
【相似历史案例参考】
{similar_cases}
【交付要求】
期望交付天数: {delivery_days}天
加急等级(系统已判定): {urgency_level} - {urgency_label}
请综合以上所有信息，仔细分析后输出一个JSON格式的评估结果。JSON格式如下：
{
  "part_type": "零件类型（如：电动滑板车）",
  "project_category": "项目类别（如：机械机构设计）",
  "project_subtype": "项目子类（如：角度调节机构设计）",
  "deliverables": ["交付物列表"],
  "deliverable_detail": {
    "assembly_count": 装配体数量(整数，没有就填0),
    "part_drawing_count": 零件图数量(整数，没有就填0),
    "machining_drawing_count": 加工图数量(整数，没有就填0),
    "process_card_count": 工序卡数量(整数，没有就填0),
    "procedure_card_count": 规程卡数量(整数，没有就填0),
    "blank_drawing_count": 毛坯图数量(整数，没有就填0),
    "model_count": 3D模型数量(整数，没有就填0),
    "instruction_book_count": 说明书数量(整数，没有就填0)
  },
  "complexity_tier": "复杂度等级(必须是simple/normal/complex三选一)",
  "estimated_hours": "预估工时(小时，熟练机械设计师完成所需时间，整数。参考上方价格表理解任务量级)",
  "urgency_level": {urgency_level},
  "difficulty_reason": ["难点1", "难点2"],
  "case_summary": "用一段话总结这个设计任务的核心内容和难点"
}
注意：
- complexity_tier 只能是 simple、normal、complex 三选一
- urgency_level 直接使用系统给定的值 {urgency_level}，不要自己判断
- estimated_hours 要参考参考价格表理解任务量级，一个示意图300-500元对应约6-10小时，一个加工图纸2000-3000元对应约40-60小时
只输出JSON，不要输出其他任何内容。
```

### 4.3 参考价格表（`{reference_price_table}` 填充内容）

```text
【参考价格表 — 供你评估工时时参考，不是最终计算公式】

一、整机设备设计
  - 示意图：简单 300-500元，中度 500-1000元，困难 1000-2000元
  - 加工图纸：简单 2000-3000元，中度 3000-5000元，困难 5000-10000元

二、固定单价交付物
  - 装配体：150元/个
  - 零件图/工序卡/规程卡/毛坯图：20元/张
  - 说明书：100元/份
  - 加工图：在零件图基础上 ×1.5（即30元/张）

三、实际计算公式（最终报价用这个）
  最终价格 = 预估工时 × 50元/h × 复杂度系数 × 加急系数
  - 复杂度系数：特别简单 ×0.8，正常 ×1.0，特别复杂 ×1.2
  - 加急系数（6档）：
    0-极宽松(>30天) ×0.85
    1-宽松(20-30天) ×0.90
    2-稍宽松(10-20天) ×0.95
    3-正常(5-10天) ×1.00
    4-加急(2-5天) ×1.30
    5-特急(<2天) ×1.50

请根据参考价格表理解不同类型任务的量级，但最终输出预估工时和系数即可，
实际价格由系统代码计算。
```

---

## 5. LLM 输出 JSON Schema

### 5.1 字段说明

| 字段 | 类型 | 说明 | 校验规则 |
|---|---|---|---|
| `part_type` | string | 零件/产品类型 | 可空字符串 |
| `project_category` | string | 项目类别（如：机械机构设计） | 可空字符串 |
| `project_subtype` | string | 项目子类（如：角度调节机构设计） | 可空字符串 |
| `deliverables` | string[] | 交付物列表 | 默认 [] |
| `deliverable_detail` | object | 交付物数量明细 | 各字段整数 ≥ 0，默认 0 |
| `complexity_tier` | string | `simple` / `normal` / `complex` | 非法值回退 `normal` |
| `estimated_hours` | number | 预估工时（小时） | **必须 > 0**，否则判为无效输出 |
| `urgency_level` | int | 0-5 | **系统强制覆盖**，不信模型值 |
| `difficulty_reason` | string[] | 难点列表 | 默认 [] |
| `case_summary` | string | 任务总结 | 可空字符串 |

### 5.2 服务端强制覆盖规则（必须实现）

1. `urgency_level` = 服务端按交付天数计算的值，**直接覆盖**模型输出；
2. `complexity_tier` 不在三档内 => 回退 `normal`（对应系数 1.0）；
3. `estimated_hours <= 0` 或缺失 => 视为模型输出无效，按失败处理（可重试一次，仍失败转人工）；
4. 模型 JSON 解析失败 => 允许修复一次，再失败转人工，**不得猜测数字**。

---

## 6. 报价计算公式与参数

### 6.1 公式

```text
base_price = estimated_hours × hourly_rate
main_price = base_price × complexity_multiplier × urgency_multiplier
addon_price = Σ(交付物数量 × 固定单价)          # 可选开关，默认关闭
final_price = round(main_price + addon_price)   # 取整到整数元
```

### 6.2 参数表（与参考实现一致，调价只改这里）

| 参数 | 值 |
|---|---|
| 工时单价 `hourly_rate` | **50 元/h** |
| 复杂度 `simple` | ×0.8 |
| 复杂度 `normal` | ×1.0 |
| 复杂度 `complex` | ×1.2 |
| 加急 0（>30 天） | ×0.85 |
| 加急 1（20–30 天） | ×0.90 |
| 加急 2（10–20 天） | ×0.95 |
| 加急 3（5–10 天） | ×1.00 |
| 加急 4（2–5 天） | ×1.30 |
| 加急 5（<2 天） | ×1.50 |
| 装配体 | 150 元/个 |
| 零件图 | 20 元/张 |
| 加工图 | 30 元/张（= 零件图 ×1.5） |
| 工序卡 / 规程卡 / 毛坯图 | 20 元/张 |
| 说明书 | 100 元/份 |
| 3D 模型 | 无固定单价（工时费已覆盖） |

> **注意**：`hourly_rate = 50` 是原型验证值，这是报价量级回归正常的关键。不要使用 QRS 文档中的 180–650 元/小时角色费率卡做 AI 报价。

---

## 7. 加急等级判定（代码实现）

```ts
function deliveryDaysToUrgency(days: number | null, isUrgent = false): number {
  if (days !== null && days !== undefined) {
    if (days > 30) return 0;
    if (days > 20) return 1;
    if (days > 10) return 2;
    if (days > 5) return 3;
    if (days > 2) return 4;
    return 5;
  }
  return isUrgent ? 5 : 3;
}
```

边界示例：30 天 → 1；20 天 → 2；10 天 → 3；5 天 → 4；2 天 → 5。

---

## 8. 价格区间与人工审核（与现有 result 结构对齐）

### 8.1 价格区间（可选，按需求完整度）

为兼容现有 `complete` 结果中的 `price.minimum / recommended / maximum`，建议按需求完整度生成区间：

| 需求完整度 | minimum | recommended | maximum |
|---|---:|---:|---:|
| ≥ 0.90 | final × 0.95 | final | final × 1.05 |
| 0.80–0.89 | final × 0.90 | final | final × 1.10 |
| 0.60–0.79 | final × 0.85 | final | final × 1.20 |
| < 0.60 | 不出固定报价（`recommended = null`，转“需求梳理/预研”报价） | | |

服务端没有完整度时，可只返回 `recommended = final`，min/max 为空。

### 8.2 人工审核标记

满足任一条件时 `manual_review_required = true`：

- 需求完整度 < 0.80；
- 建议价 ≥ 50,000 元；
- 完整度 < 0.60（同时不出固定报价）；
- 模型输出无效或字段冲突（由 5.2 判定后转人工）。

---

## 9. 服务端（NestJS）落地指南

### 9.1 建议模块位置

```text
backend-api/src/modules/quote-agent/
  ai-quote.rules.ts           # 参数常量/配置（对应 config/ai_quote_rules.yaml）
  ai-quote.pricing.service.ts # 唯一算价服务（对应 src/quote_agent/ai_quote.py）
  ai-quote.evaluator.service.ts # 调用 LLM 生成评估参数（对应 src/quote_agent/ai_evaluator.py）
```

### 9.2 TypeScript 算价伪代码

```ts
interface AiQuoteEvaluation {
  part_type?: string;
  project_category?: string;
  project_subtype?: string;
  deliverables?: string[];
  deliverable_detail?: Record<string, number>;
  complexity_tier: 'simple' | 'normal' | 'complex';
  estimated_hours: number;
  urgency_level: number; // 会被系统覆盖
  difficulty_reason?: string[];
  case_summary?: string;
}

const RULES = {
  hourlyRate: 50,
  complexity: { simple: 0.8, normal: 1.0, complex: 1.2 },
  urgency: { 0: 0.85, 1: 0.9, 2: 0.95, 3: 1.0, 4: 1.3, 5: 1.5 },
  deliverablePrices: {
    assembly: 150,
    part_drawing: 20,
    machining_drawing: 30,
    process_card: 20,
    procedure_card: 20,
    blank_drawing: 20,
    instruction_book: 100,
  },
  enableDeliverableAddon: false,
};

function calculatePrice(evalResult: AiQuoteEvaluation, completeness?: number) {
  const tier = RULES.complexity[evalResult.complexity_tier] ?? RULES.complexity.normal;
  const urgency = RULES.urgency[evalResult.urgency_level] ?? 1.0;
  const basePrice = evalResult.estimated_hours * RULES.hourlyRate;
  const mainPrice = basePrice * tier * urgency;

  let addonPrice = 0;
  if (RULES.enableDeliverableAddon) {
    const d = evalResult.deliverable_detail ?? {};
    addonPrice =
      (d.assembly_count ?? 0) * RULES.deliverablePrices.assembly +
      (d.part_drawing_count ?? 0) * RULES.deliverablePrices.part_drawing +
      (d.machining_drawing_count ?? 0) * RULES.deliverablePrices.machining_drawing +
      (d.process_card_count ?? 0) * RULES.deliverablePrices.process_card +
      (d.procedure_card_count ?? 0) * RULES.deliverablePrices.procedure_card +
      (d.blank_drawing_count ?? 0) * RULES.deliverablePrices.blank_drawing +
      (d.instruction_book_count ?? 0) * RULES.deliverablePrices.instruction_book;
  }

  const finalPrice = Math.round(mainPrice + addonPrice);
  return {
    base_price: round2(basePrice),
    main_price: round2(mainPrice),
    addon_price: round2(addonPrice),
    final_price: finalPrice,
    complexity_tier: evalResult.complexity_tier,
    complexity_multiplier: tier,
    urgency_level: evalResult.urgency_level,
    urgency_multiplier: urgency,
    // 价格区间 + 审核判定见 8.1 / 8.2
  };
}
```

### 9.3 与 `complete` 结果字段对齐

算价结果建议写入 `result.price` 与 `result.estimated_hours`，结构保持与 [06-api-contract.md](06-api-contract.md) 一致：

```json
{
  "result": {
    "estimated_hours": { "total": 8 },
    "price": {
      "currency": "CNY",
      "minimum": 380,
      "recommended": 400,
      "maximum": 420
    },
    "manual_review_required": false,
    "manual_review_reasons": [],
    "calculation_snapshot": {
      "engine": "ai_quote",
      "rules": { "rule_set_version": "1.0.0-ai-quote", "sha256": "..." },
      "inputs": { "...": "评估参数 JSON" }
    }
  }
}
```

`calculation_snapshot` 必须保存（规则版本 + 输入参数），用于审计与复现。

### 9.4 服务端二次校验

沿用 [09-server-backend-dev.md](09-server-backend-dev.md) 第 7 节的思路，落库时按本文档 8.2 再校验一次 `manual_review_required`，不信任客户端/Worker 上传值。

### 9.5 环境变量

```env
AI_QUOTE_HOURLY_RATE=50
AI_QUOTE_COMPLEXITY_MULTIPLIERS={"simple":0.8,"normal":1.0,"complex":1.2}
AI_QUOTE_URGENCY_MULTIPLIERS={"0":0.85,"1":0.9,"2":0.95,"3":1.0,"4":1.3,"5":1.5}
AI_QUOTE_ENABLE_DELIVERABLE_ADDON=false
AI_QUOTE_REVIEW_AMOUNT_ABOVE_CNY=50000
AI_QUOTE_REVIEW_COMPLETENESS_BELOW=0.80
```

> 参数必须与本地参考实现 `config/ai_quote_rules.yaml` 保持同一版本；调价走配置变更 + 版本号升级流程。

### 9.6 服务端测试建议（把原型案例写成单测）

```ts
expect(calculatePrice({ estimated_hours: 8, complexity_tier: 'normal', urgency_level: 3 }))
  .final_price === 400;
expect(calculatePrice({ estimated_hours: 8, complexity_tier: 'complex', urgency_level: 5 }))
  .final_price === 720;
expect(calculatePrice({ estimated_hours: 2.5, complexity_tier: 'complex', urgency_level: 4 }))
  .final_price === 195;
// 加急覆盖：deliveryDaysToUrgency(7) === 3
```

---

## 10. 本仓库参考实现（本分支新增）

| 文件 | 作用 |
|---|---|
| `config/ai_quote_rules.yaml` | 参数唯一事实来源（时薪、系数、单价、参考价格表、审核阈值） |
| `src/quote_agent/ai_quote.py` | 确定性算价引擎 + 数据模型（唯一算价代码） |
| `src/quote_agent/ai_evaluator.py` | LLM 评估 Agent（提示词 + 系统覆盖加急/复杂度/工时） |
| `scripts/run_ai_quote.py` | 本地演示脚本（Ollama 可用时可全流程跑通） |
| `tests/test_ai_quote.py` | 34 个单元测试（公式、边界、覆盖逻辑、确定性） |

> 本分支的本地 Worker 已接入该方法：`PRICING_MODE=ai_quote`（默认）时，Worker 主流程走"图片理解 → 系统判定加急 → LLM 综合评估 → AiQuotePricing 算价"；`PRICING_MODE=qrs` 可回退旧规则模板引擎。

### 10.1 本地演示

```powershell
docker compose run --rm quote-agent python scripts/run_ai_quote.py `
  --requirement "设计一个电动滑板车折叠机构，需要3D模型和机构设计图" `
  --delivery-days 7
```

期望输出中 `pricing.final_price` 为百元级（工时 × 50），而非万元级。

---

## 11. 常见坑（后端开发必读）

1. **不要让 LLM 输出价格**。提示词里明确禁止价格字段，Schema 校验也要拒绝任何 `price` 字段。
2. **不要用 QRS 角色费率卡（180–650 元/h）**。AI 报价方法固定 50 元/h，这是量级回归的关键。
3. **加急等级必须系统覆盖**。模型自报的加急等级会高估（它倾向认为所有订单都紧急）。
4. **工时必须 > 0**。模型输出 0/负数/缺失时按无效处理，不得用 0 元报价。
5. **复杂度非法值回退 normal**，不要抛 500（回退后价格可复现）。
6. **参考价格表只进提示词，不进公式**。公式只有 `工时 × 50 × 复杂度 × 加急 [+ 交付物]`。
7. **报价快照必须保存**：规则版本 + 评估参数 + 计算明细，否则无法审计“为什么是这个价”。

---

## 12. 服务端改造清单（按 09-server-backend-dev.md 落地时逐项核对）

### 12.1 `complete` 结果 Schema 放行新字段（否则 422）

AI 报价模式（`calculation_snapshot.engine = "ai_quote"`）的 `complete` 结果相比 QRS 模式新增/变化：

- `calculation_snapshot` 新增：`engine`、`ai_analysis`（评估参数）、`similar_cases`、`image_summary`、`completeness_score`；
- `project_type` 可能是业务大类 key（`mechanism_design`、`drawing_modeling` 等），不再是纯 `project_types` 枚举；
- `estimated_hours.total` 允许小数（如 0.5、2.5）。

AI 报价模式与原型一致，**不做需求完整度评估、不做项目分类**：

- `result.completeness_score`、`result.classification_confidence` 为占位值 `1.0`（服务端 schema 要求 number；表示未做该项评估，不会触发服务端“完整度不足/置信度低”审核）；
- `calculation_snapshot.completeness_score` 同为 `1.0`；
- `result.missing_information`、`result.clarification_questions` 为空数组；
- **始终输出价格**：`price.recommended` 非空（金额 < 0.60 完整度等概念在 AI 报价模式不适用）。

`result.schema.ts`（ajv）需允许上述字段，否则 Worker 回传会被 422 拒绝。

### 12.2 附件下载 URL 必须 HTTPS

`download_url` 拼接处（`AGENT_FILE_BASE_URL` / uploads 路由）统一使用 `https://` 前缀；Worker 已支持跟随 301 重定向兜底，但生产环境仍应直接下发 https URL。

### 12.3 服务端二次审核适配（quote-review.service.ts）

识别 `calculation_snapshot.engine == "ai_quote"` 时：

- 保留：需求完整度 < 0.80、建议价 ≥ 阈值（`REVIEW_AMOUNT_THRESHOLD_CNY`）、`missing_information` 非空；
- 新增：`ai_analysis.urgency_level >= 4`（加急/特急）→ 强制审核；`ai_analysis.complexity_tier == "complex"` 且加急 → 强制审核；
- 不再依赖 QRS 专属字段：`estimated_standard_cycle_days`、`precision`、`complexity_factor × risk_factor × rush_factor` 乘积。

### 12.4 配置

```env
RULE_SET_VERSION=1.0.0-ai-quote
REVIEW_AMOUNT_THRESHOLD_CNY=50000   # AI 报价多为百元到几千元，此阈值几乎不触发，按业务决定是否下调
REVIEW_COMPLETENESS_BELOW=0.80
AI_QUOTE_REVIEW_URGENCY_GE=4        # 新增：加急等级 >= 该值强制审核
```

### 12.5 不需要改的部分

- 表结构（`quote_project` / `quote_task` / `quote` / `agent_credential` / `agent_audit_log`）；
- `claim` / `heartbeat` / `fail` 接口、租约、幂等、COS 签名 URL 机制；
- 若服务端存在任何“自己算价 / 改价”逻辑：**移除**，价格只信 Worker 回传（或按本文档第 6 节实现同款公式后回写）。

---

## 13. 版本记录

| 版本 | 日期 | 修改说明 | 审批人 |
|---|---|---|---|
| `1.1.0` | 2026-08-11 | 增加服务端改造清单（结果 Schema 放行、https 下载 URL、二次审核适配、配置） | 待指定 |
| `1.0.0` | 2026-08-11 | 创建：AI 报价方法（原型法）后端对接文档 | 待指定 |
