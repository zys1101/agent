"""AI 报价方法（AI_quote 原型）确定性算价引擎。

设计原则与原型一致：**AI 定性，代码定量**。
- LLM 只输出：零件类型、项目分类、交付物数量、复杂度档位、预估工时、加急等级（仅参考）；
- 本模块是唯一计算价格的代码：`最终价格 = 预估工时 × 50元/h × 复杂度系数 × 加急系数 [+ 交付物固定单价]`；
- 加急等级由系统按交付天数判定后强制覆盖，LLM 不得自行决定；
- 相同输入 + 相同规则版本 => 完全相同的输出。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator

DEFAULT_AI_QUOTE_RULES_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "ai_quote_rules.yaml"
)

COMPLEXITY_TIERS = ("simple", "normal", "complex")


class AiQuoteDeliverableDetail(BaseModel):
    """交付物数量（由 LLM 从需求/图片中提取，整数）。"""

    assembly_count: int = Field(default=0, ge=0)
    part_drawing_count: int = Field(default=0, ge=0)
    machining_drawing_count: int = Field(default=0, ge=0)
    process_card_count: int = Field(default=0, ge=0)
    procedure_card_count: int = Field(default=0, ge=0)
    blank_drawing_count: int = Field(default=0, ge=0)
    model_count: int = Field(default=0, ge=0)
    instruction_book_count: int = Field(default=0, ge=0)


class AiQuoteEvaluation(BaseModel):
    """LLM 评估结果：只有定性参数，没有任何价格字段。

    字段与 AI_quote 原型 PROMPT_EVALUATION 的输出一致。
    """

    part_type: str = ""
    project_category: str = ""
    project_subtype: str = ""
    deliverables: list[str] = []
    deliverable_detail: AiQuoteDeliverableDetail = Field(default_factory=AiQuoteDeliverableDetail)
    # 用普通 str 而非 Literal：模型偶发输出非法值由评估器兜底回退 normal（与原型宽容行为一致）
    complexity_tier: str = "normal"
    estimated_hours: float = Field(ge=0)
    urgency_level: int = Field(default=3, ge=0, le=5)
    difficulty_reason: list[str] = []
    case_summary: str = ""


class PriceRangeTier(BaseModel):
    min_score: float = Field(ge=0, le=1)
    min_factor: float = Field(gt=0)
    max_factor: float = Field(gt=0)

    @model_validator(mode="after")
    def _check_factors(self) -> "PriceRangeTier":
        if self.max_factor < self.min_factor:
            raise ValueError("max_factor must be >= min_factor")
        return self


class ManualReviewConfig(BaseModel):
    quote_amount_above_cny: float = Field(ge=0)
    completeness_below: float = Field(ge=0, le=1)
    preliminary_research_below: float = Field(ge=0, le=1)


class AiQuoteRules(BaseModel):
    """AI 报价方法规则集（加载 config/ai_quote_rules.yaml）。"""

    rule_set_version: str
    currency: str = "CNY"
    quote_type_default: str = "budget_range"
    hourly_rate: float = Field(gt=0)
    complexity_multipliers: dict[str, float]
    urgency_multipliers: dict[int, float]
    urgency_labels: dict[int, str]
    deliverable_prices: dict[str, float]
    enable_deliverable_addon: bool = False
    quote_rounding_unit_cny: float = Field(gt=0)
    price_band_min: float = Field(default=0.85, gt=0, le=1)
    price_band_max: float = Field(default=1.15, ge=1)
    price_range_by_completeness: dict[str, PriceRangeTier]
    manual_review: ManualReviewConfig
    reference_price_table: str
    historical_price_reference: str = ""
    source_sha256: str = ""

    @classmethod
    def load(cls, path: str | Path = DEFAULT_AI_QUOTE_RULES_PATH) -> "AiQuoteRules":
        path = Path(path)
        raw = path.read_bytes()
        data = yaml.safe_load(raw)
        rules = cls.model_validate(data)
        rules.source_sha256 = hashlib.sha256(raw).hexdigest()
        return rules

    @model_validator(mode="after")
    def _cross_validate(self) -> "AiQuoteRules":
        missing_tiers = set(COMPLEXITY_TIERS) - set(self.complexity_multipliers)
        if missing_tiers:
            raise ValueError(f"complexity_multipliers missing tiers: {sorted(missing_tiers)}")
        missing_urgency = set(range(6)) - set(self.urgency_multipliers)
        if missing_urgency:
            raise ValueError(f"urgency_multipliers must cover 0-5, missing: {sorted(missing_urgency)}")
        missing_labels = set(range(6)) - set(self.urgency_labels)
        if missing_labels:
            raise ValueError(f"urgency_labels must cover 0-5, missing: {sorted(missing_labels)}")
        tiers = list(self.price_range_by_completeness.values())
        scores = sorted(tier.min_score for tier in tiers)
        if scores and scores[0] < 0.60:
            raise ValueError("price_range_by_completeness lowest min_score must be >= 0.60")
        if self.price_band_max < self.price_band_min:
            raise ValueError("price_band_max must be >= price_band_min")
        return self

    def urgency_label(self, level: int) -> str:
        return self.urgency_labels.get(level, "未知")


class AiQuotePrice(BaseModel):
    """AI 报价方法的最终计算结果（全部由本模块确定性产出）。"""

    rule_set_version: str
    quote_type: str
    currency: str
    estimated_hours: float
    part_type: str = ""
    project_category: str = ""
    project_subtype: str = ""
    base_price: float
    main_price: float
    addon_price: float
    final_price: float
    complexity_tier: str
    complexity_multiplier: float
    urgency_level: int
    urgency_multiplier: float
    urgency_label: str
    price: dict[str, Any]
    price_breakdown: dict[str, Any]
    manual_review_required: bool
    review_reasons: list[str]
    calculation_snapshot: dict[str, Any]


def delivery_days_to_urgency(days: float | None, is_urgent: bool = False) -> int:
    """按交付天数自动映射 0-5 加急等级（与原型 main.py 完全一致）。"""
    if days is not None:
        if days > 30:
            return 0
        if days > 20:
            return 1
        if days > 10:
            return 2
        if days > 5:
            return 3
        if days > 2:
            return 4
        return 5
    return 5 if is_urgent else 3


class AiQuotePricing:
    """确定性算价引擎：唯一计算价格的地方。

    公式（原型 v2）：
        base_price   = estimated_hours × hourly_rate
        main_price   = base_price × complexity_multiplier × urgency_multiplier
        addon_price  = Σ(交付物数量 × 固定单价)  [可选开关]
        final_price  = round(main_price + addon_price)
    """

    def __init__(self, rules: AiQuoteRules):
        self.rules = rules

    def calculate(
        self,
        evaluation: AiQuoteEvaluation,
        completeness_score: float | None = None,
    ) -> AiQuotePrice:
        rules = self.rules
        review_reasons: list[str] = []

        hours = evaluation.estimated_hours
        tier = evaluation.complexity_tier
        urgency = evaluation.urgency_level
        if tier not in COMPLEXITY_TIERS:
            raise ValueError(f"invalid complexity_tier: {tier}")
        if urgency not in rules.urgency_multipliers:
            raise ValueError(f"invalid urgency_level: {urgency}")

        complexity_mult = rules.complexity_multipliers[tier]
        urgency_mult = rules.urgency_multipliers[urgency]

        base_price = hours * rules.hourly_rate
        main_price = base_price * complexity_mult * urgency_mult

        addon_price = 0.0
        addon_detail: dict[str, str] = {}
        if rules.enable_deliverable_addon:
            addon_price, addon_detail = self._deliverable_addon(evaluation.deliverable_detail)

        unit = rules.quote_rounding_unit_cny
        final_price = self._round_to_unit(main_price + addon_price, unit)

        # ---- 价格输出 ----
        # AI 报价输出推荐价与区间：不传完整度时按 price_band_min/max 生成区间
        # （兼容前端展示，且容纳现有数据量下约 ±15% 的报价误差）。
        quote_type = rules.quote_type_default
        price: dict[str, float | None] = {
            "currency": rules.currency,
            "minimum": self._round_to_unit(final_price * rules.price_band_min, unit),
            "recommended": final_price,
            "maximum": self._round_to_unit(final_price * rules.price_band_max, unit),
        }
        if completeness_score is not None:
            tier_rule = self._price_range_tier(completeness_score)
            if tier_rule is None:
                quote_type = "preliminary_research"
                price["recommended"] = None
                price["minimum"] = None
                price["maximum"] = None
                review_reasons.append("completeness_below_0_60")
            else:
                price["minimum"] = self._round_to_unit(final_price * tier_rule.min_factor, unit)
                price["maximum"] = self._round_to_unit(final_price * tier_rule.max_factor, unit)
            if completeness_score < rules.manual_review.completeness_below:
                review_reasons.append("completeness_below_0_80")

        if final_price >= rules.manual_review.quote_amount_above_cny:
            review_reasons.append("quote_amount_above_threshold")

        breakdown = {
            "工时费": f"{hours:g}h × {rules.hourly_rate:g}元/h = {base_price:g}元",
            "复杂度系数": f"×{complexity_mult:g}（{tier}）",
            "加急系数": f"×{urgency_mult:g}（{rules.urgency_label(urgency)}）",
        }
        if addon_detail:
            breakdown["交付物加项"] = addon_detail

        review_reasons = list(dict.fromkeys(review_reasons))
        snapshot = {
            "engine": "ai_quote",
            "engine_version": "1.0.0",
            "rules": {
                "rule_set_version": rules.rule_set_version,
                "sha256": rules.source_sha256,
            },
            "inputs": evaluation.model_dump(mode="json"),
            "completeness_score": completeness_score,
        }

        return AiQuotePrice(
            rule_set_version=rules.rule_set_version,
            quote_type=quote_type,
            currency=rules.currency,
            estimated_hours=hours,
            part_type=evaluation.part_type,
            project_category=evaluation.project_category,
            project_subtype=evaluation.project_subtype,
            base_price=self._round2(base_price),
            main_price=self._round2(main_price),
            addon_price=self._round2(addon_price),
            final_price=final_price,
            complexity_tier=tier,
            complexity_multiplier=complexity_mult,
            urgency_level=urgency,
            urgency_multiplier=urgency_mult,
            urgency_label=rules.urgency_label(urgency),
            price=price,
            price_breakdown=breakdown,
            manual_review_required=bool(review_reasons),
            review_reasons=review_reasons,
            calculation_snapshot=snapshot,
        )

    # ---- 内部辅助 ----

    def _deliverable_addon(
        self,
        detail: AiQuoteDeliverableDetail,
    ) -> tuple[float, dict[str, str]]:
        prices = self.rules.deliverable_prices
        items = [
            ("装配体", detail.assembly_count, prices["assembly"]),
            ("零件图", detail.part_drawing_count, prices["part_drawing"]),
            ("加工图", detail.machining_drawing_count, prices["machining_drawing"]),
            ("工序卡", detail.process_card_count, prices["process_card"]),
            ("规程卡", detail.procedure_card_count, prices["procedure_card"]),
            ("毛坯图", detail.blank_drawing_count, prices["blank_drawing"]),
            ("说明书", detail.instruction_book_count, prices["instruction_book"]),
        ]
        total = 0.0
        detail_lines: dict[str, str] = {}
        for label, count, unit_price in items:
            if count <= 0:
                continue
            subtotal = count * unit_price
            total += subtotal
            detail_lines[label] = f"{count} × {unit_price:g}元 = {subtotal:g}元"
        return total, detail_lines

    def _price_range_tier(self, completeness: float) -> PriceRangeTier | None:
        tiers = sorted(
            self.rules.price_range_by_completeness.values(),
            key=lambda t: t.min_score,
            reverse=True,
        )
        for tier in tiers:
            if completeness >= tier.min_score:
                return tier
        return None

    @staticmethod
    def _round_to_unit(value: float, unit: float) -> float:
        if unit <= 0:
            return round(value)
        return int(value / unit + 0.5) * unit

    @staticmethod
    def _round2(value: float) -> float:
        return round(value + 1e-9, 2)
