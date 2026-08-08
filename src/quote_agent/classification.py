"""项目分类 Agent：从预定义枚举中选类型并给出置信度（QRS 6.1）。"""

from __future__ import annotations

from .config import QuoteRules
from .file_parser import ParsedFile
from .llm import LLM
from .models import ProjectClassification, ProjectRequirement


class ClassificationAgent:
    SYSTEM_PROMPT = """你是机械设计报价系统的项目分类助手。
只能从给定的业务大类（case_category）和 project_type 枚举中各自选择一个，并输出 0-1 的置信度。
同时给出最多 3 个候选 project_type。禁止输出价格或工时。"""

    def __init__(self, llm: LLM, rules: QuoteRules):
        self.llm = llm
        self.rules = rules

    def classify(
        self,
        requirement: ProjectRequirement,
        parsed: list[ParsedFile],
    ) -> ProjectClassification:
        enum = ", ".join(self.rules.project_types)
        categories = "; ".join(
            f"{key}={cat.name}"
            for key, cat in self.rules.case_categories.items()
        )
        mapping = "; ".join(
            f"{pt}->{self.rules.category_of(pt)}"
            for pt in self.rules.project_types
        )
        user = (
            f"结构化需求：\n{requirement.model_dump_json(indent=2)}\n\n"
            f"文件摘要：\n{_summarize(parsed)}\n\n"
            f"业务大类枚举：{categories}\n"
            f"可用 project_type 枚举（箭头后为所属业务大类）：{mapping}\n"
            "先判断项目最匹配的业务大类 category，再在该大类下选择最具体的 project_type。\n"
            "输出 JSON：{'category': '...', 'project_type': '...', 'confidence': 0.0-1.0, "
            "'candidates': [{'project_type': '...', 'confidence': 0.0-1.0}]}"
        )
        result = self.llm.generate_structured(self.SYSTEM_PROMPT, user, ProjectClassification)
        assert isinstance(result, ProjectClassification)
        # 兼容旧模型输出：未给 category 或给了无效 key 时，按 project_type 归属推导
        if result.category not in self.rules.case_categories:
            result.category = self.rules.category_of(result.project_type) or None
        return result


def _summarize(parsed: list[ParsedFile]) -> str:
    parts = []
    for pf in parsed:
        parts.append(f"- {pf.original_name}: {pf.text[:500] or pf.table_summary or '(无文本)'}")
    return "\n".join(parts) or "(无文件)"
