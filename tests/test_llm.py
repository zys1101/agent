"""LLM 客户端 JSON 解析与修复路径测试。"""

import json

import pytest

from quote_agent.cache import QuoteCache
from quote_agent.llm import CachedLLM, OllamaClient, _extract_json
from quote_agent.models import ProjectClassification


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def test_extract_json_with_fences_and_noise():
    text = '模型说：```json\n{"ok": true, "x": 1}\n``` 以上。'
    assert _extract_json(text) == {"ok": True, "x": 1}


def test_extract_json_plain():
    assert _extract_json('{"ok": true}') == {"ok": True}


def test_extract_json_invalid_raises():
    with pytest.raises(ValueError):
        _extract_json("没有 JSON")


def test_generate_dispatches_text_vs_vision_model(monkeypatch, tmp_path):
    captured = {}

    def fake_post(url, **kwargs):
        captured["payload"] = kwargs.get("json") or {}
        return FakeResponse({"response": "{}"})

    monkeypatch.setattr("quote_agent.llm.httpx.post", fake_post)
    client = OllamaClient(model="vl-model", text_model="text-model")

    client.generate("纯文本需求")
    assert captured["payload"]["model"] == "text-model"

    img = tmp_path / "sketch.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n fake")
    client.generate("看图提取尺寸", images=[str(img)])
    assert captured["payload"]["model"] == "vl-model"
    assert captured["payload"]["images"]


def test_structured_output_uses_json_format_and_no_thinking(monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured["payload"] = kwargs.get("json") or {}
        return FakeResponse({"response": '{"ok": true}'})

    monkeypatch.setattr("quote_agent.llm.httpx.post", fake_post)
    client = OllamaClient(model="m", text_model="t", think=False, num_predict=512)

    client.generate_json("结构化输出")
    assert captured["payload"]["format"] == "json"
    assert captured["payload"]["options"]["think"] is False
    assert captured["payload"]["options"]["num_predict"] == 512
    assert captured["payload"]["model"] == "t"  # 纯文本走轻量模型

    # 普通 generate 不加 format（供人工调试等非结构化场景）
    client.generate("普通文本")
    assert "format" not in captured["payload"]
    assert captured["payload"]["options"]["think"] is False


def test_think_can_be_enabled_via_env(monkeypatch):
    client = OllamaClient.from_env(
        env={
            "OLLAMA_HOST": "127.0.0.1",
            "LLM_THINK": "true",
            "LLM_MAX_TOKENS": "2048",
            "TEXT_MODEL": "t",
            "OLLAMA_MODEL": "vl",
        }
    )
    assert client.think is True
    assert client.num_predict == 2048
    assert client.text_model == "t"


class CountingLLM:
    def __init__(self, model="test-model"):
        self.model = model
        self.calls = 0

    def generate_structured(self, system, user, model_cls, images=None):
        self.calls += 1
        return ProjectClassification(project_type="simple_part", confidence=0.9)


def test_cached_llm_reuses_result(tmp_path):
    cache = QuoteCache(tmp_path / "agent.db")
    llm = CountingLLM()
    cached = CachedLLM(llm, cache)

    r1 = cached.generate_structured("s", "u", ProjectClassification)
    assert llm.calls == 1
    r2 = cached.generate_structured("s", "u", ProjectClassification)
    assert llm.calls == 1  # 命中缓存，不再调用底层 LLM
    assert r1 == r2
    assert cache.count_llm_cache() == 1


def test_cached_llm_key_includes_prompt_and_images(tmp_path):
    cache = QuoteCache(tmp_path / "agent.db")
    llm = CountingLLM()
    cached = CachedLLM(llm, cache)

    cached.generate_structured("s", "u1", ProjectClassification)
    cached.generate_structured("s", "u2", ProjectClassification)
    assert llm.calls == 2  # prompt 不同 -> 不同 key

    img = tmp_path / "a.png"
    img.write_bytes(b"img-a")
    img2 = tmp_path / "b.png"
    img2.write_bytes(b"img-b")
    cached.generate_structured("s", "u1", ProjectClassification, images=[str(img)])
    cached.generate_structured("s", "u1", ProjectClassification, images=[str(img2)])
    assert llm.calls == 4  # 图片内容不同 -> 不同 key


def test_cached_llm_image_key_is_content_based(tmp_path):
    cache = QuoteCache(tmp_path / "agent.db")
    llm = CountingLLM()
    cached = CachedLLM(llm, cache)

    d1 = tmp_path / "task_a"
    d2 = tmp_path / "task_b"
    d1.mkdir()
    d2.mkdir()
    (d1 / "sketch.png").write_bytes(b"same-image-content")
    (d2 / "sketch.png").write_bytes(b"same-image-content")

    cached.generate_structured("s", "u", ProjectClassification, images=[str(d1 / "sketch.png")])
    cached.generate_structured("s", "u", ProjectClassification, images=[str(d2 / "sketch.png")])
    assert llm.calls == 1  # 路径不同但图片内容相同 -> 命中同一缓存


def test_cached_llm_recovers_from_corrupt_payload(tmp_path):
    cache = QuoteCache(tmp_path / "agent.db")
    llm = CountingLLM()
    cached = CachedLLM(llm, cache)
    key = cached._key("s", "u", ProjectClassification, None)
    cache.set_llm_cache(key, "test-model", "ProjectClassification", "{broken json")

    result = cached.generate_structured("s", "u", ProjectClassification)
    assert llm.calls == 1  # 损坏缓存被作废并重算
    assert result.project_type == "simple_part"
    assert cache.get_llm_cache(key) is not None  # 重新写入有效缓存
