"""AI 报价方法评估 Agent（AI_quote 原型提示词移植）。

LLM 只负责"定性"：理解需求/图片/相似案例，输出工时、复杂度、交付物等参数；
加急等级由系统按交付天数判定并强制覆盖，价格一律交给 AiQuotePricing 计算。
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from .ai_quote import (
    AiQuoteEvaluation,
    AiQuoteRules,
    COMPLEXITY_TIERS,
    delivery_days_to_urgency,
)
from .llm import LLM, LLMOutputError

# 步骤 1：图片理解（原样复刻原型 PROMPT_IMAGE_DESC）
PROMPT_IMAGE_DESC = """你是一位经验丰富的机械设计师。请仔细分析以下客户上传的需求图片，结合客户的文字描述，总结这个设计任务的核心特点。
【客户文字需求】
{requirement_text}
请输出一段简洁的描述（200字以内），涵盖以下要点：
1. 这是什么类型的零件/产品
2. 主要的结构特点
3. 可能涉及的设计难点
4. 需要的交付物
直接输出描述文本，不需要其他格式。"""

# 步骤 4：综合评估（原样复刻原型 PROMPT_EVALUATION）
PROMPT_EVALUATION = """你是一位经验丰富的机械设计师，正在评估一个新订单的设计工时和报价参数。
{reference_table}
【客户文字需求】
{requirement_text}
【图片理解总结】
{image_summary}
【相似历史案例参考】
{similar_cases}
【交付要求】
期望交付天数: {delivery_days}天
加急等级(系统已判定): {urgency_level} - {urgency_label}
请综合以上所有信息，仔细分析后输出一个JSON格式的评估结果。JSON格式如下：
{{
  "part_type": "零件类型（如：电动滑板车）",
  "project_category": "项目类别（如：机械机构设计）",
  "project_subtype": "项目子类（如：角度调节机构设计）",
  "deliverables": ["交付物列表"],
  "deliverable_detail": {{
    "assembly_count": 装配体数量(整数，没有就填0),
    "part_drawing_count": 零件图数量(整数，没有就填0),
    "machining_drawing_count": 加工图数量(整数，没有就填0),
    "process_card_count": 工序卡数量(整数，没有就填0),
    "procedure_card_count": 规程卡数量(整数，没有就填0),
    "blank_drawing_count": 毛坯图数量(整数，没有就填0),
    "model_count": 3D模型数量(整数，没有就填0),
    "instruction_book_count": 说明书数量(整数，没有就填0)
  }},
  "complexity_tier": "复杂度等级(必须是simple/normal/complex三选一)",
  "estimated_hours": "预估工时(小时，熟练机械设计师完成所需时间，整数。参考上方价格表理解任务量级)",
  "urgency_level": {urgency_level},
  "difficulty_reason": ["难点1", "难点2"],
  "case_summary": "用一段话总结这个设计任务的核心内容和难点"
}}
注意：
- complexity_tier 只能是 simple、normal、complex 三选一
- urgency_level 直接使用系统给定的值 {urgency_level}，不要自己判断
- estimated_hours 要参考参考价格表理解任务量级，一个示意图300-500元对应约6-10小时，一个加工图纸2000-3000元对应约40-60小时
只输出JSON，不要输出其他任何内容。"""


class ImageSummary(BaseModel):
    """图片理解步骤的结构化输出（原型 PROMPT_IMAGE_DESC 的 JSON 包装）。"""

    summary: str


class AiQuoteEvaluator:
    """调用 LLM 生成 AiQuoteEvaluation，并做系统级强制覆盖。"""

    def __init__(self, llm: LLM, rules: AiQuoteRules):
        self.llm = llm
        self.rules = rules

    def summarize_images(self, requirement_text: str, images: list[str]) -> str:
        """步骤 1：让视觉模型理解需求图片，返回文字总结（复刻原型）。"""
        prompt = (
            PROMPT_IMAGE_DESC.format(requirement_text=requirement_text)
            + '\n\n只输出 JSON：{"summary": "你的描述"}'
        )
        result = self.llm.generate_structured(
            "你是机械设计报价系统的图片理解助手。只输出合法 JSON 对象。",
            prompt,
            ImageSummary,
            images=images,
        )
        assert isinstance(result, ImageSummary)
        return result.summary.strip() or "有图片但未能提取有效描述"

    def build_evaluation_prompt(
        self,
        requirement_text: str,
        image_summary: str = "无图片",
        similar_cases: list[dict[str, Any]] | None = None,
        delivery_days: float | int | None = None,
        urgency_level: int | None = None,
    ) -> str:
        urgency_level = (
            delivery_days_to_urgency(delivery_days)
            if urgency_level is None
            else urgency_level
        )
        return PROMPT_EVALUATION.format(
            reference_table=self.rules.reference_price_table,
            requirement_text=requirement_text,
            image_summary=image_summary,
            similar_cases=json.dumps(similar_cases or [], ensure_ascii=False, indent=2),
            delivery_days=delivery_days if delivery_days is not None else "未指定",
            urgency_level=urgency_level,
            urgency_label=self.rules.urgency_label(urgency_level),
        )

    def evaluate(
        self,
        requirement_text: str,
        image_summary: str = "无图片",
        similar_cases: list[dict[str, Any]] | None = None,
        delivery_days: float | int | None = None,
        is_urgent: bool = False,
        images: list[str] | None = None,
    ) -> AiQuoteEvaluation:
        """执行评估并强制覆盖：加急等级用系统值，非法复杂度回退 normal。"""

        # 加急等级由系统判定（交付天数映射），LLM 输出一律覆盖
        urgency_level = delivery_days_to_urgency(delivery_days, is_urgent)
        prompt = self.build_evaluation_prompt(
            requirement_text=requirement_text,
            image_summary=image_summary,
            similar_cases=similar_cases,
            delivery_days=delivery_days,
            urgency_level=urgency_level,
        )
        result = self.llm.generate_structured(
            "你是机械设计报价评估助手。只输出合法 JSON，价格一律由系统计算，你不得输出任何价格字段。",
            prompt,
            AiQuoteEvaluation,
            images=images,
        )
        assert isinstance(result, AiQuoteEvaluation)

        # 强制覆盖：加急等级用系统判定的值（原型 main.py 的强制覆盖逻辑）
        result.urgency_level = urgency_level
        # 兜底：模型偶发输出非法复杂度时回退 normal，避免算价崩溃
        if result.complexity_tier not in COMPLEXITY_TIERS:
            result.complexity_tier = "normal"
        # 兜底：工时必须为正数，否则视为无效输出
        if result.estimated_hours <= 0:
            raise LLMOutputError(f"invalid estimated_hours: {result.estimated_hours}")
        return result
