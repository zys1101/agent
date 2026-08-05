"""本地 Quote Worker：按 AWF-001 主流程执行单个任务。"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel

from .cache import QuoteCache
from .classification import ClassificationAgent
from .cloud import ClaimedTask, CloudClient, CloudError
from .completeness import apply_deterministic_overrides
from .config import QuoteRules
from .extraction import ExtractionAgent
from .file_parser import FileParser, ParsedFile
from .llm import LLM, LLMOutputError, LLMUnavailableError
from .models import ProjectRequirement
from .quote_engine import QuoteEngine
from .rag import CaseRecord, RagStore
from .review import ReviewAgent


class WorkerSettings(BaseModel):
    agent_id: str = "office-4070super-01"
    agent_version: str = "0.1.0"
    work_dir: Path = Path("data/incoming")
    snapshot_dir: Path = Path("data/quote_snapshots")
    sqlite_path: Path = Path("data/agent.db")
    ollama_model: str = "qwen3-vl:8b"
    rag_enabled: bool = False
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
            sqlite_path=Path(env.get("SQLITE_PATH", "data/agent.db")),
            ollama_model=env.get("OLLAMA_MODEL", cls.model_fields["ollama_model"].default),
            rag_enabled=env.get("RAG_ENABLED", "false").strip().lower() in ("1", "true", "yes"),
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
        rag: RagStore | None = None,
    ):
        self.cloud = cloud
        self.llm = llm
        self.rules = rules
        self.engine = engine
        self.settings = settings
        self.parser = parser or FileParser()
        self.cache = QuoteCache(settings.sqlite_path)
        self.rag = rag
        self.review_agent = ReviewAgent(llm, rules)

    def run_task(self, task: ClaimedTask) -> dict:
        pending = self.cache.pending_for(task.task_id)
        if pending is not None:
            # 断网期间已完成但未回传：任务重新投递后直接重传（AWF §7），不重复报价
            resp = self.cloud.complete(task.task_id, task.lease_token, pending)
            self.cache.delete_pending(task.task_id)
            self.cache.audit(task.task_id, "reuploaded", "pending result uploaded after recovery")
            pending["_cloud"] = resp
            return pending

        workdir = self.settings.work_dir / task.task_id
        workdir.mkdir(parents=True, exist_ok=True)
        result: dict | None = None
        try:
            self.cache.audit(task.task_id, "claimed", "task claimed")
            downloaded = self._download(task, workdir)
            parsed = self._parse(downloaded)
            requirement = ExtractionAgent(self.llm, self.rules).extract(task.customer_form, parsed)
            completeness = apply_deterministic_overrides(requirement, self.rules)
            classification = ClassificationAgent(self.llm, self.rules).classify(requirement, parsed)
            rag_cases = self._retrieve_cases(task.task_id, requirement, classification)
            calc = self.engine.calculate(requirement, classification)
            review = self.review_agent.review(requirement, classification, calc, rag_cases)

            result = build_result(
                task=task,
                requirement=requirement,
                classification=classification,
                calc=calc,
                settings=self.settings,
                missing_key_fields=completeness.missing_key_fields,
                review=review,
                rag_cases=rag_cases,
            )
            self._save_snapshot(task.task_id, result)
            self.cache.save_pending(task.task_id, result)
            complete_resp = self.cloud.complete(task.task_id, task.lease_token, result)
            self.cache.delete_pending(task.task_id)
            self.cache.audit(task.task_id, "completed", f"quote_id={complete_resp.get('quote_id')}")
            result["_cloud"] = complete_resp
            return result
        except LLMUnavailableError as exc:
            self._try_fail(task, "extracting_requirements", True, "LLM_UNAVAILABLE", str(exc))
            self.cache.audit(task.task_id, "failed", "LLM unavailable")
            raise
        except LLMOutputError as exc:
            self._try_fail(task, "extracting_requirements", False, "EXTRACTION_SCHEMA_INVALID", str(exc))
            self.cache.audit(task.task_id, "failed", "LLM schema invalid")
            raise
        except CloudError as exc:
            # 云端暂时不可达：不 fail，租约到期后云端重新投递；快照与 pending 已保存
            if result is not None:
                self._save_snapshot(task.task_id, result)
            self.cache.audit(task.task_id, "upload_pending", f"cloud unavailable: {sanitize_message(str(exc))}")
            raise
        except Exception as exc:
            self._try_fail(task, "processing", False, "INTERNAL_ERROR", str(exc))
            self.cache.audit(task.task_id, "failed", sanitize_message(str(exc)))
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

    def _retrieve_cases(
        self,
        task_id: str,
        requirement: ProjectRequirement,
        classification,
    ) -> list[tuple[CaseRecord, float]]:
        if self.rag is None:
            return []
        try:
            query = f"{requirement.function_description or ''} {requirement.project_type_candidate}".strip()
            cases = self.rag.search(query, project_type=classification.project_type, top_k=3)
            self.cache.audit(task_id, "retrieved_cases", f"RAG hit {len(cases)} cases")
            return cases
        except Exception as exc:
            # RAG 故障不阻塞任务
            self.cache.audit(task_id, "retrieved_cases", f"RAG unavailable: {sanitize_message(str(exc))}")
            return []

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
    review,
    rag_cases: list[tuple[CaseRecord, float]] | None = None,
) -> dict:
    price = calc.price.model_dump()
    review_reasons = list(dict.fromkeys(calc.review_reasons + list(review.extra_review_reasons)))
    manual_review_required = calc.manual_review_required or bool(review.extra_review_reasons)
    snapshot = {**calc.calculation_snapshot}
    if rag_cases:
        snapshot["rag_cases"] = [
            {
                "case_id": case.case_id,
                "project_type": case.project_type,
                "summary": case.summary,
                "score": round(score, 3),
            }
            for case, score in rag_cases
        ]
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
            "manual_review_required": manual_review_required,
            "manual_review_reasons": review_reasons,
            "missing_information": sorted(set(missing_key_fields) | set(requirement.unknowns)),
            "assumptions": requirement.assumptions,
            "exclusions": requirement.exclusions,
            "clarification_questions": requirement.clarification_questions,
            "reviewer_notes": list(review.reviewer_notes),
            "calculation_snapshot": snapshot,
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
