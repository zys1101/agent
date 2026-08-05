"""确定性报价引擎。

遵循 QRS-001：
- LLM 不参与价格计算；本引擎是唯一计算价格的模块。
- 输出完整的工时明细、价格区间、应用规则和人工审核判定。
- 相同输入 + 相同规则版本 => 完全相同的输出。
"""

from __future__ import annotations

import math

from .config import QuoteRules
from .models import (
    AppliedFactors,
    AppliedRule,
    DeliverableRequest,
    EstimatedHours,
    PriceRange,
    ProjectClassification,
    ProjectRequirement,
    QuoteCalculation,
)

ENGINE_VERSION = "0.1.0"


def _round_to_unit(value: float, unit: float) -> float:
    if unit <= 0:
        return value
    return int(value / unit + 0.5) * unit


def _round2(value: float) -> float:
    return round(value + 1e-9, 2)


def _normalize_deliverables(items: list[str | DeliverableRequest]) -> list[DeliverableRequest]:
    result: list[DeliverableRequest] = []
    for item in items:
        if isinstance(item, DeliverableRequest):
            result.append(item)
        else:
            result.append(DeliverableRequest(code=item, quantity=1))
    return result


class QuoteEngine:
    def __init__(self, rules: QuoteRules):
        self.rules = rules

    def calculate(
        self,
        requirement: ProjectRequirement,
        classification: ProjectClassification | None = None,
    ) -> QuoteCalculation:
        rules = self.rules
        applied: list[AppliedRule] = []
        required_reasons: list[str] = []
        suggested_reasons: list[str] = []

        def add_rule(rule_id: str, description: str) -> None:
            applied.append(AppliedRule(rule_id=rule_id, description=description))

        def add_review(severity: str, reason_id: str) -> None:
            if severity == "required":
                required_reasons.append(reason_id)
            elif severity == "suggested":
                suggested_reasons.append(reason_id)

        project_type = (
            classification.project_type if classification is not None else requirement.project_type_candidate
        )
        if classification is not None and classification.confidence is not None:
            if classification.confidence < rules.manual_review.classification_confidence_below:
                required_reasons.append("confidence_below_0_75")

        pt = rules.project_types.get(project_type)
        if pt is None:
            raise ValueError(f"unknown project_type: {project_type}")

        # ---- 1. 基础工时 ----
        base_hours = pt.base_hours
        role_hours: dict[str, float] = {pt.default_role: base_hours}
        add_rule("base_hours", f"{project_type} 基础工时 {base_hours:g}h（默认角色 {pt.default_role}）")

        # ---- 2. 交付物附加工时 ----
        deliverable_hours = 0.0
        for item in _normalize_deliverables(requirement.requested_deliverables):
            dv = rules.deliverables.get(item.code)
            if dv is None:
                raise ValueError(f"unknown deliverable: {item.code}")
            quantity = item.quantity if dv.per_unit else 1
            hours = dv.default_hours * quantity
            role_hours[dv.role] = role_hours.get(dv.role, 0.0) + hours
            deliverable_hours += hours
            if dv.manual_review:
                required_reasons.append(f"deliverable_{item.code}")
            add_rule(
                "deliverable",
                f"交付物 {item.code} x{quantity} = {hours:g}h（角色 {dv.role}）",
            )

        # ---- 3. 专业范围附加工时与系数 ----
        scope_add_hours = 0.0
        scope_factors: list[float] = []
        for code in requirement.professional_scope:
            sc = rules.professional_scope.get(code)
            if sc is None:
                raise ValueError(f"unknown professional_scope: {code}")
            scope_factors.append(sc.factor)
            if sc.add_hours:
                role = sc.role or pt.default_role
                role_hours[role] = role_hours.get(role, 0.0) + sc.add_hours
                scope_add_hours += sc.add_hours
                add_rule(
                    "professional_scope",
                    f"专业范围 {code} 附加工时 {sc.add_hours:g}h（角色 {role}）",
                )
            add_review(sc.review, f"scope_{code}")
            if sc.factor != 1.0:
                add_rule("professional_scope", f"专业范围 {code} 工时系数 x{sc.factor:g}")

        # ---- 4. 复杂度系数（数量 + 装配 + 精度 + 专业，上限 2.00）----
        part_band_name, part_band = self._part_band(requirement.part_count)
        assembly = rules.assembly_complexity[requirement.assembly_complexity]
        precision = rules.precision[requirement.precision]

        if part_band is not None:
            add_review(part_band.review, f"parts_{part_band_name}")
            add_rule("complexity_part_count", f"非标零件数 {requirement.part_count} 工时系数 x{part_band.factor:g}")
        add_review(assembly.review, "assembly_very_high")
        if assembly.factor != 1.0:
            add_rule("complexity_assembly", f"装配复杂度 {requirement.assembly_complexity} 工时系数 x{assembly.factor:g}")
        add_review(precision.review, "precision_0_05_or_stricter")
        if precision.factor != 1.0:
            add_rule(
                "complexity_precision",
                f"精度 {requirement.precision} 工时系数 x{precision.factor:g}"
                + (f"（{precision.note}）" if precision.note else ""),
            )

        complexity_raw = (
            (part_band.factor if part_band is not None else 1.0)
            * assembly.factor
            * precision.factor
            * math.prod(scope_factors)
        )
        complexity_factor = min(complexity_raw, rules.operating_assumptions.max_complexity_factor)
        if complexity_raw > rules.operating_assumptions.max_complexity_factor:
            required_reasons.append("complexity_factor_above_cap")
            add_rule(
                "complexity_cap",
                f"合并复杂度 {complexity_raw:.3f} 超过上限 {rules.operating_assumptions.max_complexity_factor:g}，"
                "按上限取值并强制人工拆项评估",
            )

        # ---- 5. 调整前工时 -> 沟通/修改 -> 风险缓冲 ----
        pre_adjustment = base_hours + deliverable_hours + scope_add_hours
        complexity_adjustment = pre_adjustment * (complexity_factor - 1.0)
        communication_hours = pre_adjustment * rules.operating_assumptions.communication_and_revision_pct
        adjusted_before_risk = pre_adjustment + complexity_adjustment + communication_hours

        risk_factor, risk_tier_id = self._risk_factor(requirement, required_reasons, add_rule)
        risk_buffer_ratio = min(risk_factor - 1.0, rules.operating_assumptions.max_risk_buffer_ratio)
        risk_buffer_hours = adjusted_before_risk * risk_buffer_ratio
        total_hours = adjusted_before_risk + risk_buffer_hours
        add_rule("risk_completeness", f"需求完整度风险档 {risk_tier_id} 系数 x{risk_factor:g}")
        if risk_buffer_ratio > 0:
            add_rule("risk_buffer", f"风险缓冲工时 = 调整前工时 x {risk_buffer_ratio:.2%}")

        # 附加/沟通/风险工时按角色比例分摊
        spread_hours = complexity_adjustment + communication_hours + risk_buffer_hours
        if pre_adjustment > 0:
            for role, hours in list(role_hours.items()):
                role_hours[role] = hours + spread_hours * (hours / pre_adjustment)
        else:
            role_hours[pt.default_role] = role_hours.get(pt.default_role, 0.0) + spread_hours

        # ---- 6. 标准周期与加急 ----
        cycle_days = self._standard_cycle_days(
            requirement=requirement,
            total_hours=total_hours,
            base_hours=base_hours,
            base_cycle_days=pt.base_cycle_days,
        )
        rush_factor, rush_band = self._rush_factor(requirement, cycle_days, required_reasons, add_rule)

        # ---- 7. 价格 ----
        unit = rules.operating_assumptions.quote_rounding_unit_cny
        fixed_fees = rules.operating_assumptions.fixed_fees_cny + requirement.fixed_fees_cny
        fee = sum(hours * rules.rate_cards[role].external for role, hours in role_hours.items())
        recommended_raw = fee * rush_factor * risk_factor + fixed_fees
        recommended = _round_to_unit(recommended_raw, unit)

        quote_type = rules.quote_type_default
        price: PriceRange | None
        tier = self._price_range_tier(requirement.completeness_score)
        if tier is None:
            quote_type = "preliminary_research"
            required_reasons.append("completeness_below_0_60")
            price = PriceRange(currency=rules.currency)
        else:
            price = PriceRange(
                currency=rules.currency,
                minimum=_round_to_unit(recommended_raw * tier.min_factor, unit),
                recommended=recommended,
                maximum=_round_to_unit(recommended_raw * tier.max_factor, unit),
            )

        # ---- 8. 强制审核判定 ----
        if pt.manual_review:
            required_reasons.append("project_type_manual_review")
        if requirement.completeness_score < rules.manual_review.completeness_below:
            required_reasons.append("completeness_below_0_80")
        if (
            price is not None
            and price.recommended is not None
            and price.recommended >= rules.manual_review.quote_amount_above_cny
        ):
            required_reasons.append("quote_amount_above_threshold")
        if requirement.scope_uncertain:
            required_reasons.append("scope_uncertain")
        if requirement.missing_workpiece_info:
            required_reasons.append("missing_workpiece_info")
        if requirement.missing_acceptance_criteria:
            required_reasons.append("missing_acceptance_criteria")
        if requirement.input_conflicts:
            required_reasons.append("input_conflicts")
        if requirement.deadline_workdays is None:
            required_reasons.append("deadline_unknown")
        if (
            complexity_raw * risk_factor * rush_factor
            > rules.manual_review.factor_product_above
        ):
            required_reasons.append("factor_product_above_2")

        required_reasons = list(dict.fromkeys(required_reasons))
        suggested_reasons = list(dict.fromkeys(suggested_reasons))

        snapshot = {
            "engine_version": ENGINE_VERSION,
            "rules": {
                "rule_set_version": rules.rule_set_version,
                "sha256": rules.source_sha256,
            },
            "inputs": requirement.model_dump(mode="json"),
            "classification": (
                classification.model_dump(mode="json") if classification is not None else None
            ),
        }

        estimated_hours = EstimatedHours(
            base=_round2(base_hours),
            deliverables=_round2(deliverable_hours),
            professional_scope=_round2(scope_add_hours),
            complexity_adjustment=_round2(complexity_adjustment),
            communication_and_revision=_round2(communication_hours),
            risk_buffer=_round2(risk_buffer_hours),
            total=_round2(total_hours),
        )

        return QuoteCalculation(
            rule_set_version=rules.rule_set_version,
            quote_type=quote_type,
            currency=rules.currency,
            project_type=project_type,
            estimated_hours=estimated_hours,
            hours_by_role={role: _round2(h) for role, h in sorted(role_hours.items())},
            estimated_standard_cycle_days=_round2(cycle_days),
            applied_rules=applied,
            applied_factors=AppliedFactors(
                complexity_factor=_round2(complexity_factor),
                risk_factor=_round2(risk_factor),
                rush_factor=_round2(rush_factor),
                risk_buffer_ratio=_round2(risk_buffer_ratio),
            ),
            price=price,
            manual_review_required=bool(required_reasons),
            review_reasons=required_reasons,
            suggested_review_reasons=suggested_reasons,
            calculation_snapshot=snapshot,
        )

    # ---- 内部辅助 ----

    def _part_band(self, part_count: int | None) -> tuple[str | None, object | None]:
        if part_count is None:
            return None, None
        for name, band in self.rules.part_count_factors.items():
            if band.max_count is None or part_count <= band.max_count:
                return name, band
        return None, None

    def _price_range_tier(self, completeness: float):
        tiers = sorted(
            self.rules.price_range_by_completeness.values(),
            key=lambda t: t.min_score,
            reverse=True,
        )
        for tier in tiers:
            if completeness >= tier.min_score:
                return tier
        return None

    def _risk_factor(
        self,
        requirement: ProjectRequirement,
        required_reasons: list[str],
        add_rule,
    ) -> tuple[float, str]:
        factor = 1.0
        tier_id = "none"
        for name, tier in sorted(
            self.rules.risk_factors.completeness_tiers.items(),
            key=lambda kv: kv[1].min_score,
            reverse=True,
        ):
            if requirement.completeness_score >= tier.min_score:
                factor *= tier.factor
                tier_id = name
                break
        for code, cond in self.rules.risk_factors.conditions.items():
            if getattr(requirement, code, False):
                factor *= cond.factor
                add_rule("risk_condition", f"风险条件 {code} 系数 x{cond.factor:g}")
                if cond.review == "required":
                    required_reasons.append(f"risk_{code}")
        return factor, tier_id

    def _standard_cycle_days(
        self,
        requirement: ProjectRequirement,
        total_hours: float,
        base_hours: float,
        base_cycle_days: float,
    ) -> float:
        additional = max(total_hours - base_hours, 0.0)
        return base_cycle_days + additional / self.rules.operating_assumptions.effective_engineering_hours_per_day

    def _rush_factor(
        self,
        requirement: ProjectRequirement,
        cycle_days: float,
        required_reasons: list[str],
        add_rule,
    ) -> tuple[float, str]:
        if requirement.deadline_workdays is None:
            add_rule("rush", "交期未提供，加急系数按 1.00（人工审核）")
            return 1.0, "unknown"
        ratio = requirement.deadline_workdays / cycle_days
        for name, band in sorted(
            self.rules.rush_factors.items(),
            key=lambda kv: kv[1].min_ratio,
            reverse=True,
        ):
            if ratio >= band.min_ratio:
                add_rule(
                    "rush",
                    f"交期比例 {ratio:.2f}（{requirement.deadline_workdays:g}/{cycle_days:.2f} 工作日）"
                    f" -> {name} 系数 x{band.factor:g}",
                )
                if band.review == "required":
                    required_reasons.append("rush_ratio_below_0_70")
                return band.factor, name
        add_rule("rush", f"交期比例 {ratio:.2f} 无匹配档位，按 1.00")
        return 1.0, "unknown"
