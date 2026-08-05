"""项目分类 Agent：从预定义枚举中选类型并给出置信度（QRS 6.1）。"""

from __future__ import annotations

from .config import QuoteRules
from .file_parser import ParsedFile
from .llm import LLM
from .models import ProjectClassification, ProjectRequirement


class ClassificationAgent:
    SYSTEM_PROMPT = """你是机械设计报价系统的项目分类助手。
只能从给定的 project_type 枚举中选择一个类型，并输出 0-1 的置信度。
同时给出最多 3 个候选类型。禁止输出价格或工时。"""

    def __init__(self, llm: LLM, rules: QuoteRules):
        self.llm = llm
        self.rules = rules

    def classify(
        self,
        requirement: ProjectRequirement,
        parsed: list[ParsedFile],
    ) -> ProjectClassification:
        enum = ", ".join(self.rules.project_types)
        user = (
            f"结构化需求：\n{requirement.model_dump_json(indent=2)}\n\n"
            f"文件摘要：\n{_summarize(parsed)}\n\n"
            f"可用 project_type 枚举：[{enum}]\n"
            "输出 JSON：{'project_type': '...', 'confidence': 0.0-1.0, 'candidates': [{'project_type': '...', 'confidence': 0.0-1.0}]}"
        )
        result = self.llm.generate_structured(self.SYSTEM_PROMPT, user, ProjectClassification)
        assert isinstance(result, ProjectClassification)
        return result


def _summarize(parsed: list[ParsedFile]) -> str:
    parts = []
    for pf in parsed:
        parts.append(f"- {pf.original_name}: {pf.text[:500] or pf.table_summary or '(无文本)'}")
    return "\n".join(parts) or "(无文件)"
