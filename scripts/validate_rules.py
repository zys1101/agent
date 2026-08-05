"""校验 config/quote_rules.yaml 是否合法且通过交叉校验。"""

from __future__ import annotations

from quote_agent.config import QuoteRules


def main() -> int:
    rules = QuoteRules.load()
    print(
        f"OK rule_set={rules.rule_set_version} sha256={rules.source_sha256[:16]} "
        f"project_types={len(rules.project_types)} deliverables={len(rules.deliverables)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
