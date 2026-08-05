"""审核助手（AWF REVIEW_QUOTE 步骤）。

LLM 只能增加审核项与审核意见，不得修改价格、工时或系数；
manual_review_required 由规则引擎判定，LLM 补充项只会让审核更严格。
"""

from __future__ import annotations

from .config import QuoteRules
from .llm import LLM
from .models import (
    ProjectClassification,
    ProjectRequirement,
    QuoteCalculation,
    ReviewLLMOutput,
)
from .rag import CaseRecord


class ReviewAgent:
    SYSTEM_PROMPT = """你是机械设计报价系统的内部审核助手（只服务内部人员，不面向客户）。
硬性规则：
1. 只检查是否遗漏风险、缺失资料、范围遗漏或审核项；不得降低引擎已有的审核等级。
2. 不得输出或修改任何价格、工时、费率、系数——价格控制权在规则引擎。
3. 只有在确实发现引擎未覆盖的问题时才添加 extra_review_reasons，宁缺毋滥，不得编造。
4. 审核意见 reviewer_notes 应具体、可执行，供工程/销售复核使用。"""

    def __init__(self, llm: LLM, rules: QuoteRules):
        self.llm = llm
        self.rules = rules

    def review(
        self,
        requirement: ProjectRequirement,
        classification: ProjectClassification,
        calc: QuoteCalculation,
        rag_cases: list[tuple[CaseRecord, float]] | None = None,
    ) -> ReviewLLMOutput:
        user = (
            "# 结构化需求\n"
            f"{requirement.model_dump_json(indent=2)}\n\n"
            "# 规则引擎计算结果\n"
            f"{calc.model_dump_json(indent=2)}\n\n"
            f"# 参考案例（脱敏，相似度排序）\n"
            + _format_cases(rag_cases or [])
            + "\n输出 JSON：{'reviewer_notes': [...], 'extra_review_reasons': [...]}"
        )
        result = self.llm.generate_structured(self.SYSTEM_PROMPT, user, ReviewLLMOutput)
        assert isinstance(result, ReviewLLMOutput)
        return result


def _format_cases(cases: list[tuple[CaseRecord, float]]) -> str:
    if not cases:
        return "(无参考案例)"
    lines = []
    for case, score in cases:
        lines.append(
            f"- {case.project_type}（相似度 {score:.2f}）: {case.summary}；"
            f"风险点：{'；'.join(case.risk_notes) or '无'}；"
            f"交付物：{'、'.join(case.deliverables) or '无'}"
        )
    return "\n".join(lines)
