# 业务大类分类规范（对齐官网案例/设计类型分类）

- **文档编号**：TXN-001
- **版本**：0.1.0-draft
- **数据来源**：https://www.sonsentech.cn/cases（案例页 13 个大类，与 /design-types 页“13 大设计领域”一致）
- **规则集版本**：`quote_rules.yaml` → `0.2.0-draft`

## 1. 背景

AI 报价系统把客户需求分类到具体的 `project_type`（如 `pneumatic_press_fixture`），
但对外展示和服务目录需要更上层的“业务大类”。官网案例页与设计类型页已经定义了 13 个大类，
本规范将其作为报价数据库的统一分类维度，并给出与内部 `project_type` 的映射。

## 2. 13 个业务大类（key = 代码内标识，name = 对外名称）

| key | id | name | 说明 |
|---|---|---|---|
| `machine_design` | 1 | 整机设备设计 | 自动化产线、专用设备、整机产品 |
| `mechanism_design` | 2 | 机械机构设计 | 传动、连杆、凸轮等机构设计与优化 |
| `product_structure` | 3 | 产品结构设计 | 产品堆叠、装配、强度与可制造性 |
| `sheet_metal_cabinet` | 4 | 钣金机柜设计 | 机柜、控制箱、操作台、钣金件 |
| `welding_frame` | 5 | 焊接机架设计 | 焊接机架、底座、支撑结构 |
| `tooling_fixture` | 6 | 工装夹具设计 | 加工/装配/检测环节专用工装夹具 |
| `test_fixture` | 7 | 测试治具设计 | 功能测试、可靠性测试治具与检测设备 |
| `drawing_modeling` | 8 | 工程制图与建模 | 三维建模、二维工程图、国标规范 |
| `reverse_engineering` | 9 | 产品改造与逆向 | 三维扫描、逆向建模、改造升级 |
| `simulation_analysis` | 10 | 仿真分析 | FEA、运动学、热流体等仿真验证 |
| `process_production` | 11 | 工艺与生产方案 | 产线布局、节拍分析、工艺规划 |
| `drawing_standardization` | 12 | 图纸整理与标准化 | 已有图纸整理、规范化、标准化 |
| `technical_consulting` | 13 | 技术咨询 | 设计评审、技术答疑、方案论证 |

> `id` 与官网案例页的分类 ID 一致；key 用于代码与数据库，name 用于对外展示。

## 3. `project_type` → 业务大类映射

`config/quote_rules.yaml` 中每个 `project_types` 条目都带 `category` 字段：

| project_type | category（业务大类） |
|---|---|
| `simple_part` | `product_structure` |
| `machined_part` | `product_structure` |
| `sheet_metal_part` | `sheet_metal_cabinet` |
| `drawing_conversion` | `drawing_modeling` |
| `inspection_fixture` | `tooling_fixture` |
| `assembly_fixture` | `tooling_fixture` |
| `welding_fixture` | `tooling_fixture` |
| `pneumatic_press_fixture` | `tooling_fixture` |
| `loading_module` | `machine_design` |
| `single_station_machine` | `machine_design` |
| `design_review` | `technical_consulting` |

当前 `mechanism_design`、`test_fixture`、`reverse_engineering`、`simulation_analysis`、
`process_production`、`drawing_standardization` 等大类还没有内部报价类型，
先由官网案例与后续需求分类覆盖；待业务确认后再扩展 `project_types`（需走 QRS 变更流程）。

## 4. 分类输出

`ProjectClassification` 新增 `category` 字段；`QuoteCalculation` 与 Worker 回传结果新增
`category`（key）与 `category_name`（对外名称）。

- 分类 Agent 先选业务大类，再在大类下选最具体的 `project_type`。
- 模型未返回或返回无效 `category` 时，按 `project_type` 的归属映射回填（兼容旧模型）。
- 报价引擎采用分类结果中的大类；无效时按 `project_type` 映射兜底。

## 5. 案例数据库分类

`examples/knowledge_cases/` 下所有案例记录（`CaseRecord`）都带 `category` 字段：

- 已有 3 个脱敏内部案例已补齐大类（如 `case_001` → `tooling_fixture`）。
- `site_cases.json` 收录官网案例页公开的 56 个案例，按官网分类 ID 映射到大类，
  `project_type` 取最贴近的内部类型（无对应类型时用大类 key）。
- RAG 检索同时支持按 `project_type`、`category` 过滤；两者同时给出时任一命中即可，
  保证按大类归类的官网案例也能被检索到。

## 6. 同步/维护

- 官网分类调整时：同步 `case_categories`（key/name/id）并升级 `rule_set_version`。
- 新增内部 `project_type`：必须指定 `category`，且 `category` 必须存在于 `case_categories`（配置校验会拦截）。
