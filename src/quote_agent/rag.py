"""Qdrant RAG：脱敏历史案例检索（AWF RETRIEVE_CASES，MVP 默认关闭）。

案例向量由本地 embedding 模型（默认 bge-m3）生成；原始客户文件不得入库。
"""

from __future__ import annotations

import os
import uuid
from typing import Callable

from pydantic import BaseModel, Field
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    VectorParams,
)


class CaseRecord(BaseModel):
    case_id: str
    project_type: str
    summary: str
    deliverables: list[str] = Field(default_factory=list)
    risk_notes: list[str] = Field(default_factory=list)
    typical_hours: float | None = None
    price_range_cny: tuple[int, int] | None = None


EmbedFn = Callable[[list[str]], list[list[float]]]


class RagStore:
    def __init__(
        self,
        host: str,
        port: int,
        collection: str = "quote_cases",
        embed_fn: EmbedFn | None = None,
        dim: int = 1024,
        timeout_s: float = 10.0,
    ):
        self.collection = collection
        self.embed_fn = embed_fn or self._default_embed
        self.client = QdrantClient(host=host, port=port, timeout=timeout_s)
        self._ensure_collection(dim)

    @classmethod
    def from_env(
        cls,
        embed_fn: EmbedFn | None = None,
        env: dict | None = None,
    ) -> "RagStore":
        env = env or os.environ
        return cls(
            host=env.get("QDRANT_HOST", "127.0.0.1"),
            port=int(env.get("QDRANT_PORT", "6333")),
            embed_fn=embed_fn,
        )

    def _default_embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("RagStore 需要注入 embed_fn（通常来自 OllamaClient.embed）")

    def _ensure_collection(self, dim: int) -> None:
        existing = self.client.get_collections().collections
        if not any(c.name == self.collection for c in existing):
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
            )

    def upsert_cases(self, cases: list[CaseRecord]) -> int:
        if not cases:
            return 0
        texts = [case.summary for case in cases]
        vectors = self.embed_fn(texts)
        points = [
            PointStruct(
                id=_stable_id(case.case_id),
                vector=vector,
                payload=case.model_dump(mode="json"),
            )
            for case, vector in zip(cases, vectors)
        ]
        self.client.upsert(collection_name=self.collection, points=points)
        return len(points)

    def search(
        self,
        query: str,
        project_type: str | None = None,
        top_k: int = 3,
    ) -> list[tuple[CaseRecord, float]]:
        vector = self.embed_fn([query])[0]
        query_filter = None
        if project_type:
            query_filter = Filter(
                must=[FieldCondition(key="project_type", match=MatchValue(value=project_type))]
            )
        # qdrant-client >=1.10 用 query_points 取代 search
        response = self.client.query_points(
            collection_name=self.collection,
            query=vector,
            limit=top_k,
            query_filter=query_filter,
        )
        return [
            (CaseRecord.model_validate(hit.payload), float(hit.score))
            for hit in response.points
        ]

    def ping(self) -> bool:
        return self.client.collection_exists(self.collection)


def _stable_id(case_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, case_id))
