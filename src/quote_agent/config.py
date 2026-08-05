"""报价规则配置：加载、校验 quote_rules.yaml。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

REVIEW_SEVERITIES = ("none", "suggested", "required")

DEFAULT_RULES_PATH = Path(__file__).resolve().parents[2] / "config" / "quote_rules.yaml"


class Rate(BaseModel):
    internal: float = Field(ge=0)
    external: float = Field(ge=0)


class OperatingAssumptions(BaseModel):
    effective_engineering_hours_per_day: float = Field(gt=0)
    default_revision_rounds: int = Field(ge=0)
    min_quote_amount_cny: float = Field(ge=0)
    quote_rounding_unit_cny: float = Field(gt=0)
    communication_and_revision_pct: float = Field(ge=0)
    max_complexity_factor: float = Field(ge=1.0)
    max_risk_buffer_ratio: float = Field(ge=0)
    fixed_fees_cny: float = 0.0


class PriceRangeTier(BaseModel):
    min_score: float = Field(ge=0, le=1)
    min_factor: float = Field(gt=0)
    max_factor: float = Field(gt=0)

    @model_validator(mode="after")
    def _check_factors(self) -> "PriceRangeTier":
        if self.max_factor < self.min_factor:
            raise ValueError("max_factor must be >= min_factor")
        return self


class ProjectTypeRule(BaseModel):
    base_hours: float = Field(gt=0)
    base_cycle_days: float = Field(gt=0)
    default_role: str
    manual_review: bool = False


class DeliverableRule(BaseModel):
    default_hours: float = Field(gt=0)
    role: str
    per_unit: bool = False
    manual_review: bool = False


class CompletenessField(BaseModel):
    weight: float = Field(gt=0)


class CompletenessTemplate(BaseModel):
    fields: dict[str, CompletenessField]


class BandRule(BaseModel):
    max_count: int | None = None
    factor: float = Field(ge=1.0)
    review: Literal["none", "suggested", "required"] = "none"


class ComplexityRule(BaseModel):
    factor: float = Field(ge=1.0)
    review: Literal["none", "suggested", "required"] = "none"


class PrecisionRule(ComplexityRule):
    note: str | None = None


class ScopeRule(BaseModel):
    factor: float = Field(default=1.0, ge=1.0)
    add_hours: float = Field(default=0.0, ge=0)
    role: str | None = None
    review: Literal["none", "suggested", "required"] = "none"


class RiskTier(BaseModel):
    min_score: float = Field(ge=0, le=1)
    factor: float = Field(ge=1.0)


class RiskCondition(BaseModel):
    factor: float = Field(ge=1.0)
    review: Literal["none", "suggested", "required"] = "none"


class RiskFactors(BaseModel):
    completeness_tiers: dict[str, RiskTier]
    conditions: dict[str, RiskCondition]


class RushBand(BaseModel):
    min_ratio: float = Field(ge=0)
    factor: float = Field(ge=1.0)
    review: Literal["none", "suggested", "required"] = "none"


class ManualReviewConfig(BaseModel):
    quote_amount_above_cny: float = Field(ge=0)
    classification_confidence_below: float = Field(ge=0, le=1)
    completeness_below: float = Field(ge=0, le=1)
    factor_product_above: float = Field(ge=1.0)


class QuoteRules(BaseModel):
    rule_set_version: str
    currency: str
    quote_type_default: str = "budget_range"
    operating_assumptions: OperatingAssumptions
    rate_cards: dict[str, Rate]
    price_range_by_completeness: dict[str, PriceRangeTier]
    project_types: dict[str, ProjectTypeRule]
    deliverables: dict[str, DeliverableRule]
    completeness_templates: dict[str, CompletenessTemplate]
    project_families: dict[str, str]
    part_count_factors: dict[str, BandRule]
    assembly_complexity: dict[str, ComplexityRule]
    precision: dict[str, PrecisionRule]
    professional_scope: dict[str, ScopeRule]
    risk_factors: RiskFactors
    rush_factors: dict[str, RushBand]
    manual_review: ManualReviewConfig
    source_sha256: str = ""

    @classmethod
    def load(cls, path: str | Path = DEFAULT_RULES_PATH) -> "QuoteRules":
        path = Path(path)
        raw = path.read_bytes()
        data = yaml.safe_load(raw)
        rules = cls.model_validate(data)
        rules.source_sha256 = hashlib.sha256(raw).hexdigest()
        return rules

    @model_validator(mode="after")
    def _cross_validate(self) -> "QuoteRules":
        rate_names = set(self.rate_cards)
        if not self.completeness_templates:
            raise ValueError("completeness_templates must not be empty")
        unknown_families = set(self.project_families.values()) - set(self.completeness_templates)
        if unknown_families:
            raise ValueError(f"project_families reference unknown templates: {sorted(unknown_families)}")
        missing_families = set(self.project_types) - set(self.project_families)
        if missing_families:
            raise ValueError(f"project_types missing family mapping: {sorted(missing_families)}")
        for code, pt in self.project_types.items():
            if pt.default_role not in rate_names:
                raise ValueError(f"project_types[{code}].default_role unknown: {pt.default_role}")
        for code, dv in self.deliverables.items():
            if dv.role not in rate_names:
                raise ValueError(f"deliverables[{code}].role unknown: {dv.role}")
        for code, sc in self.professional_scope.items():
            if sc.role is not None and sc.role not in rate_names:
                raise ValueError(f"professional_scope[{code}].role unknown: {sc.role}")

        tiers = list(self.price_range_by_completeness.values())
        scores = sorted(tier.min_score for tier in tiers)
        if scores and scores[0] < 0.60:
            raise ValueError("price_range_by_completeness lowest min_score must be >= 0.60")

        risk_tiers = sorted(
            self.risk_factors.completeness_tiers.values(),
            key=lambda t: t.min_score,
            reverse=True,
        )
        if not risk_tiers or risk_tiers[-1].min_score != 0.0:
            raise ValueError("risk completeness tiers must cover 0.0")

        bands = sorted(
            self.rush_factors.values(),
            key=lambda b: b.min_ratio,
            reverse=True,
        )
        if not bands or bands[-1].min_ratio != 0.0:
            raise ValueError("rush_factors must cover ratio 0.0")

        count_bands = sorted(
            (b for b in self.part_count_factors.values() if b.max_count is not None),
            key=lambda b: b.max_count,
        )
        open_bands = [b for b in self.part_count_factors.values() if b.max_count is None]
        if len(open_bands) != 1:
            raise ValueError("part_count_factors must have exactly one open-ended band")
        for prev, cur in zip(count_bands, count_bands[1:]):
            if prev.max_count >= cur.max_count:
                raise ValueError("part_count_factors bands must be strictly increasing")
        return self
