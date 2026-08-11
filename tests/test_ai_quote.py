"""AI 报价方法（AI_quote 原型）测试：算价公式、加急映射、评估器覆盖、确定性。"""

import pytest

from quote_agent.ai_evaluator import AiQuoteEvaluator
from quote_agent.ai_quote import (
    AiQuoteDeliverableDetail,
    AiQuoteEvaluation,
    AiQuotePricing,
    AiQuoteRules,
    delivery_days_to_urgency,
)
from quote_agent.llm import LLMOutputError


@pytest.fixture(scope="module")
def rules() -> AiQuoteRules:
    return AiQuoteRules.load()


@pytest.fixture(scope="module")
def pricing(rules: AiQuoteRules) -> AiQuotePricing:
    return AiQuotePricing(rules)


def make_evaluation(**overrides) -> AiQuoteEvaluation:
    defaults = {
        "part_type": "测试零件",
        "project_category": "机械机构设计",
        "project_subtype": "测试机构设计",
        "deliverables": ["3D模型"],
        "complexity_tier": "normal",
        "estimated_hours": 8,
        "urgency_level": 3,
        "difficulty_reason": [],
        "case_summary": "测试",
    }
    defaults.update(overrides)
    return AiQuoteEvaluation(**defaults)


class FakeLLM:
    def __init__(self, payload: dict):
        self.payload = payload
        self.last_images = None

    def generate_structured(self, system, user, model_cls, images=None):
        self.last_images = images
        return model_cls.model_validate(self.payload)


# ---------------------------------------------------------------- 算价公式


def test_golden_normal(pricing: AiQuotePricing):
    result = pricing.calculate(make_evaluation())
    assert result.base_price == 400
    assert result.main_price == 400
    assert result.addon_price == 0
    assert result.final_price == 400
    assert result.price["recommended"] == 400
    assert not result.manual_review_required


def test_golden_complex(pricing: AiQuotePricing):
    result = pricing.calculate(
        make_evaluation(complexity_tier="complex", estimated_hours=8)
    )
    assert result.complexity_multiplier == 1.2
    assert result.final_price == 480


def test_golden_simple(pricing: AiQuotePricing):
    result = pricing.calculate(
        make_evaluation(complexity_tier="simple", estimated_hours=8)
    )
    assert result.complexity_multiplier == 0.8
    assert result.final_price == 320


def test_golden_urgent_levels(pricing: AiQuotePricing):
    assert pricing.calculate(make_evaluation(urgency_level=5)).final_price == 600
    assert pricing.calculate(make_evaluation(urgency_level=0)).final_price == 340
    assert pricing.calculate(make_evaluation(urgency_level=4)).final_price == 520


def test_golden_combined_factors(pricing: AiQuotePricing):
    # 8h × 50 × 1.2(complex) × 1.5(very urgent) = 720
    result = pricing.calculate(
        make_evaluation(complexity_tier="complex", urgency_level=5)
    )
    assert result.final_price == 720
    # 2.5h × 50 × 1.2 × 1.3 = 195
    result2 = pricing.calculate(
        make_evaluation(
            complexity_tier="complex",
            urgency_level=4,
            estimated_hours=2.5,
        )
    )
    assert result2.final_price == 195


def test_deliverable_addon():
    rules = AiQuoteRules.load()
    rules.enable_deliverable_addon = True
    result = AiQuotePricing(rules).calculate(
        make_evaluation(
            deliverable_detail=AiQuoteDeliverableDetail(
                assembly_count=1,
                part_drawing_count=8,
                machining_drawing_count=2,
                process_card_count=1,
                procedure_card_count=1,
                blank_drawing_count=1,
                instruction_book_count=1,
            )
        )
    )
    # 150 + 8×20 + 2×30 + 20×3 + 100 = 530；工时费 400 -> 930
    assert result.addon_price == 530
    assert result.final_price == 930
    assert len(result.price_breakdown["交付物加项"]) == 7


def test_price_range_by_completeness(pricing: AiQuotePricing):
    result = pricing.calculate(make_evaluation(), completeness_score=0.95)
    assert result.price == {
        "currency": "CNY",
        "minimum": 380,
        "recommended": 400,
        "maximum": 420,
    }

    result2 = pricing.calculate(make_evaluation(), completeness_score=0.65)
    assert result2.price == {
        "currency": "CNY",
        "minimum": 340,
        "recommended": 400,
        "maximum": 480,
    }


def test_completeness_below_0_60_preliminary_research(pricing: AiQuotePricing):
    result = pricing.calculate(make_evaluation(), completeness_score=0.5)
    assert result.quote_type == "preliminary_research"
    assert result.price["recommended"] is None
    assert "completeness_below_0_60" in result.review_reasons
    assert "completeness_below_0_80" in result.review_reasons
    assert result.manual_review_required


def test_completeness_below_0_80_requires_review(pricing: AiQuotePricing):
    result = pricing.calculate(make_evaluation(), completeness_score=0.75)
    assert "completeness_below_0_80" in result.review_reasons
    assert result.manual_review_required


def test_amount_above_threshold_requires_review(pricing: AiQuotePricing):
    result = pricing.calculate(
        make_evaluation(complexity_tier="complex", urgency_level=5, estimated_hours=1000)
    )
    assert result.final_price == 90000
    assert "quote_amount_above_threshold" in result.review_reasons
    assert result.manual_review_required


def test_invalid_complexity_raises(pricing: AiQuotePricing):
    with pytest.raises(ValueError, match="invalid complexity_tier"):
        pricing.calculate(make_evaluation(complexity_tier="hard"))


def test_determinism(pricing: AiQuotePricing):
    evaluation = make_evaluation(
        complexity_tier="complex",
        urgency_level=4,
        estimated_hours=12.5,
    )
    a = pricing.calculate(evaluation, completeness_score=0.85)
    b = pricing.calculate(evaluation, completeness_score=0.85)
    assert a.model_dump(mode="json") == b.model_dump(mode="json")


# ---------------------------------------------------------------- 加急映射


@pytest.mark.parametrize(
    ("days", "urgent", "expected"),
    [
        (40, False, 0),
        (30, False, 1),
        (21, False, 1),
        (20, False, 2),
        (11, False, 2),
        (10, False, 3),
        (6, False, 3),
        (5, False, 4),
        (3, False, 4),
        (2, False, 5),
        (1, False, 5),
        (0, False, 5),
        (None, False, 3),
        (None, True, 5),
    ],
)
def test_delivery_days_to_urgency(days, urgent, expected):
    assert delivery_days_to_urgency(days, urgent) == expected


# ---------------------------------------------------------------- 评估器


def test_evaluator_overrides_urgency(rules: AiQuoteRules):
    llm = FakeLLM(make_evaluation(urgency_level=0).model_dump(mode="json"))
    evaluator = AiQuoteEvaluator(llm, rules)
    result = evaluator.evaluate(
        requirement_text="设计一个支架",
        delivery_days=7,  # 系统判定为 3（正常）
    )
    assert result.urgency_level == 3


def test_evaluator_coerces_invalid_complexity(rules: AiQuoteRules):
    payload = make_evaluation(complexity_tier="hard").model_dump(mode="json")
    llm = FakeLLM(payload)
    result = AiQuoteEvaluator(llm, rules).evaluate(requirement_text="设计一个支架")
    assert result.complexity_tier == "normal"


def test_evaluator_rejects_zero_hours(rules: AiQuoteRules):
    payload = make_evaluation(estimated_hours=0).model_dump(mode="json")
    llm = FakeLLM(payload)
    with pytest.raises(LLMOutputError, match="estimated_hours"):
        AiQuoteEvaluator(llm, rules).evaluate(requirement_text="设计一个支架")


def test_evaluator_passes_images(rules: AiQuoteRules):
    llm = FakeLLM(make_evaluation().model_dump(mode="json"))
    AiQuoteEvaluator(llm, rules).evaluate(
        requirement_text="设计一个支架",
        images=["a.png", "b.jpg"],
    )
    assert llm.last_images == ["a.png", "b.jpg"]


def test_evaluation_prompt_contains_reference_and_urgency(rules: AiQuoteRules):
    evaluator = AiQuoteEvaluator(FakeLLM({}), rules)
    prompt = evaluator.build_evaluation_prompt(
        requirement_text="设计一个支架",
        image_summary="支架结构",
        similar_cases=[{"case_id": "CASE_1", "final_price": 600}],
        delivery_days=7,
    )
    assert "参考价格表" in prompt
    assert "加急等级(系统已判定): 3 - 正常" in prompt
    assert "支架结构" in prompt
    assert "CASE_1" in prompt
    assert "7天" in prompt


def test_evaluation_prompt_unknown_delivery_days(rules: AiQuoteRules):
    evaluator = AiQuoteEvaluator(FakeLLM({}), rules)
    prompt = evaluator.build_evaluation_prompt(requirement_text="设计一个支架")
    assert "未指定" in prompt


# ---------------------------------------------------------------- 配置


def test_rules_load_and_source_sha256(rules: AiQuoteRules):
    assert rules.rule_set_version == "1.0.0-ai-quote"
    assert rules.hourly_rate == 50
    assert rules.urgency_multipliers[5] == 1.5
    assert rules.deliverable_prices["assembly"] == 150
    assert rules.deliverable_prices["machining_drawing"] == 30
    assert rules.source_sha256


def test_rules_reject_incomplete_urgency_map():
    data = AiQuoteRules.load().model_dump(mode="json")
    data["urgency_multipliers"] = {0: 0.85, 1: 0.9}
    with pytest.raises(ValueError, match="urgency_multipliers must cover 0-5"):
        AiQuoteRules.model_validate(data)
