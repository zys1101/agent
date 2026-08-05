"""确定性完整度计算测试（QRS 9.1/9.2）。"""

import pytest

from quote_agent.completeness import apply_deterministic_overrides, calculate_completeness
from quote_agent.config import QuoteRules
from quote_agent.models import ProjectRequirement


@pytest.fixture(scope="module")
def rules() -> QuoteRules:
    return QuoteRules.load()


def test_fixture_partial_completeness(rules: QuoteRules):
    requirement = ProjectRequirement(
        project_type_candidate="pneumatic_press_fixture",
        requested_deliverables=["three_d_assembly"],
        deadline_workdays=10,
        function_description="电机壳体压装工装",
        provided_materials=["requirements.pdf"],
        revision_policy="limited",
        missing_critical_interface=True,
        missing_load_or_force=True,
        workpiece_info_known=True,
        cycle_time_known=True,
        precision="0.05",
        completeness_score=0.99,  # 应被重算覆盖
    )
    result = calculate_completeness(requirement, rules)
    assert result.score == pytest.approx(11.0 / 17.5, abs=1e-4)
    assert "interface" in result.missing_key_fields
    assert "load" in result.missing_key_fields
    assert result.field_states["precision"] == "confirmed"
    assert result.field_states["acceptance"] == "unknown"


def test_apply_overrides_recomputes_and_derives(rules: QuoteRules):
    requirement = ProjectRequirement(
        project_type_candidate="pneumatic_press_fixture",
        requested_deliverables=["bom"],
        deadline_workdays=10,
        revision_policy="unlimited",
        missing_critical_interface=True,
        missing_load_or_force=True,
        completeness_score=0.99,
    )
    apply_deterministic_overrides(requirement, rules)
    assert requirement.completeness_score != 0.99
    assert requirement.unlimited_revisions is True
    assert requirement.missing_critical_interface is True
    assert requirement.missing_load_or_force is True
    assert requirement.missing_workpiece_info is True
    assert requirement.missing_acceptance_criteria is True
    assert requirement.field_states["scope"] == "confirmed"


def test_generic_family_ignores_fixture_fields(rules: QuoteRules):
    requirement = ProjectRequirement(
        project_type_candidate="simple_part",
        requested_deliverables=["two_d_part_drawing"],
        deadline_workdays=5,
        function_description="支架",
        revision_policy="limited",
        provided_materials=["spec.pdf"],
        acceptance_criteria_known=True,
        completeness_score=0.5,
    )
    result = calculate_completeness(requirement, rules)
    # generic 模板不含 workpiece/load/interface 等字段
    assert "interface" not in result.field_states
    assert result.score == 1.0


def test_high_precision_is_inferred(rules: QuoteRules):
    requirement = ProjectRequirement(
        project_type_candidate="assembly_fixture",
        requested_deliverables=["three_d_assembly"],
        deadline_workdays=10,
        precision="high_precision",
        completeness_score=0.5,
    )
    result = calculate_completeness(requirement, rules)
    assert result.field_states["precision"] == "inferred"
