"""RagStore 测试：Qdrant 可达时用假 embedding 验证检索；不可达则跳过。"""

import os
import time

import pytest

from quote_agent.rag import CaseRecord, RagStore

TOKENS = ["压装", "钣金", "焊接", "夹具", "电机"]


def fake_embed(texts):
    out = []
    for text in texts:
        vector = [0.0] * len(TOKENS)
        for i, token in enumerate(TOKENS):
            if token in text:
                vector[i] = 1.0
        out.append(vector)
    return out


@pytest.fixture(scope="module")
def store():
    host = os.environ.get("QDRANT_HOST", "127.0.0.1")
    port = int(os.environ.get("QDRANT_PORT", "6333"))
    collection = f"test_cases_{int(time.time())}"
    try:
        rag = RagStore(host=host, port=port, collection=collection, embed_fn=fake_embed, dim=len(TOKENS), timeout_s=3)
        rag.ping()
    except Exception as exc:  # Qdrant 未启动时跳过
        pytest.skip(f"Qdrant not reachable: {exc}")
    yield rag
    try:
        rag.client.delete_collection(collection)
    except Exception:
        pass


def test_upsert_and_search(store: RagStore):
    store.upsert_cases(
        [
            CaseRecord(
                case_id="c1",
                project_type="pneumatic_press_fixture",
                category="tooling_fixture",
                summary="电机壳体轴承压装工装，气动夹具",
                deliverables=["three_d_assembly", "bom"],
                risk_notes=["压装力需确认"],
                typical_hours=156,
                price_range_cny=(48000, 65000),
            ),
            CaseRecord(
                case_id="c2",
                project_type="sheet_metal_part",
                category="sheet_metal_cabinet",
                summary="机柜钣金件，折弯件罩壳",
                deliverables=["three_d_model", "two_d_part_drawing"],
                typical_hours=12,
            ),
        ]
    )
    hits = store.search("电机壳体气动压装工装", top_k=3)
    assert hits
    assert hits[0][0].case_id == "c1"
    assert hits[0][1] > 0


def test_search_filter_by_project_type(store: RagStore):
    hits = store.search("压装工装", project_type="sheet_metal_part", top_k=3)
    assert all(case.project_type == "sheet_metal_part" for case, _ in hits)


def test_search_filter_by_category(store: RagStore):
    hits = store.search("压装工装", category="tooling_fixture", top_k=3)
    assert hits
    assert all(case.category == "tooling_fixture" for case, _ in hits)


def test_search_project_type_or_category(store: RagStore):
    # project_type 与 category 同时给定：任一匹配即命中（官网案例可能只按大类入库）
    hits = store.search(
        "压装工装",
        project_type="pneumatic_press_fixture",
        category="sheet_metal_cabinet",
        top_k=5,
    )
    assert hits
    assert any(case.project_type == "pneumatic_press_fixture" for case, _ in hits)
    assert any(case.category == "sheet_metal_cabinet" for case, _ in hits)
