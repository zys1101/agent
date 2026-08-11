"""把脱敏历史案例批量导入 Qdrant 向量库（RAG 案例库）。

支持三种数据形态（同一目录可混用）：
- JSON 单条记录：{"case_id": "...", ...}
- JSON 数组：[{...}, {...}]
- JSONL：每行一条 JSON 记录（允许 # 开头的注释行）

用法：
  python scripts/seed_rag_cases.py                                   # 默认导入 examples/knowledge_cases
  python scripts/seed_rag_cases.py --data-dir data/my_cases          # 指定数据目录
  python scripts/seed_rag_cases.py --data-dir data/my_cases --glob "*.jsonl" --glob "*.json"
  python scripts/seed_rag_cases.py --dry-run                         # 只校验格式与统计，不连 Qdrant/Ollama

环境变量：
  QDRANT_HOST / QDRANT_PORT / QDRANT_COLLECTION / EMBED_DIM
  OLLAMA_HOST / OLLAMA_PORT / EMBEDDING_MODEL
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

from quote_agent.llm import OllamaClient
from quote_agent.rag import CaseRecord, RagStore

DEFAULT_DATA_DIR = "examples/knowledge_cases"
DEFAULT_PATTERNS = ["*.json", "*.jsonl"]


def load_records_from_path(path: Path) -> list[dict]:
    """读取单个文件：支持单条 JSON、JSON 数组、JSONL。"""
    if path.suffix.lower() == ".jsonl":
        records: list[dict] = []
        with path.open("r", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, start=1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path}:{line_no} 不是合法 JSON：{exc}") from exc
        return records

    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else [data]


def collect_cases(data_dir: Path, patterns: list[str]) -> list[CaseRecord]:
    """扫描目录并校验为 CaseRecord 列表。"""
    files = sorted({p for pattern in patterns for p in data_dir.glob(pattern)})
    if not files:
        raise FileNotFoundError(f"在 {data_dir} 下未找到匹配 {patterns} 的数据文件")

    records: list[CaseRecord] = []
    for path in files:
        for raw in load_records_from_path(path):
            if "price_range_cny" in raw and isinstance(raw["price_range_cny"], list):
                raw["price_range_cny"] = tuple(raw["price_range_cny"])
            records.append(CaseRecord.model_validate(raw))
    return records


def format_stats(records: list[CaseRecord]) -> str:
    by_cat = Counter(r.category or "(未分类)" for r in records)
    by_type = Counter(r.project_type for r in records)
    lines = [
        f"共 {len(records)} 条案例",
        "按业务大类: " + ", ".join(f"{k}={v}" for k, v in sorted(by_cat.items())),
        "按 project_type: " + ", ".join(f"{k}={v}" for k, v in sorted(by_type.items())),
    ]
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="批量导入脱敏案例到 Qdrant 向量库（支持 JSON / JSON 数组 / JSONL）"
    )
    parser.add_argument(
        "--data-dir",
        default=DEFAULT_DATA_DIR,
        help=f"数据目录（默认 {DEFAULT_DATA_DIR}）",
    )
    parser.add_argument(
        "--glob",
        action="append",
        default=[],
        help="文件匹配模式，可多次指定（默认 *.json 和 *.jsonl）",
    )
    parser.add_argument(
        "--collection",
        default=os.environ.get("QDRANT_COLLECTION", "quote_cases"),
        help="Qdrant collection 名称（默认 quote_cases，可用 QDRANT_COLLECTION 覆盖）",
    )
    parser.add_argument(
        "--dim",
        type=int,
        default=int(os.environ.get("EMBED_DIM", "1024")),
        help="向量维度（默认 1024，即 bge-m3；换嵌入模型时同步修改）",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=64,
        help="每批嵌入/写入的条数（默认 64）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只校验数据格式与统计，不连接 Qdrant/Ollama",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    patterns = args.glob or DEFAULT_PATTERNS
    data_dir = Path(args.data_dir)

    try:
        records = collect_cases(data_dir, patterns)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1

    print(format_stats(records))
    if args.dry_run:
        print("dry-run 校验通过：数据格式正确，未连接 Qdrant/Ollama，未写入。")
        return 0

    llm = OllamaClient.from_env()
    store = RagStore.from_env(collection=args.collection, embed_fn=llm.embed, dim=args.dim)
    n = store.upsert_cases(records, batch_size=args.batch_size)
    print(f"已写入 {n} 条案例到 collection={store.collection}（batch_size={args.batch_size}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
