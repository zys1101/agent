"""本地 AI 客服 Worker：领取聊天任务 → 本地模型生成回复 → 回传。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from quote_agent.llm import LLMUnavailableError

from .cloud import ClaimedCsTask, CloudError, CsCloudClient
from .config import CsAgentSettings
from .service import CustomerService


class CsWorker:
    def __init__(
        self,
        cloud: CsCloudClient,
        service: CustomerService,
        settings: CsAgentSettings,
    ):
        self.cloud = cloud
        self.service = service
        self.settings = settings

    def run_task(self, task: ClaimedCsTask) -> dict:
        try:
            result = self.service.reply(task.message, task.history)
            payload = {
                "result_schema_version": "1.0",
                "agent": {
                    "agent_id": self.settings.agent_id,
                    "agent_version": self.settings.agent_version,
                },
                "runtime": {
                    "ollama_model": self.settings.ollama_model,
                    "ollama_host": f"{self.settings.ollama_host}:{self.settings.ollama_port}",
                    "temperature": self.settings.temperature,
                    "max_reply_tokens": self.settings.max_reply_tokens,
                },
                "result": {
                    "reply": result["reply"],
                    "suggestions": result.get("suggestions", []),
                },
            }
            self._save_snapshot(task.task_id, payload)
            cloud_resp = self.cloud.complete(task.task_id, task.lease_token, payload)
            return {**result, "_cloud": cloud_resp}
        except CloudError as exc:
            # 云端不可达：不 fail，租约到期后云端重新投递
            raise
        except LLMUnavailableError as exc:
            self._try_fail(
                task,
                "generating",
                True,
                "LLM_UNAVAILABLE",
                str(exc),
            )
            raise
        except Exception as exc:
            self._try_fail(task, "processing", False, "INTERNAL_ERROR", str(exc))
            raise

    def _save_snapshot(self, task_id: str, payload: dict) -> None:
        try:
            self.settings.snapshot_dir.mkdir(parents=True, exist_ok=True)
            (self.settings.snapshot_dir / f"{task_id}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _try_fail(
        self,
        task: ClaimedCsTask,
        stage: str,
        retryable: bool,
        error_code: str,
        message: str,
    ) -> None:
        try:
            self.cloud.fail(
                task.task_id,
                task.lease_token,
                stage=stage,
                retryable=retryable,
                error_code=error_code,
                message=_sanitize(message),
            )
        except Exception:
            pass


def _sanitize(message: str) -> str:
    text = str(message)
    return text[:2000]
