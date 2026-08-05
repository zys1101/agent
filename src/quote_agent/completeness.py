"""需求完整度确定性计算（QRS 9.1 / 9.2）。

LLM 输出的 completeness_score 不可信；Worker 必须用本模块按关键字段状态重算。
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import QuoteRules
from .models import ProjectRequirement

STATE_WEIGHTS = {
    "confirmed": 1.0,
    "inferred": 0.5,
    "unknown": 0.0,
    "not_applicable": None,  # 不计入分母
}

CONFIRMED_PRECISION = {"normal", "0.10", "0.05", "0.01"}


@dataclass
class CompletenessResult:
    score: float
    field_states: dict[str, str]
    missing_key_fields: list[str]


def _state_for(requirement: ProjectRequirement, field: str) -> str:
    """根据结构化需求推导字段状态，所有规则可复现。"""
    if field == "function_description":
        return "confirmed" if requirement.function_description.strip() else "unknown"
    if field == "deliverables":
        return "confirmed" if requirement.requested_deliverables else "unknown"
    if field == "deadline":
        return "confirmed" if requirement.deadline_workdays is not None else "unknown"
    if field == "materials":
        return "confirmed" if requirement.provided_materials or requirement.evidence else "unknown"
    if field == "revision_policy":
        return "confirmed" if requirement.revision_policy in {"limited", "unlimited"} else "unknown"
    if field == "scope":
        return "confirmed" if not requirement.scope_uncertain else "unknown"
    if field == "acceptance":
        return "confirmed" if requirement.acceptance_criteria_known else "unknown"
    if field == "workpiece":
        return "confirmed" if requirement.workpiece_info_known else "unknown"
    if field == "interface":
        return "unknown" if requirement.missing_critical_interface else "confirmed"
    if field == "load":
        return "unknown" if requirement.missing_load_or_force else "confirmed"
    if field == "cycle_time":
        return "confirmed" if requirement.cycle_time_known else "unknown"
    if field == "precision":
        if requirement.precision in CONFIRMED_PRECISION:
            return "confirmed"
        if requirement.precision == "high_precision":
            return "inferred"
        return "unknown"
    if field == "utilities":
        return "confirmed" if requirement.utilities_known else "unknown"
    if field == "safety":
        return "confirmed" if requirement.safety_requirements_known else "unknown"
    return "unknown"


def calculate_completeness(
    requirement: ProjectRequirement,
    rules: QuoteRules,
) -> CompletenessResult:
    family = rules.project_families.get(requirement.project_type_candidate, "generic")
    template = rules.completeness_templates.get(family, rules.completeness_templates["generic"])

    states: dict[str, str] = {}
    total_weight = 0.0
    weighted = 0.0
    missing: list[str] = []

    for field, field_rule in template.fields.items():
        state = _state_for(requirement, field)
        states[field] = state
        state_weight = STATE_WEIGHTS[state]
        if state_weight is None:
            continue
        total_weight += field_rule.weight
        weighted += field_rule.weight * state_weight
        if state == "unknown":
            missing.append(field)

    score = weighted / total_weight if total_weight > 0 else 1.0
    return CompletenessResult(
        score=round(score, 4),
        field_states=states,
        missing_key_fields=missing,
    )


def apply_deterministic_overrides(
    requirement: ProjectRequirement,
    rules: QuoteRules,
) -> CompletenessResult:
    """Worker 在 VALIDATE_REQUIREMENTS 阶段调用：重算完整度并回填派生字段。"""
    result = calculate_completeness(requirement, rules)
    requirement.completeness_score = result.score
    requirement.field_states = result.field_states
    requirement.missing_load_or_force = result.field_states.get("load") == "unknown"
    requirement.missing_critical_interface = result.field_states.get("interface") == "unknown"
    requirement.missing_workpiece_info = result.field_states.get("workpiece") == "unknown"
    requirement.missing_acceptance_criteria = result.field_states.get("acceptance") == "unknown"
    requirement.unlimited_revisions = requirement.revision_policy == "unlimited"
    return result
