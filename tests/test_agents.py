"""需求提取与分类 Agent 测试（FakeLLM，不连真实模型）。"""

import pytest

from quote_agent.classification import ClassificationAgent
from quote_agent.config import QuoteRules
from quote_agent.extraction import ExtractionAgent
from quote_agent.file_parser import ParsedFile
from quote_agent.models import ProjectClassification, ProjectRequirement


class FakeLLM:
    def __init__(self, extraction: dict, classification: dict, fail_extraction_first: bool = False):
        self.extraction = extraction
        self.classification = classification
        self.fail_extraction_first = fail_extraction_first
        self.calls = 0

    def generate_structured(self, system, user, model_cls, images=None):
        self.calls += 1
        if self.fail_extraction_first and self.calls == 1:
            raise ValueError("invalid json")  # 由修复逻辑处理
        if model_cls is ProjectRequirement:
            return ProjectRequirement.model_validate(self.extraction)
        if model_cls is ProjectClassification:
            return ProjectClassification.model_validate(self.classification)
        raise AssertionError(f"unexpected model_cls: {model_cls}")


@pytest.fixture(scope="module")
def rules() -> QuoteRules:
    return QuoteRules.load()


def test_extraction_agent_builds_prompt_and_returns_model(rules: QuoteRules):
    fake = FakeLLM(
        extraction={
            "project_type_candidate": "pneumatic_press_fixture",
            "requested_deliverables": ["three_d_assembly", {"code": "two_d_part_drawing", "quantity": 8}],
            "deadline_workdays": 10.0,
            "completeness_score": 0.7,
            "precision": "0.05",
        },
        classification={"project_type": "pneumatic_press_fixture", "confidence": 0.8},
    )
    agent = ExtractionAgent(fake, rules)
    parsed = [ParsedFile(file_id="f1", original_name="req.txt", mime_type="text/plain", text="压装力待确认")]
    result = agent.extract({"project_name": "压装工装"}, parsed)
    assert isinstance(result, ProjectRequirement)
    assert result.project_type_candidate == "pneumatic_press_fixture"
    assert len(result.requested_deliverables) == 2


def test_classification_agent_returns_model(rules: QuoteRules):
    fake = FakeLLM(
        extraction={"project_type_candidate": "x", "completeness_score": 0.5},
        classification={
            "project_type": "welding_fixture",
            "confidence": 0.9,
            "candidates": [
                {"project_type": "welding_fixture", "confidence": 0.9},
                {"project_type": "assembly_fixture", "confidence": 0.08},
            ],
        },
    )
    agent = ClassificationAgent(fake, rules)
    result = agent.classify(
        ProjectRequirement(project_type_candidate="x", completeness_score=0.5),
        [],
    )
    assert result.project_type == "welding_fixture"
    assert len(result.candidates) == 2


def test_extraction_prompt_contains_enums(rules: QuoteRules, monkeypatch):
    captured = {}

    class CallableLLM:
        def generate_structured(self, system, user, model_cls, images=None):
            captured["user"] = user
            return ProjectRequirement(project_type_candidate="simple_part", completeness_score=0.9)

    agent = ExtractionAgent(CallableLLM(), rules)
    agent.extract({}, [])
    assert "pneumatic_press_fixture" in captured["user"]
    assert "two_d_part_drawing" in captured["user"]
    assert "high_precision" in captured["user"]
