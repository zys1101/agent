"""Ollama 客户端：稳定 JSON 输出 + 一次修复（AWF 第 5 节）。"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from typing import Protocol

import httpx
from pydantic import BaseModel, ValidationError


class LLMUnavailableError(Exception):
    pass


class LLMOutputError(Exception):
    pass


class LLM(Protocol):
    def generate_structured(
        self,
        system: str,
        user: str,
        model_cls: type[BaseModel],
        images: list[str] | None = None,
    ) -> BaseModel: ...


class LLMResultCache(Protocol):
    """CachedLLM 依赖的最小缓存接口（QuoteCache 已实现）。"""

    def get_llm_cache(self, key: str) -> str | None: ...
    def set_llm_cache(self, key: str, model: str, schema: str, payload: str) -> None: ...
    def delete_llm_cache(self, key: str) -> None: ...


class CachedLLM:
    """LLM 结果缓存包装器。

    以 system+user+模型+schema+图片内容 的哈希为 key；命中时直接反序列化，
    避免重复推理。缓存内容与当前 Schema 不兼容时自动作废重算。
    """

    def __init__(self, llm: LLM, cache: LLMResultCache):
        self.llm = llm
        self.cache = cache

    def generate_structured(
        self,
        system: str,
        user: str,
        model_cls: type[BaseModel],
        images: list[str] | None = None,
    ) -> BaseModel:
        key = self._key(system, user, model_cls, images)
        cached = self.cache.get_llm_cache(key)
        if cached is not None:
            try:
                return model_cls.model_validate(json.loads(cached))
            except (json.JSONDecodeError, ValidationError):
                self.cache.delete_llm_cache(key)

        result = self.llm.generate_structured(system, user, model_cls, images)
        self.cache.set_llm_cache(
            key,
            getattr(self.llm, "model", ""),
            model_cls.__name__,
            json.dumps(result.model_dump(mode="json"), ensure_ascii=False),
        )
        return result

    def _key(
        self,
        system: str,
        user: str,
        model_cls: type[BaseModel],
        images: list[str] | None,
    ) -> str:
        parts = {
            "system": system,
            "user": user,
            "model": getattr(self.llm, "model", ""),
            "schema": model_cls.__name__,
            "images": [self._image_fingerprint(path) for path in (images or [])],
        }
        blob = json.dumps(parts, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    @staticmethod
    def _image_fingerprint(path: str) -> dict:
        try:
            digest = hashlib.sha256()
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 16), b""):
                    digest.update(chunk)
            # 只按内容哈希：同一张图放在不同任务目录也应命中同一缓存
            return {"sha256": digest.hexdigest()}
        except OSError:
            return {"path": path, "sha256": None}


def _extract_json(text: str) -> dict:
    """去除代码围栏后解析 JSON。"""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("response contains no JSON object")
    return json.loads(cleaned[start : end + 1])


def _env_int(env: dict, name: str, default: int | None = None) -> int | None:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class OllamaClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "qwen3-vl:8b",
        text_model: str = "",
        embed_model: str = "bge-m3:latest",
        timeout_s: float = 180.0,
        temperature: float = 0.0,
        top_p: float = 0.9,
        think: bool = False,
        num_predict: int | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.text_model = text_model or model
        self.embed_model = embed_model
        self.timeout_s = timeout_s
        self.temperature = temperature
        self.top_p = top_p
        self.think = think
        self.num_predict = num_predict

    @classmethod
    def from_env(cls, env: dict | None = None) -> "OllamaClient":
        env = env or os.environ
        host = env.get("OLLAMA_HOST", "127.0.0.1")
        port = env.get("OLLAMA_PORT", "11434")
        return cls(
            base_url=f"http://{host}:{port}",
            model=env.get("OLLAMA_MODEL", "qwen3-vl:8b"),
            text_model=env.get("TEXT_MODEL", "") or env.get("OLLAMA_MODEL", "qwen3-vl:8b"),
            embed_model=env.get("EMBEDDING_MODEL", "bge-m3:latest"),
            think=env.get("LLM_THINK", "false").strip().lower() in ("1", "true", "yes"),
            num_predict=_env_int(env, "LLM_MAX_TOKENS"),
        )

    def generate(
        self,
        prompt: str,
        images: list[str] | None = None,
        model: str | None = None,
        format_json: bool = False,
    ) -> str:
        # 模型分派：带图片的任务用视觉模型，纯文本任务用轻量文本模型（提速）
        if model is None:
            model = self.model if images else self.text_model
        options: dict = {
            "temperature": self.temperature,
            "top_p": self.top_p,
            # qwen3 系默认开启 thinking，会先输出大量推理 token；默认关闭以提速
            "think": self.think,
        }
        if self.num_predict is not None:
            options["num_predict"] = self.num_predict
        payload: dict = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": options,
        }
        if format_json:
            payload["format"] = "json"
        if images:
            encoded = []
            for path in images:
                with open(path, "rb") as fh:
                    encoded.append(base64.b64encode(fh.read()).decode("ascii"))
            payload["images"] = encoded
        try:
            resp = httpx.post(
                f"{self.base_url}/api/generate",
                json=payload,
                timeout=self.timeout_s,
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise LLMUnavailableError(f"Ollama request failed: {exc}") from exc
        response = data.get("response", "") or ""
        # 兜底：qwen3 系列推理模型的 JSON 可能被放进 thinking 字段
        if not response.strip() and data.get("thinking"):
            return data["thinking"]
        return response

    def generate_json(self, prompt: str, images: list[str] | None = None) -> dict:
        text = self.generate(prompt, images, format_json=True)
        try:
            return _extract_json(text)
        except (ValueError, json.JSONDecodeError) as exc:
            raise LLMOutputError(f"invalid JSON from model: {exc}") from exc

    def embed(self, texts: list[str]) -> list[list[float]]:
        """调用 Ollama /api/embed（默认 bge-m3），供 RAG 使用。"""
        if not texts:
            return []
        try:
            resp = httpx.post(
                f"{self.base_url}/api/embed",
                json={"model": self.embed_model, "input": texts},
                timeout=self.timeout_s,
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise LLMUnavailableError(f"embedding request failed: {exc}") from exc
        embeddings = data.get("embeddings")
        if not embeddings:
            raise LLMOutputError("embedding response contains no embeddings")
        return embeddings

    def generate_structured(
        self,
        system: str,
        user: str,
        model_cls: type[BaseModel],
        images: list[str] | None = None,
    ) -> BaseModel:
        first_prompt = f"{system}\n\n{user}\n\n只输出一个合法 JSON 对象，不要输出其他内容。"
        try:
            return model_cls.model_validate(self.generate_json(first_prompt, images))
        except (LLMOutputError, ValidationError) as exc:
            repair_prompt = (
                f"{system}\n\n{user}\n\n"
                f"你上一次的输出不合法或不符合 Schema，错误信息：{exc}\n"
                "请只输出修正后的合法 JSON 对象，不要输出其他内容。"
            )
            try:
                repaired = self.generate_json(repair_prompt, images)
                return model_cls.model_validate(repaired)
            except (LLMOutputError, ValidationError) as exc2:
                raise LLMOutputError(f"model output invalid after repair: {exc2}") from exc2
