"""Worker AI 报价模式（原型法）端到端测试：PRICING_MODE=ai_quote。"""

import json
from pathlib import Path

from quote_agent.ai_evaluator import ImageSummary
from quote_agent.ai_quote import AiQuoteEvaluation
from quote_agent.cloud import CloudClient, MockCloudServer, MockCloudStore
from quote_agent.config import QuoteRules
from quote_agent.models import ProjectClassification, ProjectRequirement
from quote_agent.quote_engine import QuoteEngine
from quote_agent.worker import Worker, WorkerSettings


class FakeLLM:
    def __init__(self, low_completeness: bool = False):
        self.low_completeness = low_completeness
        self.last_images = None
        self.eval_images = None
        self.image_summary_images = None

    def generate_structured(self, system, user, model_cls, images=None):
        if model_cls is ProjectRequirement:
            self.last_images = images
            if self.low_completeness:
                # 关键字段大量缺失 -> 确定性完整度 < 0.60
                payload = {
                    "project_type_candidate": "simple_part",
                    "function_description": "设计一个简易支架",
                    "completeness_score": 0.5,
                }
            else:
                payload = {
                    "project_type_candidate": "simple_part",
                    "requested_deliverables": ["two_d_part_drawing"],
                    "deadline_workdays": 10.0,
                    "function_description": "设计一个简易支架",
                    "provided_materials": ["requirements.txt"],
                    "revision_policy": "limited",
                    "acceptance_criteria_known": True,
                }
            return ProjectRequirement.model_validate(
                payload
            )
        if model_cls is ProjectClassification:
            return ProjectClassification(
                project_type="simple_part",
                confidence=0.9,
                category="product_structure",
            )
        if model_cls is ImageSummary:
            self.image_summary_images = images
            return ImageSummary(summary="支架结构图：简单折弯件")
        if model_cls is AiQuoteEvaluation:
            self.eval_images = images
            return AiQuoteEvaluation(
                part_type="支架",
                project_category="产品结构设计",
                project_subtype="支架结构设计",
                deliverables=["2D零件图"],
                complexity_tier="normal",
                estimated_hours=8,
                urgency_level=0,  # 会被系统按交期覆盖为 3
                difficulty_reason=["结构简单"],
                case_summary="简易支架结构设计",
            )
        raise AssertionError(f"unexpected model_cls: {model_cls}")


def _make_worker(tmp_path, store, llm=None):
    sample = tmp_path / "requirements.txt"
    sample.write_text("设计一个简易支架。", encoding="utf-8")
    task_id = store.seed_task(
        customer_form={
            "project_name": "支架",
            "customer_description": "设计一个简易支架",
            "deadline_date": "",
            "requested_deliverables": [],
            "currency": "CNY",
        },
        files=[
            {
                "file_id": "file_01",
                "original_name": "requirements.txt",
                "mime_type": "text/plain",
                "size_bytes": sample.stat().st_size,
                "local_path": str(sample),
            }
        ],
    )
    rules = QuoteRules.load()
    settings = WorkerSettings(
        agent_id="test-agent",
        work_dir=tmp_path / "incoming",
        snapshot_dir=tmp_path / "snapshots",
        sqlite_path=tmp_path / "agent.db",
    )
    worker = Worker(
        cloud=CloudClient("http://unused", "t", "test-agent"),
        llm=llm or FakeLLM(),
        rules=rules,
        engine=QuoteEngine(rules),
        settings=settings,
    )
    return task_id, worker, settings


def test_ai_quote_default_mode_outputs_prototype_scale(tmp_path):
    """默认 ai_quote 模式：工时来自 LLM 评估（8h），价格 = 8×50 = 400，不是几百小时。"""
    store = MockCloudStore()
    server = MockCloudServer(store).start()
    try:
        task_id, worker, settings = _make_worker(tmp_path, store)
        cloud = CloudClient(server.base_url, "dev-token", "test-agent")
        worker.cloud = cloud

        task = cloud.claim()
        assert task is not None and task.task_id == task_id
        result = worker.run_task(task)

        summary = result["result"]
        assert summary["estimated_hours"]["total"] == 8
        # 交期 10 天 -> 加急 3（正常系数 1.0），8h × 50元/h = 400
        assert summary["price"] == {
            "currency": "CNY",
            "minimum": 380,
            "recommended": 400,
            "maximum": 420,
        }
        assert summary["manual_review_required"] is False
        assert summary["manual_review_reasons"] == []
        assert summary["reviewer_notes"] == []

        # 快照可审计：评估参数、相似案例、图片总结都在
        snapshot = summary["calculation_snapshot"]
        assert snapshot["engine"] == "ai_quote"
        assert snapshot["ai_analysis"]["estimated_hours"] == 8
        assert snapshot["ai_analysis"]["urgency_level"] == 3  # 系统覆盖生效
        assert "similar_cases" in snapshot
        assert snapshot["image_summary"] == "无图片"
        assert result["_cloud"]["quote_id"].startswith("quote_")

        # 快照已保存，临时目录已清理
        snapshot_file = settings.snapshot_dir / f"{task_id}.json"
        assert snapshot_file.exists()
        assert not (settings.work_dir / task_id).exists()
        assert store.task(task_id)["status"] == "completed"
    finally:
        server.stop()


def test_ai_quote_low_completeness_requires_review(tmp_path):
    """完整度低时：转预研报价（不出固定价）并强制人工审核。"""
    store = MockCloudStore()
    server = MockCloudServer(store).start()
    try:
        task_id, worker, _ = _make_worker(tmp_path, store, llm=FakeLLM(low_completeness=True))
        cloud = CloudClient(server.base_url, "dev-token", "test-agent")
        worker.cloud = cloud

        task = cloud.claim()
        result = worker.run_task(task)

        summary = result["result"]
        assert summary["price"]["recommended"] is None
        assert summary["manual_review_required"] is True
        assert "completeness_below_0_80" in summary["manual_review_reasons"]
    finally:
        server.stop()


def test_ai_quote_passes_images_to_all_steps(tmp_path):
    """有图片时：图片理解、评估都要拿到图片副本，且任务正常完成。"""
    store = MockCloudStore()
    server = MockCloudServer(store).start()
    try:
        img = tmp_path / "sketch.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n fake image content")
        task_id = store.seed_task(
            customer_form={
                "project_name": "图片任务",
                "customer_description": "设计一个支架",
                "deadline_date": "",
                "requested_deliverables": [],
                "currency": "CNY",
            },
            files=[
                {
                    "file_id": "file_02",
                    "original_name": "sketch.png",
                    "mime_type": "image/png",
                    "size_bytes": img.stat().st_size,
                    "local_path": str(img),
                }
            ],
        )
        fake_llm = FakeLLM()
        rules = QuoteRules.load()
        settings = WorkerSettings(
            agent_id="test-agent",
            work_dir=tmp_path / "incoming",
            snapshot_dir=tmp_path / "snapshots",
            sqlite_path=tmp_path / "agent.db",
        )
        worker = Worker(
            cloud=CloudClient(server.base_url, "t", "test-agent"),
            llm=fake_llm,
            rules=rules,
            engine=QuoteEngine(rules),
            settings=settings,
        )
        task = worker.cloud.claim()
        assert task is not None and task.task_id == task_id
        result = worker.run_task(task)

        assert result["result"]["estimated_hours"]["total"] == 8
        assert fake_llm.image_summary_images and len(fake_llm.image_summary_images) == 1
        assert fake_llm.eval_images and len(fake_llm.eval_images) == 1
        passed = Path(fake_llm.eval_images[0])
        assert passed.name == "file_02_sketch.png"
        assert settings.work_dir in passed.parents
        assert not (settings.work_dir / task_id).exists()
    finally:
        server.stop()


def test_pricing_mode_env_and_default():
    assert WorkerSettings().pricing_mode == "ai_quote"
    assert WorkerSettings.from_env({"PRICING_MODE": "qrs"}).pricing_mode == "qrs"
    assert WorkerSettings.from_env({"PRICING_MODE": "AI_QUOTE"}).pricing_mode == "ai_quote"
    assert WorkerSettings.from_env({}).pricing_mode == "ai_quote"
