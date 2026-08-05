"""验证 Ollama 连通性与结构化 JSON 输出路径。"""

from __future__ import annotations

from dotenv import load_dotenv
from pydantic import BaseModel

from quote_agent.llm import OllamaClient


class Ping(BaseModel):
    ok: bool
    model_echo: str


def main() -> int:
    load_dotenv()
    client = OllamaClient.from_env()
    print(f"连接 {client.base_url}，模型 {client.model}（首次调用会加载模型，可能较慢）")
    result = client.generate_structured(
        "你是测试助手。",
        '只输出一个 JSON 对象，不要输出任何其他内容：{"ok": true, "model_echo": "连接成功"}',
        Ping,
    )
    print(result.model_dump())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
