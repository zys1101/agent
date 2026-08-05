"""Worker 端到端测试：mock 云端 + FakeLLM + 规则引擎 + 快照 + 清理。"""

import json
from pathlib import Path

from quote_agent.cloud import CloudClient, MockCloudServer, MockCloudStore
from quote_agent.config import QuoteRules
from quote_agent.extraction import ImageTranscript, ImageTranscripts
from quote_agent.models import ProjectClassification, ProjectRequirement, ReviewLLMOutput
from quote_agent.quote_engine import QuoteEngine
from quote_agent.worker import Worker, WorkerSettings


class FakeLLM:
    def __init__(self):
        self.last_images = None

    def generate_structured(self, system, user, model_cls, images=None):
        if model_cls is ProjectRequirement:
            self.last_images = images
            return ProjectRequirement.model_validate(
                {
                    "project_type_candidate": "pneumatic_press_fixture",
                    "requested_deliverables": ["three_d_assembly", "bom"],
                    "deadline_workdays": 10.0,
                    "function_description": "电机壳体压装工装",
                    "provided_materials": ["requirements_sample.txt"],
                    "revision_policy": "limited",
                    "unknowns": ["maximum_press_force"],
                    "risks": ["missing_installation_interface"],
                    "completeness_score": 0.5,
                    "part_count": 20,
                    "assembly_complexity": "medium",
                    "precision": "0.05",
                    "professional_scope": ["pneumatic"],
                    "missing_critical_interface": True,
                    "missing_load_or_force": True,
                    "workpiece_info_known": True,
                    "cycle_time_known": True,
                    "assumptions": ["压装力待确认"],
                    "exclusions": ["不含电控"],
                    "clarification_questions": ["请确认最大压装力"],
                    "scope_uncertain": False,
                }
            )
        if model_cls is ProjectClassification:
            return ProjectClassification.model_validate(
                {
                    "project_type": "pneumatic_press_fixture",
                    "confidence": 0.82,
                    "candidates": [
                        {"project_type": "pneumatic_press_fixture", "confidence": 0.82},
                        {"project_type": "assembly_fixture", "confidence": 0.12},
                    ],
                }
            )
        if model_cls is ImageTranscripts:
            return ImageTranscripts(
                files=[ImageTranscript(original_name="sketch.png", summary="工件信息", key_facts=["200x150mm"])]
            )
        if model_cls is ReviewLLMOutput:
            return ReviewLLMOutput(
                reviewer_notes=["压装力缺失需先向客户澄清"],
                extra_review_reasons=["missing_load_or_force"],
            )
        raise AssertionError(f"unexpected model_cls: {model_cls}")


def _make_worker(tmp_path, store):
    sample = tmp_path / "requirements_sample.txt"
    sample.write_text("设计一套用于电机壳体压装的工装。", encoding="utf-8")
    task_id = store.seed_task(
        customer_form={"project_name": "压装工装", "customer_description": "见附件", "deadline_date": "", "requested_deliverables": [], "currency": "CNY"},
        files=[
            {
                "file_id": "file_01",
                "original_name": "requirements_sample.txt",
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
        llm=FakeLLM(),
        rules=rules,
        engine=QuoteEngine(rules),
        settings=settings,
    )
    return task_id, worker, settings


def test_worker_end_to_end(tmp_path):
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
        assert summary["project_type"] == "pneumatic_press_fixture"
        assert summary["completeness_score"] == 0.6286
        assert summary["manual_review_required"] is True
        assert "completeness_below_0_80" in summary["manual_review_reasons"]
        assert "maximum_press_force" in summary["missing_information"]
        assert "interface" in summary["missing_information"]
        assert summary["price"]["recommended"] is not None
        assert summary["reviewer_notes"] == ["压装力缺失需先向客户澄清"]
        assert "missing_load_or_force" in summary["manual_review_reasons"]
        assert result["_cloud"]["quote_id"].startswith("quote_")

        # 快照已保存，原始目录已清理
        snapshot = settings.snapshot_dir / f"{task_id}.json"
        assert snapshot.exists()
        assert json.loads(snapshot.read_text(encoding="utf-8"))["result"]["project_type"] == "pneumatic_press_fixture"
        assert not (settings.work_dir / task_id).exists()
        assert store.task(task_id)["status"] == "completed"
    finally:
        server.stop()


def test_complete_idempotency(tmp_path):
    store = MockCloudStore()
    server = MockCloudServer(store).start()
    try:
        task_id, worker, _ = _make_worker(tmp_path, store)
        cloud = CloudClient(server.base_url, "dev-token", "test-agent")
        worker.cloud = cloud
        task = cloud.claim()
        result = worker.run_task(task)
        quote_id = result["_cloud"]["quote_id"]

        # 用同一 Idempotency-Key 重复 complete 应返回同一 quote_id
        again = cloud.complete(task_id, task.lease_token, result)
        assert again["quote_id"] == quote_id
    finally:
        server.stop()


def test_worker_fail_marks_task_failed(tmp_path):
    class BrokenLLM(FakeLLM):
        def generate_structured(self, system, user, model_cls, images=None):
            raise ValueError("boom")

    store = MockCloudStore()
    server = MockCloudServer(store).start()
    try:
        task_id, worker, settings = _make_worker(tmp_path, store)
        worker.llm = BrokenLLM()
        cloud = CloudClient(server.base_url, "dev-token", "test-agent")
        worker.cloud = cloud
        task = cloud.claim()
        try:
            worker.run_task(task)
            raise AssertionError("expected exception")
        except ValueError:
            pass
        assert store.task(task_id)["status"] == "failed"
        assert store.task(task_id)["error"]["error_code"] == "INTERNAL_ERROR"
        assert not (settings.work_dir / task_id).exists()
    finally:
        server.stop()


def test_worker_passes_images_to_extraction(tmp_path):
    store = MockCloudStore()
    server = MockCloudServer(store).start()
    try:
        img = tmp_path / "sketch.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n fake image content")
        task_id = store.seed_task(
            customer_form={"project_name": "图片任务", "customer_description": "", "deadline_date": "", "requested_deliverables": [], "currency": "CNY"},
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
        rules = QuoteRules.load()
        settings = WorkerSettings(
            agent_id="test-agent",
            work_dir=tmp_path / "incoming",
            snapshot_dir=tmp_path / "snapshots",
            sqlite_path=tmp_path / "agent.db",
        )
        fake_llm = FakeLLM()
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
        assert result["result"]["project_type"] == "pneumatic_press_fixture"
        assert fake_llm.last_images and len(fake_llm.last_images) == 1
        passed_image = Path(fake_llm.last_images[0])
        assert passed_image.name == "file_02_sketch.png"
        assert settings.work_dir in passed_image.parents  # 传的是任务临时目录内的下载副本
        assert not (settings.work_dir / task_id).exists()
    finally:
        server.stop()
