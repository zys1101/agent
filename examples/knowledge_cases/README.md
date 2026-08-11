# 知识案例目录说明

本目录存放 **脱敏后的历史案例**，供 Qdrant RAG 检索与回归测试使用。

## 数据来源

- `case_001_pneumatic_press.json`、`case_002_sheet_metal.json`：手工整理的样例案例；
- `site_cases.json`：官网案例页公开案例（脱敏）；
- `ai_quote_historical_cases.json`：来自 `E:\Program Files\AI_quote\data\CASE_*.json` 的 **14 条真实成交订单案例**（AI 报价原型验证过的数据）。

## ai_quote_historical_cases.json 脱敏规则

原始真实案例只保留了 RAG 需要的字段，并做了以下处理：

1. **不包含任何图片/附件**：原案例的 `images` 路径、外部图床 URL（chatglm 等）全部移除；
2. **不包含客户个人信息**：无客户名称、公司、联系方式、地址；原案例中本就不含此类信息，另做了全文检查；
3. **摘要重写**：以 `case_summary` / `process_route` 为基础重写为关键词丰富的技术描述，移除"价格 XX 元"等成交细节，避免 LLM 被历史价格锚定（价格只保留在 `price_range_cny` 供程序使用）；
4. **ID 重映射**：`aiq_0001` ~ `aiq_0014`，映射关系见下表；
5. **价格区间**：`price_range_cny` 用历史成交价 `final_price` 作为精确参考值 `[final, final]`，`typical_hours` 用原 `estimated_hours`；
6. **类型映射**：`project_type` / `category` 映射到本仓库的枚举（`config/quote_rules.yaml` 的 `project_types` 与 `case_categories`），无合适设计类型时使用业务大类 key（与 `site_cases.json` 的做法一致）。

## ID 映射

| 新 ID | 原始 case_id | 内容 |
|---|---|---|
| aiq_0001 | CASE_20260710_0001 | 轴套类零件机械加工工艺设计（毛坯图/过程卡/工序卡） |
| aiq_0002 | CASE_20260710_0002 | 电动滑板车角度调节与折叠机构设计 |
| aiq_0003 | CASE_20260710_0003 | 塑封机自动化设备设计 |
| aiq_0004 | CASE_20260710_0004 | 液压辊压设备概念设计 |
| aiq_0005 | CASE_20260710_0005 | 机床门板改造为滑动护罩 |
| aiq_0006 | CASE_20260710_0006 | 2T1R 并联机构设计 |
| aiq_0007 | CASE_20260710_0007 | 曲柄机构设计 |
| aiq_0008 | CASE_20260710_0008 | Z 轴移动机构建模 |
| aiq_0009 | CASE_20260710_0009 | 夹爪机构建模与运动仿真 |
| aiq_0010 | CASE_20260710_0010 | 螺旋桨齿轮机构修改 |
| aiq_0011 | CASE_20260710_0011 | 连杆机构设计 |
| aiq_0012 | CASE_20260710_0012 | 壳体类产品 Creo 三维建模 |
| aiq_0013 | CASE_20260710_0013 | 游戏摇杆 3D 建模 |
| aiq_0014 | CASE_20260710_0014 | 合页产品建模 |

## 使用方式

```powershell
# 校验格式（不连 Qdrant/Ollama）
docker compose run --rm quote-agent python scripts/seed_rag_cases.py --dry-run

# 真实导入 Qdrant
docker compose run --rm quote-agent python scripts/seed_rag_cases.py
```
