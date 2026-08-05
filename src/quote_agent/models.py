"""领域数据模型：需求、分类、报价计算、审核结果。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


NULL_DEFAULTS = {
    "requested_deliverables": [],
    "unknowns": [],
    "risks": [],
    "evidence": [],
    "provided_materials": [],
    "professional_scope": [],
    "assumptions": [],
    "exclusions": [],
    "clarification_questions": [],
    "input_conflicts": [],
    "field_states": {},
    "function_description": "",
    "assembly_complexity": "simple",
    "precision": "unspecified",
    "completeness_score": 0.0,
    "missing_critical_interface": False,
    "missing_load_or_force": False,
    "missing_workpiece_info": False,
    "missing_acceptance_criteria": False,
    "unverified_solution": False,
    "multi_party_coordination": False,
    "new_customer": False,
    "unlimited_revisions": False,
    "high_responsibility_industry": False,
    "scope_uncertain": False,
    "acceptance_criteria_known": False,
    "workpiece_info_known": False,
    "cycle_time_known": False,
    "utilities_known": False,
    "safety_requirements_known": False,
    "fixed_fees_cny": 0.0,
}


class Evidence(BaseModel):
    field: str
    source_file: str
    value: str


class DeliverableRequest(BaseModel):
    code: str
    quantity: int = Field(default=1, ge=1)


class ProjectRequirement(BaseModel):
    """结构化需求输入（由后续 LLM 提取阶段产出，engine 只消费此结构）。"""

    # LLM 常把未知字段输出为 null：统一回填默认值，避免 Schema 校验被 null 击穿。
    @field_validator("*", mode="before")
    @classmethod
    def _coerce_null_to_default(cls, value, info):
        if value is None and info.field_name in NULL_DEFAULTS:
            return NULL_DEFAULTS[info.field_name]
        return value

    project_type_candidate: str
    requested_deliverables: list[str | DeliverableRequest] = []
    deadline_workdays: float | None = None
    function_description: str = ""
    provided_materials: list[str] = []
    revision_policy: str | None = None  # "limited" | "unlimited" | None
    unknowns: list[str] = []
    risks: list[str] = []
    # 模型可省略；Worker 在 VALIDATE_REQUIREMENTS 阶段必须用确定性算法重算（QRS 9.2）
    completeness_score: float = Field(default=0.0, ge=0, le=1)
    evidence: list[Evidence] = []

    # 复杂度输入
    part_count: int | None = Field(default=None, ge=0)
    assembly_complexity: str = "simple"
    precision: str = "unspecified"
    professional_scope: list[str] = []

    # 风险输入
    missing_critical_interface: bool = False
    missing_load_or_force: bool = False
    missing_workpiece_info: bool = False
    missing_acceptance_criteria: bool = False
    unverified_solution: bool = False
    multi_party_coordination: bool = False
    new_customer: bool = False
    unlimited_revisions: bool = False
    high_responsibility_industry: bool = False
    scope_uncertain: bool = False

    # 完整度关键字段状态（工装/夹具类附加字段）
    acceptance_criteria_known: bool = False
    workpiece_info_known: bool = False
    cycle_time_known: bool = False
    utilities_known: bool = False
    safety_requirements_known: bool = False

    # 客户可读输出
    assumptions: list[str] = []
    exclusions: list[str] = []
    clarification_questions: list[str] = []

    # 由确定性完整度计算器回填
    field_states: dict[str, str] = {}

    # 输入质量（由 VALIDATE_REQUIREMENTS 阶段写入）
    input_conflicts: list[str] = []
    fixed_fees_cny: float = 0.0


class ClassificationCandidate(BaseModel):
    project_type: str
    confidence: float = Field(ge=0, le=1)


class ProjectClassification(BaseModel):
    project_type: str
    confidence: float | None = Field(default=None, ge=0, le=1)
    candidates: list[ClassificationCandidate] = []


class EstimatedHours(BaseModel):
    base: float = 0.0
    deliverables: float = 0.0
    professional_scope: float = 0.0
    complexity_adjustment: float = 0.0
    communication_and_revision: float = 0.0
    risk_buffer: float = 0.0
    total: float = 0.0


class AppliedRule(BaseModel):
    rule_id: str
    description: str


class AppliedFactors(BaseModel):
    complexity_factor: float
    risk_factor: float
    rush_factor: float
    risk_buffer_ratio: float


class PriceRange(BaseModel):
    currency: str
    minimum: float | None = None
    recommended: float | None = None
    maximum: float | None = None


class QuoteCalculation(BaseModel):
    rule_set_version: str
    quote_type: str
    currency: str
    project_type: str
    estimated_hours: EstimatedHours
    hours_by_role: dict[str, float]
    estimated_standard_cycle_days: float
    applied_rules: list[AppliedRule]
    applied_factors: AppliedFactors
    price: PriceRange
    manual_review_required: bool
    review_reasons: list[str]
    suggested_review_reasons: list[str]
    calculation_snapshot: dict[str, Any]


class QuoteReview(BaseModel):
    manual_review_required: bool
    review_reasons: list[str]
    suggested_review_reasons: list[str]
    reviewer_notes: list[str] = []


class ReviewLLMOutput(BaseModel):
    """审核助手（LLM）输出：只能增加审核项，不得修改价格/工时/系数。"""

    reviewer_notes: list[str] = []
    extra_review_reasons: list[str] = []
