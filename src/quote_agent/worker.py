"""本地 Quote Worker：按 AWF-001 主流程执行单个任务。"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel

from .classification import ClassificationAgent
from .cloud import ClaimedTask, CloudClient, CloudError
from .completeness import apply_deterministic_overrides
from .config import QuoteRules
from .extraction import ExtractionAgent
from .file_parser import FileParser, ParsedFile
from .llm import LLM, LLMOutputError, LLMUnavailableError
from .models import ProjectRequirement
from .quote_engine import QuoteEngine


class WorkerSettings(BaseModel):
    agent_id: str = "office-4070super-01"
    agent_version: str = "0.1.0"
    work_dir: Path = Path("data/incoming")
    snapshot_dir: Path = Path("data/quote_snapshots")
    ollama_model: str = "qwen3-vl:8b"
    prompt_versions: dict[str, str] = {
        "requirement_extraction": "v1",
        "classification": "v1",
        "review": "v1",
    }

    @classmethod
    def from_env(cls, env: dict | None = None) -> "WorkerSettings":
        env = env or os.environ
        work_dir = Path(env.get("WORK_DIR", "data/incoming"))
        return cls(
            agent_id=env.get("AGENT_ID", cls.model_fields["agent_id"].default),
            agent_version=env.get("AGENT_VERSION", cls.model_fields["agent_version"].default),
            work_dir=work_dir,
            snapshot_dir=Path(env.get("SNAPSHOT_DIR", work_dir.parent / "quote_snapshots")),
            ollama_model=env.get("OLLAMA_MODEL", cls.model_fields["ollama_model"].default),
        )


class Worker:
    def __init__(
        self,
        cloud: CloudClient,
        llm: LLM,
        rules: QuoteRules,
        engine: QuoteEngine,
        settings: WorkerSettings,
        parser: FileParser | None = None,
    ):
        self.cloud = cloud
        self.llm = llm
        self.rules = rules
        self.engine = engine
        self.settings = settings
        self.parser = parser or FileParser()

    def run_task(self, task: ClaimedTask) -> dict:
        workdir = self.settings.work_dir / task.task_id
        workdir.mkdir(parents=True, exist_ok=True)
        result: dict | None = None
        try:
            downloaded = self._download(task, workdir)
            parsed = self._parse(downloaded)
            requirement = ExtractionAgent(self.llm, self.rules).extract(task.customer_form, parsed)
            completeness = apply_deterministic_overrides(requirement, self.rules)
            classification = ClassificationAgent(self.llm, self.rules).classify(requirement, parsed)
            calc = self.engine.calculate(requirement, classification)

            result = build_result(
                task=task,
                requirement=requirement,
                classification=classification,
                calc=calc,
                settings=self.settings,
                missing_key_fields=completeness.missing_key_fields,
            )
            self._save_snapshot(task.task_id, result)
            complete_resp = self.cloud.complete(task.task_id, task.lease_token, result)
            result["_cloud"] = complete_resp
            return result
        except LLMUnavailableError as exc:
            self._try_fail(task, "extracting_requirements", True, "LLM_UNAVAILABLE", str(exc))
            raise
        except LLMOutputError as exc:
            self._try_fail(task, "extracting_requirements", False, "EXTRACTION_SCHEMA_INVALID", str(exc))
            raise
        except CloudError as exc:
            # 云端暂时不可达：不 fail，租约到期后由云端重新投递；本地快照已保存
            if result is not None:
                self._save_snapshot(task.task_id, result)
            raise
        except Exception as exc:
            self._try_fail(task, "processing", False, "INTERNAL_ERROR", str(exc))
            raise
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    # ---- 内部 ----

    def _download(self, task: ClaimedTask, workdir: Path) -> list[tuple[Any, Path]]:
        paths = []
        for file in task.files:
            target = workdir / f"{file.file_id}_{sanitize_name(file.original_name)}"
            if file.local_path:
                shutil.copyfile(file.local_path, target)
            elif file.download_url:
                resp = httpx.get(file.download_url, timeout=120)
                resp.raise_for_status()
                target.write_bytes(resp.content)
            else:
                raise ValueError(f"file {file.file_id} has neither local_path nor download_url")
            paths.append((file, target))
        return paths

    def _parse(self, downloaded: list[tuple[Any, Path]]) -> list[ParsedFile]:
        parsed = []
        for file, path in downloaded:
            parsed.append(
                self.parser.parse(
                    path,
                    file_id=file.file_id,
                    original_name=file.original_name,
                    mime_type=file.mime_type,
                )
            )
        return parsed

    def _save_snapshot(self, task_id: str, result: dict) -> None:
        self.settings.snapshot_dir.mkdir(parents=True, exist_ok=True)
        (self.settings.snapshot_dir / f"{task_id}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _try_fail(self, task: ClaimedTask, stage: str, retryable: bool, error_code: str, message: str) -> None:
        try:
            self.cloud.fail(
                task.task_id,
                task.lease_token,
                stage=stage,
                retryable=retryable,
                error_code=error_code,
                message=sanitize_message(message),
            )
        except Exception:
            pass


def build_result(
    task: ClaimedTask,
    requirement: ProjectRequirement,
    classification,
    calc,
    settings: WorkerSettings,
    missing_key_fields: list[str],
) -> dict:
    price = calc.price.model_dump()
    return {
        "result_schema_version": "1.0",
        "agent": {"agent_id": settings.agent_id, "agent_version": settings.agent_version},
        "runtime": {
            "ollama_model": settings.ollama_model,
            "rule_set_version": calc.rule_set_version,
            "prompt_versions": settings.prompt_versions,
        },
        "result": {
            "project_type": calc.project_type,
            "completeness_score": requirement.completeness_score,
            "classification_confidence": classification.confidence,
            "estimated_hours": {"total": calc.estimated_hours.total},
            "price": price,
            "manual_review_required": calc.manual_review_required,
            "manual_review_reasons": calc.review_reasons,
            "missing_information": sorted(set(missing_key_fields) | set(requirement.unknowns)),
            "assumptions": requirement.assumptions,
            "exclusions": requirement.exclusions,
            "clarification_questions": requirement.clarification_questions,
            "calculation_snapshot": calc.calculation_snapshot,
        },
    }


def sanitize_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in name)


def sanitize_message(message: str) -> str:
    """避免把 Token/签名 URL 原样回传（AWF §7）。"""
    text = str(message)
    for token in (os.environ.get("AGENT_API_TOKEN", ""),):
        if token and token in text:
            text = text.replace(token, "[REDACTED]")
    return text[:2000]


def _guess_mime(path: Path) -> str:
    return {
        ".pdf": "application/pdf",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xls": "application/vnd.ms-excel",
        ".txt": "text/plain",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
    }.get(path.suffix.lower(), "application/octet-stream")
