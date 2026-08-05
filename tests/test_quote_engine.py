"""报价引擎测试：边界值与确定性（对应 08-test-and-evaluation-plan.md 第 4 节）。"""

import pytest

from quote_agent.config import QuoteRules
from quote_agent.models import (
    DeliverableRequest,
    ProjectClassification,
    ProjectRequirement,
)
from quote_agent.quote_engine import QuoteEngine


def make_requirement(**overrides) -> ProjectRequirement:
    defaults = {
        "project_type_candidate": "simple_part",
        "requested_deliverables": [],
        "deadline_workdays": 10.0,
        "completeness_score": 0.95,
    }
    defaults.update(overrides)
    return ProjectRequirement(**defaults)


def make_classification(project_type: str = "simple_part", confidence: float = 0.95):
    return ProjectClassification(project_type=project_type, confidence=confidence)


@pytest.fixture(scope="module")
def engine() -> QuoteEngine:
    return QuoteEngine(QuoteRules.load())


def test_simple_part_budget_range_golden(engine: QuoteEngine):
    requirement = make_requirement(
        requested_deliverables=[DeliverableRequest(code="two_d_part_drawing", quantity=2)]
    )
    result = engine.calculate(requirement, make_classification())

    assert result.quote_type == "budget_range"
    assert not result.manual_review_required
    assert result.estimated_hours.total == 13.8
    assert result.hours_by_role["junior_mechanical_engineer"] == 9.2
    assert result.hours_by_role["drafter"] == 4.6
    assert result.price.recommended == 2900
    assert result.price.minimum == 2700
    assert result.price.maximum == 3000


def test_completeness_below_0_80_requires_review(engine: QuoteEngine):
    result = engine.calculate(make_requirement(completeness_score=0.73), make_classification())
    assert result.manual_review_required
    assert "completeness_below_0_80" in result.review_reasons


def test_completeness_0_80_boundary_no_review(engine: QuoteEngine):
    result = engine.calculate(make_requirement(completeness_score=0.80), make_classification())
    assert "completeness_below_0_80" not in result.review_reasons
    assert not result.manual_review_required


def test_completeness_below_0_60_preliminary_research(engine: QuoteEngine):
    result = engine.calculate(make_requirement(completeness_score=0.50), make_classification())
    assert result.quote_type == "preliminary_research"
    assert result.price.recommended is None
    assert result.manual_review_required
    assert "completeness_below_0_60" in result.review_reasons


def test_amount_above_50000_requires_review(engine: QuoteEngine):
    result = engine.calculate(
        make_requirement(
            project_type_candidate="single_station_machine",
            requested_deliverables=["bom"],
            deadline_workdays=60.0,
        ),
        make_classification("single_station_machine"),
    )
    assert "quote_amount_above_threshold" in result.review_reasons
    assert result.price.recommended >= 50000


def test_simple_part_amount_below_threshold(engine: QuoteEngine):
    result = engine.calculate(make_requirement(), make_classification())
    assert "quote_amount_above_threshold" not in result.review_reasons


def test_rush_urgent_requires_review(engine: QuoteEngine):
    result = engine.calculate(
        make_requirement(deadline_workdays=1.0),  # 周期约 2.2 天 -> 比例约 0.45（0.40-0.69 加急档）
        make_classification(),
    )
    assert result.applied_factors.rush_factor == 1.25
    assert "rush_ratio_below_0_70" in result.review_reasons


def test_rush_slightly_urgent_not_reviewed(engine: QuoteEngine):
    result = engine.calculate(
        make_requirement(deadline_workdays=1.9),  # 周期约 2.2 天 -> 比例约 0.86（0.70-0.89 略紧档）
        make_classification(),
    )
    assert result.applied_factors.rush_factor == 1.10
    assert "rush_ratio_below_0_70" not in result.review_reasons
    assert not result.manual_review_required


def test_precision_0_05_requires_review(engine: QuoteEngine):
    result = engine.calculate(make_requirement(precision="0.05"), make_classification())
    assert "precision_0_05_or_stricter" in result.review_reasons


def test_precision_0_10_not_forced(engine: QuoteEngine):
    result = engine.calculate(make_requirement(precision="0.10"), make_classification())
    assert "precision_0_05_or_stricter" not in result.review_reasons
    assert not result.manual_review_required


def test_parts_over_80_required_review(engine: QuoteEngine):
    result = engine.calculate(make_requirement(part_count=81), make_classification())
    assert "parts_over_80" in result.review_reasons
    assert result.applied_factors.complexity_factor == 1.60


def test_parts_80_suggested_review(engine: QuoteEngine):
    result = engine.calculate(make_requirement(part_count=80), make_classification())
    assert "parts_31_80" in result.suggested_review_reasons
    assert "parts_31_80" not in result.review_reasons
    assert not result.manual_review_required
    assert result.applied_factors.complexity_factor == 1.35


def test_complexity_factor_capped_at_2(engine: QuoteEngine):
    result = engine.calculate(
        make_requirement(part_count=81, precision="0.01"),
        make_classification(),
    )
    assert result.applied_factors.complexity_factor == 2.0
    assert "complexity_factor_above_cap" in result.review_reasons


def test_missing_load_and_interface_review(engine: QuoteEngine):
    requirement = make_requirement(
        project_type_candidate="pneumatic_press_fixture",
        completeness_score=0.73,
        missing_load_or_force=True,
        missing_critical_interface=True,
    )
    result = engine.calculate(requirement, make_classification("pneumatic_press_fixture", 0.82))
    assert "risk_missing_load_or_force" in result.review_reasons
    assert "risk_missing_critical_interface" in result.review_reasons


def test_determinism(engine: QuoteEngine):
    requirement = make_requirement(
        project_type_candidate="inspection_fixture",
        requested_deliverables=["three_d_model", "bom"],
        deadline_workdays=5.0,
        completeness_score=0.85,
        part_count=15,
        precision="0.10",
    )
    a = engine.calculate(requirement, make_classification("inspection_fixture"))
    b = engine.calculate(requirement, make_classification("inspection_fixture"))
    assert a.model_dump(mode="json") == b.model_dump(mode="json")


def test_prices_rounded_to_100(engine: QuoteEngine):
    result = engine.calculate(
        make_requirement(
            project_type_candidate="assembly_fixture",
            requested_deliverables=["two_d_assembly_drawing", "bom"],
            completeness_score=0.85,
            part_count=40,
        ),
        make_classification("assembly_fixture"),
    )
    for value in (result.price.minimum, result.price.recommended, result.price.maximum):
        assert value is None or value % 100 == 0


def test_unknown_project_type_raises(engine: QuoteEngine):
    with pytest.raises(ValueError, match="unknown project_type"):
        engine.calculate(make_requirement(project_type_candidate="nope"), make_classification("nope"))


def test_unknown_deliverable_raises(engine: QuoteEngine):
    with pytest.raises(ValueError, match="unknown deliverable"):
        engine.calculate(
            make_requirement(requested_deliverables=["not_a_deliverable"]),
            make_classification(),
        )
