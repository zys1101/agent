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


def test_case_categories_match_site_taxonomy():
    rules = QuoteRules.load()
    assert set(rules.case_categories) == {
        "machine_design",
        "mechanism_design",
        "product_structure",
        "sheet_metal_cabinet",
        "welding_frame",
        "tooling_fixture",
        "test_fixture",
        "drawing_modeling",
        "reverse_engineering",
        "simulation_analysis",
        "process_production",
        "drawing_standardization",
        "technical_consulting",
    }
    # 与官网案例页一致的对外名称
    names = {cat.name for cat in rules.case_categories.values()}
    assert "整机设备设计" in names
    assert "工装夹具设计" in names
    assert "仿真分析" in names
    assert "技术咨询" in names


def test_all_project_types_mapped_to_category():
    rules = QuoteRules.load()
    for pt in rules.project_types:
        assert rules.category_of(pt) in rules.case_categories
    assert rules.category_of("pneumatic_press_fixture") == "tooling_fixture"
    assert rules.category_of("single_station_machine") == "machine_design"
    assert rules.category_of("sheet_metal_part") == "sheet_metal_cabinet"
    assert rules.category_of("design_review") == "technical_consulting"
    assert rules.category_name_of("tooling_fixture") == "工装夹具设计"


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
