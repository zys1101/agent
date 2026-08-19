"""本地 AI 客服 Worker 配置。"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel


class CsAgentSettings(BaseModel):
    agent_id: str = "office-4070super-01"
    agent_version: str = "0.1.0"
    ollama_host: str = "127.0.0.1"
    ollama_port: int = 11434
    ollama_model: str = "qwen3-vl:8b"
    poll_interval_s: float = 2.0
    max_history: int = 10
    max_reply_tokens: int = 500
    temperature: float = 0.6
    timeout_s: float = 120.0
    snapshot_dir: Path = Path("data/cs_snapshots")

    @classmethod
    def from_env(cls, env: dict | None = None) -> "CsAgentSettings":
        env = env or os.environ
        return cls(
            agent_id=env.get("AGENT_ID", cls.model_fields["agent_id"].default),
            agent_version=env.get("AGENT_VERSION", cls.model_fields["agent_version"].default),
            ollama_host=env.get("OLLAMA_HOST", cls.model_fields["ollama_host"].default),
            ollama_port=int(env.get("OLLAMA_PORT", cls.model_fields["ollama_port"].default)),
            ollama_model=env.get("OLLAMA_MODEL", cls.model_fields["ollama_model"].default),
            poll_interval_s=float(env.get("CS_POLL_INTERVAL", cls.model_fields["poll_interval_s"].default)),
            max_history=int(env.get("CS_MAX_HISTORY", cls.model_fields["max_history"].default)),
            max_reply_tokens=int(
                env.get("CS_MAX_REPLY_TOKENS", cls.model_fields["max_reply_tokens"].default)
            ),
            temperature=float(env.get("CS_TEMPERATURE", cls.model_fields["temperature"].default)),
            timeout_s=float(env.get("CS_TIMEOUT_S", cls.model_fields["timeout_s"].default)),
            snapshot_dir=Path(env.get("CS_SNAPSHOT_DIR", "data/cs_snapshots")),
        )
