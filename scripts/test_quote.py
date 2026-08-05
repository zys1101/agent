"""报价引擎演示：用气动压装工装样例跑一遍并输出完整结果。"""

from __future__ import annotations

import json

from quote_agent.config import QuoteRules
from quote_agent.models import (
    ClassificationCandidate,
    DeliverableRequest,
    Evidence,
    ProjectClassification,
    ProjectRequirement,
)
from quote_agent.quote_engine import QuoteEngine


def main() -> int:
    rules = QuoteRules.load()
    engine = QuoteEngine(rules)

    requirement = ProjectRequirement(
        project_type_candidate="pneumatic_press_fixture",
        requested_deliverables=[
            "three_d_assembly",
            "bom",
            DeliverableRequest(code="two_d_part_drawing", quantity=8),
        ],
        deadline_workdays=10,
        unknowns=["maximum_press_force", "installation_interface"],
        risks=["missing_installation_interface"],
        completeness_score=0.73,
        evidence=[
            Evidence(field="deadline_workdays", source_file="form", value="10"),
            Evidence(field="deliverables", source_file="form", value="3D模型/2D工程图/BOM"),
        ],
        part_count=20,
        assembly_complexity="medium",
        precision="0.05",
        professional_scope=["pneumatic"],
        missing_load_or_force=True,
        missing_critical_interface=True,
    )
    classification = ProjectClassification(
        project_type="pneumatic_press_fixture",
        confidence=0.82,
        candidates=[
            ClassificationCandidate(project_type="pneumatic_press_fixture", confidence=0.82),
            ClassificationCandidate(project_type="assembly_fixture", confidence=0.12),
        ],
    )

    result = engine.calculate(requirement, classification)
    print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
