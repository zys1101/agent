"""ReviewAgent 测试：只增审核，不改价格。"""

from quote_agent.config import QuoteRules
from quote_agent.models import (
    ProjectClassification,
    ProjectRequirement,
    ReviewLLMOutput,
)
from quote_agent.quote_engine import QuoteEngine
from quote_agent.review import ReviewAgent


class FakeReviewLLM:
    def __init__(self, notes, extra):
        self.notes = notes
        self.extra = extra

    def generate_structured(self, system, user, model_cls, images=None):
        assert model_cls is ReviewLLMOutput
        return ReviewLLMOutput(reviewer_notes=self.notes, extra_review_reasons=self.extra)


def test_review_agent_returns_output():
    rules = QuoteRules.load()
    agent = ReviewAgent(
        FakeReviewLLM(notes=["压装力缺失需先澄清"], extra=["risk_missing_load_or_force"]),
        rules,
    )
    requirement = ProjectRequirement(
        project_type_candidate="pneumatic_press_fixture",
        requested_deliverables=["bom"],
        deadline_workdays=10,
        completeness_score=0.7,
    )
    classification = ProjectClassification(project_type="pneumatic_press_fixture", confidence=0.9)
    calc = QuoteEngine(rules).calculate(requirement, classification)
    review = agent.review(requirement, classification, calc, [])
    assert review.reviewer_notes == ["压装力缺失需先澄清"]
    assert review.extra_review_reasons == ["risk_missing_load_or_force"]


def test_review_prompt_contains_calc_and_cases():
    captured = {}

    class CaptureLLM:
        def generate_structured(self, system, user, model_cls, images=None):
            captured["user"] = user
            return ReviewLLMOutput()

    rules = QuoteRules.load()
    requirement = ProjectRequirement(
        project_type_candidate="simple_part",
        requested_deliverables=[],
        deadline_workdays=5,
        completeness_score=0.9,
    )
    calc = QuoteEngine(rules).calculate(requirement, ProjectClassification(project_type="simple_part", confidence=0.9))
    ReviewAgent(CaptureLLM(), rules).review(requirement, ProjectClassification(project_type="simple_part", confidence=0.9), calc, [])
    assert "规则引擎计算结果" in captured["user"]
    assert "参考案例" in captured["user"]
