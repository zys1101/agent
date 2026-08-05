"""LLM 客户端 JSON 解析与修复路径测试。"""

import pytest

from quote_agent.llm import _extract_json


def test_extract_json_with_fences_and_noise():
    text = '模型说：```json\n{"ok": true, "x": 1}\n``` 以上。'
    assert _extract_json(text) == {"ok": True, "x": 1}


def test_extract_json_plain():
    assert _extract_json('{"ok": true}') == {"ok": True}


def test_extract_json_invalid_raises():
    with pytest.raises(ValueError):
        _extract_json("没有 JSON")
