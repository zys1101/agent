"""AI 报价方法（AI_quote 原型）独立演示脚本。

用法：
  python scripts/run_ai_quote.py --requirement "客户需求文本" [--delivery-days 10]
      [--urgent] [--images a.png b.jpg] [--similar-cases cases.json]
      [--completeness 0.85] [--enable-addon]

流程与原型 main.py 一致：
  1. （有图片时）Qwen3-VL 图片理解 -> 文字总结；
  2. 系统按交付天数判定加急等级；
  3. Qwen3-VL 综合评估（参考价格表 + 图片总结 + 相似案例）输出结构化参数；
  4. AiQuotePricing 确定性算价（工时 x 50 x 复杂度 x 加急 + 交付物加项）。

环境变量：OLLAMA_HOST / OLLAMA_PORT / OLLAMA_MODEL（默认 127.0.0.1:11434 / qwen3-vl:8b）。
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from quote_agent.ai_evaluator import PROMPT_IMAGE_DESC, AiQuoteEvaluator
from quote_agent.ai_quote import AiQuotePricing, AiQuoteRules, delivery_days_to_urgency
from quote_agent.llm import LLMUnavailableError, OllamaClient


def _load_similar_cases(path: str | None) -> list[dict]:
    if not path:
        return []
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("cases", [])
    return list(data)


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="AI 报价方法（AI_quote 原型）演示")
    parser.add_argument("--requirement", required=True, help="客户文字需求")
    parser.add_argument("--delivery-days", type=float, default=None, help="期望交付天数")
    parser.add_argument("--urgent", action="store_true", help="是否加急（无交付天数时使用）")
    parser.add_argument("--images", nargs="*", default=[], help="需求图片路径列表")
    parser.add_argument("--similar-cases", default=None, help="相似案例 JSON 文件路径")
    parser.add_argument("--completeness", type=float, default=None, help="需求完整度 0-1（可选）")
    parser.add_argument("--enable-addon", action="store_true", help="启用交付物固定单价加项")
    args = parser.parse_args()

    rules = AiQuoteRules.load()
    if args.enable_addon:
        rules.enable_deliverable_addon = True
    ollama = OllamaClient.from_env(os.environ)
    evaluator = AiQuoteEvaluator(ollama, rules)

    # 步骤 1：图片理解（有图片时）
    image_summary = "无图片"
    if args.images:
        prompt1 = PROMPT_IMAGE_DESC.format(requirement_text=args.requirement)
        image_summary = ollama.generate(prompt1, images=args.images)

    # 步骤 2：系统判定加急等级
    urgency_level = delivery_days_to_urgency(args.delivery_days, args.urgent)

    # 步骤 3：综合评估
    similar_cases = _load_similar_cases(args.similar_cases)
    evaluation = evaluator.evaluate(
        requirement_text=args.requirement,
        image_summary=image_summary,
        similar_cases=similar_cases,
        delivery_days=args.delivery_days,
        is_urgent=args.urgent,
        images=args.images or None,
    )

    # 步骤 4：确定性算价
    pricing = AiQuotePricing(rules).calculate(
        evaluation,
        completeness_score=args.completeness,
    )

    result = {
        "success": True,
        "urgency_level": urgency_level,
        "urgency_label": rules.urgency_label(urgency_level),
        "image_summary": image_summary,
        "similar_cases": similar_cases,
        "ai_analysis": evaluation.model_dump(mode="json"),
        "pricing": pricing.model_dump(mode="json"),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except LLMUnavailableError as exc:
        print(f"错误：无法连接 Ollama，请确认服务已启动（{exc}）", file=__import__("sys").stderr)
        raise SystemExit(2)
