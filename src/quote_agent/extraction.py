"""需求提取 Agent：把客户表单 + 解析后的文件内容转成结构化 ProjectRequirement。"""

from __future__ import annotations

from .config import QuoteRules
from .file_parser import ParsedFile
from .llm import LLM
from .models import ProjectRequirement


def _enums(rules: QuoteRules) -> str:
    types = ", ".join(rules.project_types)
    deliverables = ", ".join(rules.deliverables)
    scopes = ", ".join(rules.professional_scope)
    return (
        "可用枚举：\n"
        f"- project_type_candidate: [{types}]\n"
        f"- requested_deliverables（数组，元素为字符串或 {{'code','quantity'}}）: [{deliverables}]\n"
        "- assembly_complexity: [simple, medium, high, very_high]\n"
        "- precision: [unspecified, normal, 0.10, 0.05, 0.01, high_precision]\n"
        f"- professional_scope（数组）: [{scopes}]"
    )


def _build_prompt(rules: QuoteRules, form: dict, parsed: list[ParsedFile]) -> str:
    sections = ["# 客户表单", f"项目名称: {form.get('project_name', '')}"]
    sections.append(f"需求描述: {form.get('customer_description', '')}")
    sections.append(f"期望交期: {form.get('deadline_date', '')}")
    sections.append(f"要求交付物: {form.get('requested_deliverables', [])}")
    sections.append("# 文件内容")
    for pf in parsed:
        sections.append(f"## {pf.original_name} ({pf.mime_type})")
        if pf.text:
            sections.append(pf.text[:12_000])
        if pf.table_summary:
            sections.append(f"表格摘要: {pf.table_summary}")
        if pf.ocr_pending:
            sections.append("[图片内容暂不可读，需向客户补充询问]")
    return "\n\n".join(sections)


class ExtractionAgent:
    SYSTEM_PROMPT = """你是机械设计报价系统的资料理解助手。
职责：仅从给定资料中提取结构化需求，不编造、不猜测。
硬性规则：
1. 未知字段写 null 或空列表，禁止伪造参数。
2. 不得输出任何价格、工时、报价金额或费率。
3. 不确定的内容必须列入 unknowns / risks / clarification_questions。
4. missing_critical_interface、missing_load_or_force、scope_uncertain 等布尔字段，仅在资料明确时才可为 false。
5. requested_deliverables 只能使用给定枚举，并按数量使用 {"code": "...", "quantity": N}。
输出 JSON 可包含以下字段（未知的省略或为 null）：
project_type_candidate, requested_deliverables, deadline_workdays, function_description,
provided_materials, revision_policy, unknowns, risks,
part_count, assembly_complexity, precision, professional_scope,
missing_critical_interface, missing_load_or_force, unverified_solution,
multi_party_coordination, new_customer, high_responsibility_industry, scope_uncertain,
acceptance_criteria_known, workpiece_info_known, cycle_time_known,
utilities_known, safety_requirements_known,
assumptions, exclusions, clarification_questions, evidence
"""

    def __init__(self, llm: LLM, rules: QuoteRules):
        self.llm = llm
        self.rules = rules

    def extract(self, form: dict, parsed: list[ParsedFile]) -> ProjectRequirement:
        user = _build_prompt(self.rules, form, parsed) + "\n\n" + _enums(self.rules)
        result = self.llm.generate_structured(self.SYSTEM_PROMPT, user, ProjectRequirement)
        assert isinstance(result, ProjectRequirement)
        return result
