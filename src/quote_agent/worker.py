"""本地 Quote Worker：按 AWF-001 主流程执行单个任务。"""

from __future__ import annotations

import json
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel

from .ai_evaluator import AiQuoteEvaluator
from .ai_quote import AiQuoteEvaluation, AiQuotePricing, AiQuoteRules
from .cache import QuoteCache
from .classification import ClassificationAgent
from .cloud import ClaimedTask, CloudClient, CloudError
from .completeness import apply_deterministic_overrides
from .config import QuoteRules
from .extraction import ExtractionAgent
from .file_parser import FileParser, ParsedFile
from .llm import CachedLLM, LLM, LLMOutputError, LLMUnavailableError
from .models import ProjectRequirement, ReviewLLMOutput
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
    llm_cache_enabled: bool = True
    # ai_quote = AI 报价方法（原型：LLM 估工时，代码按 50元/h 算价）；
    # qrs = QRS 规则模板引擎（保留，供对比/回退）
    pricing_mode: str = "ai_quote"
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
            llm_cache_enabled=env.get("LLM_CACHE_ENABLED", "true").strip().lower() in ("1", "true", "yes"),
            pricing_mode=env.get("PRICING_MODE", "ai_quote").strip().lower(),
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

    def run_task(self, task: ClaimedTask) -> dict:
        pending = self.cache.pending_for(task.task_id)
        if pending is not None:
            # 断网期间已完成但未回传：任务重新投递后直接重传（AWF §7），不重复报价
            try:
                resp = self.cloud.complete(task.task_id, task.lease_token, pending)
            except CloudError as exc:
                if exc.status == 422 or "SCHEMA" in str(exc).upper():
                    # 旧 pending 结构被云端拒绝（如 classification_confidence=null）：作废后重新报价
                    self.cache.delete_pending(task.task_id)
                    self.cache.audit(
                        task.task_id,
                        "pending_discarded",
                        f"payload schema invalid, reprocessing: {sanitize_message(str(exc))}",
                    )
                else:
                    raise
            else:
                self.cache.delete_pending(task.task_id)
                self.cache.audit(task.task_id, "reuploaded", "pending result uploaded after recovery")
                pending["_cloud"] = resp
                return pending

        workdir = self.settings.work_dir / task.task_id
        workdir.mkdir(parents=True, exist_ok=True)
        result: dict | None = None
        llm = CachedLLM(self.llm, self.cache) if self.settings.llm_cache_enabled else self.llm
        try:
            self.cache.audit(task.task_id, "claimed", "task claimed")
            t_parse = time.perf_counter()
            downloaded = self._download(task, workdir)
            parsed = self._parse(downloaded)
            self.cache.audit(task.task_id, "parsed", f"files={len(parsed)} in {time.perf_counter() - t_parse:.2f}s")
            if self.settings.pricing_mode == "ai_quote":
                result = self._run_ai_quote_pipeline(task, llm, parsed)
            else:
                result = self._run_qrs_pipeline(task, llm, parsed)
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
        def fetch(file) -> tuple[Any, Path]:
            target = workdir / f"{file.file_id}_{sanitize_name(file.original_name)}"
            if file.local_path:
                shutil.copyfile(file.local_path, target)
            elif file.download_url:
                # 跟随 http->https 等重定向：云端可能下发 http 下载 URL 并 301 到 https
                resp = httpx.get(file.download_url, timeout=120, follow_redirects=True)
                resp.raise_for_status()
                target.write_bytes(resp.content)
            else:
                raise ValueError(f"file {file.file_id} has neither local_path nor download_url")
            return (file, target)

        workers = min(4, max(len(task.files), 1))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(fetch, task.files))

    def _parse(self, downloaded: list[tuple[Any, Path]]) -> list[ParsedFile]:
        def do(item) -> ParsedFile:
            file, path = item
            return self.parser.parse(
                path,
                file_id=file.file_id,
                original_name=file.original_name,
                mime_type=file.mime_type,
            )

        workers = min(4, max(len(downloaded), 1))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(do, downloaded))

    def _classify_and_retrieve(
        self,
        task_id: str,
        llm: LLM,
        requirement: ProjectRequirement,
        parsed: list[ParsedFile],
    ) -> tuple[object, list]:
        """分类 LLM 与 RAG 检索并行执行（B 项提速）。"""
        t0 = time.perf_counter()
        classifier = ClassificationAgent(llm, self.rules)
        if self.rag is None:
            classification = classifier.classify(requirement, parsed)
            self.cache.audit(task_id, "classified", f"in {time.perf_counter() - t0:.2f}s")
            return classification, []
        with ThreadPoolExecutor(max_workers=2) as pool:
            classify_future = pool.submit(classifier.classify, requirement, parsed)
            retrieve_future = pool.submit(self._retrieve_cases, task_id, requirement, None)
            classification = classify_future.result()
            rag_cases = retrieve_future.result()
        self.cache.audit(task_id, "classified", f"in {time.perf_counter() - t0:.2f}s（分类与RAG检索并行）")
        return classification, rag_cases

    # ---- AI 报价方法（原型：AI 定性，代码定量）----

    def _run_ai_quote_pipeline(self, task: ClaimedTask, llm: LLM, parsed: list[ParsedFile]) -> dict:
        ai_rules = AiQuoteRules.load()

        # 1. 提取结构化需求：只用于完整度、澄清问题与交期；价格不经过它
        t_extract = time.perf_counter()
        requirement = ExtractionAgent(llm, self.rules).extract(task.customer_form, parsed)
        self.cache.audit(task.task_id, "extracted", f"in {time.perf_counter() - t_extract:.2f}s")
        completeness = apply_deterministic_overrides(requirement, self.rules)

        # 2. 分类 + RAG 检索相似案例（并行）
        classification, rag_cases = self._classify_and_retrieve(task.task_id, llm, requirement, parsed)
        similar_cases = _format_similar_cases(rag_cases)

        # 3. 图片理解（原型步骤 1：视觉模型总结图片）
        image_paths = [pf.image_path for pf in parsed if pf.image_path and not pf.errors]
        requirement_text = _requirement_text(task.customer_form, requirement)
        evaluator = AiQuoteEvaluator(llm, ai_rules)
        image_summary = "无图片"
        if image_paths:
            t_img = time.perf_counter()
            image_summary = evaluator.summarize_images(requirement_text, image_paths)
            self.cache.audit(task.task_id, "image_summarized", f"in {time.perf_counter() - t_img:.2f}s")

        # 4. 综合评估（原型步骤 4：参考价格表 + 需求 + 图片总结 + 相似案例）
        delivery_days = requirement.deadline_workdays
        if delivery_days is None:
            delivery_days = _days_until_deadline(task.customer_form.get("deadline_date"))
        t_eval = time.perf_counter()
        evaluation = evaluator.evaluate(
            requirement_text=requirement_text,
            image_summary=image_summary,
            similar_cases=similar_cases,
            delivery_days=delivery_days,
            is_urgent=False,
            images=image_paths or None,
        )
        self.cache.audit(task.task_id, "evaluated", f"in {time.perf_counter() - t_eval:.2f}s")

        # 5. 确定性算价（唯一计算价格的地方）
        t_calc = time.perf_counter()
        pricing = AiQuotePricing(ai_rules).calculate(
            evaluation,
            completeness_score=requirement.completeness_score,
        )
        self.cache.audit(task.task_id, "calculated", f"in {time.perf_counter() - t_calc:.3f}s")

        project_type = classification.project_type or evaluation.project_category
        category = classification.category or self.rules.category_of(project_type)
        return build_ai_quote_result(
            task=task,
            requirement=requirement,
            classification=classification,
            evaluation=evaluation,
            pricing=pricing,
            settings=self.settings,
            missing_key_fields=completeness.missing_key_fields,
            image_summary=image_summary,
            similar_cases=similar_cases,
            project_type=project_type,
            category=category,
            category_name=self.rules.category_name_of(category),
        )

    # ---- QRS 规则模板引擎（PRICING_MODE=qrs，保留）----

    def _run_qrs_pipeline(self, task: ClaimedTask, llm: LLM, parsed: list[ParsedFile]) -> dict:
        t_extract = time.perf_counter()
        requirement = ExtractionAgent(llm, self.rules).extract(task.customer_form, parsed)
        self.cache.audit(task.task_id, "extracted", f"in {time.perf_counter() - t_extract:.2f}s")
        completeness = apply_deterministic_overrides(requirement, self.rules)
        classification, rag_cases = self._classify_and_retrieve(task.task_id, llm, requirement, parsed)
        t_calc = time.perf_counter()
        calc = self.engine.calculate(requirement, classification)
        self.cache.audit(task.task_id, "calculated", f"in {time.perf_counter() - t_calc:.3f}s")
        if calc.manual_review_required:
            t_review = time.perf_counter()
            review = ReviewAgent(llm, self.rules).review(requirement, classification, calc, rag_cases)
            self.cache.audit(task.task_id, "reviewed", f"in {time.perf_counter() - t_review:.2f}s")
        else:
            # 引擎判定无需人工审核时跳过 LLM 审核，减少一次慢调用（提速）
            review = ReviewLLMOutput(reviewer_notes=[], extra_review_reasons=[])
            self.cache.audit(task.task_id, "reviewed", "skipped (no manual review required)")
        return build_result(
            task=task,
            requirement=requirement,
            classification=classification,
            calc=calc,
            settings=self.settings,
            missing_key_fields=completeness.missing_key_fields,
            review=review,
            rag_cases=rag_cases,
        )

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
        classification=None,
    ) -> list[tuple[CaseRecord, float]]:
        if self.rag is None:
            return []
        project_type = (
            classification.project_type
            if classification is not None
            else requirement.project_type_candidate
        )
        category = (
            classification.category
            if classification is not None
            else self.rules.category_of(project_type)
        )
        try:
            query = f"{requirement.function_description or ''} {project_type}".strip()
            cases = self.rag.search(
                query,
                project_type=project_type,
                category=category,
                top_k=3,
            )
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
                "category": case.category,
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
            "category": calc.category,
            "category_name": calc.category_name,
            "completeness_score": requirement.completeness_score,
            "classification_confidence": float(
                classification.confidence if classification.confidence is not None else 0.5
            ),
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


def build_ai_quote_result(
    task: ClaimedTask,
    requirement: ProjectRequirement,
    classification,
    evaluation: AiQuoteEvaluation,
    pricing,
    settings: WorkerSettings,
    missing_key_fields: list[str],
    image_summary: str,
    similar_cases: list[dict],
    project_type: str,
    category: str,
    category_name: str,
) -> dict:
    """AI 报价方法结果：评估参数与价格全部来自 ai_quote 模块，可审计复现。"""
    snapshot = {**pricing.calculation_snapshot}
    snapshot["similar_cases"] = similar_cases
    snapshot["image_summary"] = image_summary
    snapshot["ai_analysis"] = evaluation.model_dump(mode="json")
    return {
        "result_schema_version": "1.0",
        "agent": {"agent_id": settings.agent_id, "agent_version": settings.agent_version},
        "runtime": {
            "ollama_model": settings.ollama_model,
            "rule_set_version": pricing.rule_set_version,
            "prompt_versions": settings.prompt_versions,
        },
        "result": {
            "project_type": project_type,
            "category": category,
            "category_name": category_name,
            "completeness_score": requirement.completeness_score,
            "classification_confidence": float(
                classification.confidence if classification.confidence is not None else 0.5
            ),
            "estimated_hours": {"total": evaluation.estimated_hours},
            "price": pricing.price,
            "manual_review_required": pricing.manual_review_required,
            "manual_review_reasons": list(pricing.review_reasons),
            "missing_information": sorted(set(missing_key_fields) | set(requirement.unknowns)),
            "assumptions": requirement.assumptions,
            "exclusions": requirement.exclusions,
            "clarification_questions": requirement.clarification_questions,
            "reviewer_notes": [],
            "calculation_snapshot": snapshot,
        },
    }


def _requirement_text(form: dict, requirement: ProjectRequirement) -> str:
    """拼装喂给 AI 评估提示词的需求文本（原型 main.py 的 requirement_text）。"""
    parts = []
    if form.get("project_name"):
        parts.append(f"项目名称：{form['project_name']}")
    if form.get("customer_description"):
        parts.append(f"客户需求：{form['customer_description']}")
    if requirement.function_description:
        parts.append(f"功能描述：{requirement.function_description}")
    if requirement.provided_materials:
        parts.append("提供资料：" + "、".join(requirement.provided_materials))
    return "\n".join(parts) or "（无文字需求）"


def _format_similar_cases(rag_cases: list[tuple[CaseRecord, float]]) -> list[dict]:
    """把 RAG 脱敏案例转成 AI 评估提示词里的相似案例 JSON。"""
    out = []
    for case, score in rag_cases:
        out.append(
            {
                "case_id": case.case_id,
                "project_type": case.project_type,
                "category": case.category,
                "case_summary": case.summary,
                "deliverables": case.deliverables,
                "typical_hours": case.typical_hours,
                "price_range_cny": list(case.price_range_cny) if case.price_range_cny else None,
                "similarity_score": round(score, 4),
            }
        )
    return out


def _days_until_deadline(date_str: str | None) -> float | None:
    """从交付日期字符串估算剩余天数（用于加急等级判定；无法解析返回 None）。"""
    if not date_str:
        return None
    try:
        deadline = date.fromisoformat(str(date_str).strip()[:10])
        return float(max((deadline - date.today()).days, 0))
    except ValueError:
        return None


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
