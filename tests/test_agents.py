"""需求提取与分类 Agent 测试（FakeLLM，不连真实模型）。"""

import pytest

from quote_agent.classification import ClassificationAgent
from quote_agent.config import QuoteRules
from quote_agent.extraction import ExtractionAgent, ImageTranscript, ImageTranscripts, MAX_PROMPT_CHARS
from quote_agent.file_parser import ParsedFile
from quote_agent.models import ProjectClassification, ProjectRequirement


class FakeLLM:
    def __init__(self, extraction: dict, classification: dict, fail_extraction_first: bool = False):
        self.extraction = extraction
        self.classification = classification
        self.fail_extraction_first = fail_extraction_first
        self.calls = 0
        self.last_images = None

    def generate_structured(self, system, user, model_cls, images=None):
        self.calls += 1
        if model_cls is ProjectRequirement:
            self.last_images = images
        if self.fail_extraction_first and self.calls == 1:
            raise ValueError("invalid json")  # 由修复逻辑处理
        if model_cls is ProjectRequirement:
            return ProjectRequirement.model_validate(self.extraction)
        if model_cls is ProjectClassification:
            return ProjectClassification.model_validate(self.classification)
        if model_cls is ImageTranscripts:
            return ImageTranscripts(
                files=[ImageTranscript(original_name="a.png", summary="工件信息", key_facts=["200x150mm"])]
            )
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
    assert result.provided_materials == ["req.txt"]  # 由已解析文件确定性回填


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
    # 模型未返回 category 时，按 project_type 归属推导业务大类
    assert result.category == "tooling_fixture"
    assert len(result.candidates) == 2


def test_classification_prompt_contains_category_taxonomy(rules: QuoteRules, monkeypatch):
    captured = {}

    class CallableLLM:
        def generate_structured(self, system, user, model_cls, images=None):
            captured["user"] = user
            return ProjectClassification(project_type="assembly_fixture", confidence=0.9)

    agent = ClassificationAgent(CallableLLM(), rules)
    agent.classify(ProjectRequirement(project_type_candidate="x", completeness_score=0.5), [])
    assert "整机设备设计" in captured["user"]
    assert "工装夹具设计" in captured["user"]
    assert "仿真分析" in captured["user"]
    assert "assembly_fixture->tooling_fixture" in captured["user"]


def test_classification_missing_confidence_defaults_conservative(rules: QuoteRules):
    fake = FakeLLM(
        extraction={"project_type_candidate": "x", "completeness_score": 0.5},
        classification={"project_type": "welding_fixture"},  # 模型未给 confidence
    )
    agent = ClassificationAgent(fake, rules)
    result = agent.classify(ProjectRequirement(project_type_candidate="x", completeness_score=0.5), [])
    assert result.confidence == 0.5
    assert result.category == "tooling_fixture"


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


def test_extraction_prompt_caps_total_file_text(rules: QuoteRules):
    captured = {}

    class CallableLLM:
        def generate_structured(self, system, user, model_cls, images=None):
            captured["user"] = user
            return ProjectRequirement(project_type_candidate="simple_part", completeness_score=0.9)

    agent = ExtractionAgent(CallableLLM(), rules)
    big = ParsedFile(file_id="f1", original_name="big.txt", mime_type="text/plain", text="机" * 100_000)
    agent.extract({}, [big])
    assert len(captured["user"]) < MAX_PROMPT_CHARS + 5_000
    assert "已截断" in captured["user"]


def test_requirement_evidence_strings_coerced():
    req = ProjectRequirement.model_validate(
        {
            "project_type_candidate": "simple_part",
            "completeness_score": 0.9,
            "evidence": ["req.txt", {"field": "precision", "source_file": "a.pdf", "value": "0.05"}],
        }
    )
    assert req.evidence[0].source_file == "req.txt"
    assert req.evidence[0].field == ""
    assert req.evidence[1].field == "precision"


def test_extraction_passes_image_paths_to_llm(rules: QuoteRules):
    fake = FakeLLM(
        extraction={"project_type_candidate": "simple_part", "completeness_score": 0.9},
        classification={"project_type": "simple_part", "confidence": 0.9},
    )
    agent = ExtractionAgent(fake, rules)
    parsed = [
        ParsedFile(file_id="f1", original_name="a.png", mime_type="image/png", ocr_pending=True, image_path="/tmp/a.png"),
        ParsedFile(file_id="f2", original_name="b.txt", mime_type="text/plain", text="hello"),
        ParsedFile(file_id="f3", original_name="c.png", mime_type="image/png", ocr_pending=True, image_path="/tmp/c.png", errors=["x"]),
    ]
    agent.extract({}, parsed)
    assert fake.last_images == ["/tmp/a.png"]  # 有错误标记的图片不传
