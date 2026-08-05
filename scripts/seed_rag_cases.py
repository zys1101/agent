"""把脱敏历史案例（examples/knowledge_cases/*.json）写入 Qdrant。

用法：docker compose run --rm quote-agent python scripts/seed_rag_cases.py
"""

from __future__ import annotations

import json
from pathlib import Path

from quote_agent.llm import OllamaClient
from quote_agent.rag import CaseRecord, RagStore


def main() -> int:
    cases_dir = Path("examples/knowledge_cases")
    records = []
    for path in sorted(cases_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if "price_range_cny" in data and isinstance(data["price_range_cny"], list):
            data["price_range_cny"] = tuple(data["price_range_cny"])
        records.append(CaseRecord.model_validate(data))

    llm = OllamaClient.from_env()
    store = RagStore.from_env(embed_fn=llm.embed)
    n = store.upsert_cases(records)
    print(f"已写入 {n} 个脱敏案例到 collection={store.collection}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
