"""AI 报价泛化评测：14 条真实历史案例，留一法（leave-one-out）。

对每条案例：把该案例自己的成交价从历史校准表中剔除，仅凭其余 13 条案例
评估其工时，验证报价机制的**泛化能力**（而非针对单例的规则）。

用法：
  python scripts/benchmark_ai_quote.py [--days 7] [--limit N] [--out results.json]

评测指标：预测价 vs 真实成交价，统计 ±15% / ±30% 命中率与平均绝对偏差。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from quote_agent.ai_evaluator import AiQuoteEvaluator
from quote_agent.ai_quote import AiQuotePricing, AiQuoteRules
from quote_agent.llm import OllamaClient

CASES_PATH = Path("examples/knowledge_cases/ai_quote_historical_cases.json")


def _row(case: dict) -> str:
    deliverables = "、".join(case.get("deliverables", [])) or "无"
    return f"- {case['summary'].strip()}（交付：{deliverables}）→ {case['price_range_cny'][0]}元"


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="AI 报价留一法泛化评测")
    parser.add_argument("--days", type=float, default=7.0, help="交期天数（默认 7，正常档）")
    parser.add_argument("--limit", type=int, default=0, help="只评测前 N 条（0=全部）")
    parser.add_argument("--out", default=None, help="结果 JSON 输出路径")
    args = parser.parse_args(argv)

    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    if args.limit:
        cases = cases[: args.limit]

    rules = AiQuoteRules.load()
    llm = OllamaClient.from_env()
    results = []

    for idx, case in enumerate(cases, start=1):
        expected = case["price_range_cny"][0]
        case_id = case["case_id"]
        print(f"[{idx}/{len(cases)}] {case_id} 评测中...", flush=True)
        # 留一法：剔除该案例自己的成交价行
        others = "\n".join(_row(c) for c in cases if c["case_id"] != case_id)
        rules_loo = rules.model_copy(update={"historical_price_reference": others})
        evaluator = AiQuoteEvaluator(llm, rules_loo)
        requirement_text = case["summary"].strip()
        if case.get("deliverables"):
            requirement_text += "，交付物：" + "、".join(case["deliverables"])

        try:
            evaluation = evaluator.evaluate(
                requirement_text=requirement_text,
                delivery_days=args.days,
            )
            price = AiQuotePricing(rules).calculate(evaluation)
            predicted = price.final_price
            price_min = price.price["minimum"]
            price_max = price.price["maximum"]
            diff_pct = (predicted - expected) / expected * 100
            in_range = price_min <= expected <= price_max
            results.append(
                {
                    "case_id": case_id,
                    "expected": expected,
                    "predicted": predicted,
                    "price_range": [price_min, price_max],
                    "in_range": in_range,
                    "diff_pct": round(diff_pct, 1),
                    "hours": evaluation.estimated_hours,
                    "complexity": evaluation.complexity_tier,
                    "error": None,
                }
            )
            print(
                f"    期望 {expected} 元 | 预测 {predicted} 元 "
                f"区间 [{price_min}, {price_max}] 含期望={in_range} "
                f"({diff_pct:+.0f}%) | {evaluation.estimated_hours:g}h {evaluation.complexity_tier}",
                flush=True,
            )
        except Exception as exc:
            results.append(
                {
                    "case_id": case_id,
                    "expected": expected,
                    "predicted": None,
                    "price_range": None,
                    "in_range": False,
                    "diff_pct": None,
                    "hours": None,
                    "complexity": None,
                    "error": str(exc)[:300],
                }
            )
            print(f"    失败: {exc}", flush=True)

    # ---- 汇总 ----
    ok = [r for r in results if r["predicted"] is not None]
    within_15 = sum(1 for r in ok if abs(r["diff_pct"]) <= 15)
    within_30 = sum(1 for r in ok if abs(r["diff_pct"]) <= 30)
    in_range = sum(1 for r in ok if r["in_range"])
    mape = sum(abs(r["diff_pct"]) for r in ok) / len(ok) if ok else 0.0
    print("=" * 60)
    print(f"共 {len(results)} 条，成功 {len(ok)} 条")
    print(f"±15% 命中: {within_15}/{len(ok)} ({within_15 / max(len(ok), 1):.0%})")
    print(f"±30% 命中: {within_30}/{len(ok)} ({within_30 / max(len(ok), 1):.0%})")
    print(f"期望价落在报价区间内: {in_range}/{len(ok)} ({in_range / max(len(ok), 1):.0%})")
    print(f"平均绝对偏差 (MAPE): {mape:.1f}%")
    print("=" * 60)

    if args.out:
        Path(args.out).write_text(
            json.dumps(
                {
                    "summary": {
                        "total": len(results),
                        "ok": len(ok),
                        "within_15": within_15,
                        "within_30": within_30,
                        "in_range": in_range,
                        "mape": round(mape, 1),
                    },
                    "cases": results,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"结果已保存到 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
