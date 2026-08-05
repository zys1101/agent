"""规则配置的加载与交叉校验测试。"""

from quote_agent.config import QuoteRules


def test_rules_load_success():
    rules = QuoteRules.load()
    assert rules.rule_set_version
    assert len(rules.source_sha256) == 64


def test_all_project_types_from_qrs_present():
    rules = QuoteRules.load()
    expected = {
        "simple_part",
        "machined_part",
        "sheet_metal_part",
        "drawing_conversion",
        "inspection_fixture",
        "assembly_fixture",
        "welding_fixture",
        "pneumatic_press_fixture",
        "loading_module",
        "single_station_machine",
        "design_review",
    }
    assert set(rules.project_types) == expected


def test_rate_cards_have_all_roles():
    rules = QuoteRules.load()
    expected_roles = {
        "drafter",
        "junior_mechanical_engineer",
        "mechanical_engineer",
        "senior_mechanical_engineer",
        "technical_lead",
        "project_manager",
        "simulation_engineer",
        "field_engineer",
    }
    assert set(rules.rate_cards) == expected_roles


def test_precision_keys_are_strings():
    rules = QuoteRules.load()
    assert "0.10" in rules.precision
    assert "0.05" in rules.precision
    assert "0.01" in rules.precision


def test_deterministic_sha():
    a = QuoteRules.load()
    b = QuoteRules.load()
    assert a.source_sha256 == b.source_sha256
