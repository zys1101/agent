"""Ollama 客户端：稳定 JSON 输出 + 一次修复（AWF 第 5 节）。"""

from __future__ import annotations

import base64
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


def _extract_json(text: str) -> dict:
    """去除代码围栏后解析 JSON。"""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("response contains no JSON object")
    return json.loads(cleaned[start : end + 1])


class OllamaClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "qwen3-vl:8b",
        timeout_s: float = 180.0,
        temperature: float = 0.0,
        top_p: float = 0.9,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self.temperature = temperature
        self.top_p = top_p

    @classmethod
    def from_env(cls, env: dict | None = None) -> "OllamaClient":
        env = env or os.environ
        host = env.get("OLLAMA_HOST", "127.0.0.1")
        port = env.get("OLLAMA_PORT", "11434")
        return cls(
            base_url=f"http://{host}:{port}",
            model=env.get("OLLAMA_MODEL", "qwen3-vl:8b"),
        )

    def generate(self, prompt: str, images: list[str] | None = None) -> str:
        payload: dict = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": self.temperature,
                "top_p": self.top_p,
            },
        }
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
        text = self.generate(prompt, images)
        try:
            return _extract_json(text)
        except (ValueError, json.JSONDecodeError) as exc:
            raise LLMOutputError(f"invalid JSON from model: {exc}") from exc

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
