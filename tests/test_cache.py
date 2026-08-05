"""SQLite 离线缓存与回传恢复测试。"""

import pytest

from quote_agent.cache import QuoteCache
from quote_agent.cloud import ClaimedTask, CloudError, TaskFile
from quote_agent.config import QuoteRules
from quote_agent.models import ProjectClassification, ProjectRequirement, ReviewLLMOutput
from quote_agent.quote_engine import QuoteEngine
from quote_agent.worker import Worker, WorkerSettings


def test_pending_roundtrip_and_upsert(tmp_path):
    cache = QuoteCache(tmp_path / "agent.db")
    cache.save_pending("t1", {"a": 1})
    assert cache.pending_for("t1") == {"a": 1}
    cache.save_pending("t1", {"a": 2})  # 幂等覆盖
    assert cache.pending_for("t1") == {"a": 2}
    cache.delete_pending("t1")
    assert cache.pending_for("t1") is None
    assert cache.count_pending() == 0


def test_audit_log(tmp_path):
    cache = QuoteCache(tmp_path / "agent.db")
    cache.audit("t1", "claimed", "task claimed")
    cache.audit(None, "startup", "worker started")
    rows = cache.recent_audit()
    assert len(rows) == 2
    assert rows[0]["stage"] == "startup"  # 倒序


class SimpleLLM:
    def generate_structured(self, system, user, model_cls, images=None):
        if model_cls is ProjectRequirement:
            return ProjectRequirement(
                project_type_candidate="simple_part",
                requested_deliverables=["two_d_part_drawing"],
                deadline_workdays=5.0,
                function_description="支架",
                provided_materials=["req.txt"],
                revision_policy="limited",
                acceptance_criteria_known=True,
            )
        if model_cls is ProjectClassification:
            return ProjectClassification(project_type="simple_part", confidence=0.9)
        if model_cls is ReviewLLMOutput:
            return ReviewLLMOutput(reviewer_notes=["测试意见"])
        raise AssertionError(f"unexpected model_cls: {model_cls}")


class FlakyCloud:
    def __init__(self):
        self.fail_next = True
        self.completed = []
        self.fail_calls = []

    def claim(self):
        return None

    def heartbeat(self, *args, **kwargs):
        pass

    def complete(self, task_id, lease_token, result):
        if self.fail_next:
            self.fail_next = False
            raise CloudError("network down", retryable=True)
        self.completed.append(result)
        return {"task_id": task_id, "quote_id": "quote_x"}

    def fail(self, *args, **kwargs):
        self.fail_calls.append(args)


def _task(tmp_path) -> tuple[ClaimedTask, WorkerSettings]:
    sample = tmp_path / "req.txt"
    sample.write_text("设计一个简单支架，出2D工程图。", encoding="utf-8")
    task = ClaimedTask(
        task_id="qt_recover_test",
        project_id="prj_test",
        lease_token="lease-1",
        lease_expires_at="2099-01-01T00:00:00+08:00",
        customer_form={"project_name": "支架", "customer_description": "简单支架", "deadline_date": "", "requested_deliverables": [], "currency": "CNY"},
        files=[TaskFile(file_id="file_01", original_name="req.txt", mime_type="text/plain", local_path=str(sample))],
    )
    settings = WorkerSettings(
        agent_id="test-agent",
        work_dir=tmp_path / "incoming",
        snapshot_dir=tmp_path / "snapshots",
        sqlite_path=tmp_path / "agent.db",
    )
    return task, settings


def test_worker_offline_keeps_pending_then_recovers(tmp_path):
    rules = QuoteRules.load()
    task, settings = _task(tmp_path)
    cloud = FlakyCloud()
    worker = Worker(cloud, SimpleLLM(), rules, QuoteEngine(rules), settings)

    # 第一次：云端 complete 失败 -> pending 保留，快照已存
    with pytest.raises(CloudError):
        worker.run_task(task)
    assert worker.cache.pending_for(task.task_id) is not None
    assert (settings.snapshot_dir / f"{task.task_id}.json").exists()

    # 任务重新投递后：直接重传 pending，不重新处理
    result = worker.run_task(task)
    assert worker.cache.pending_for(task.task_id) is None
    assert len(cloud.completed) == 1
    assert result["_cloud"]["quote_id"] == "quote_x"
    assert result["result"]["project_type"] == "simple_part"


def test_worker_reupload_does_not_reprocess(tmp_path):
    """恢复路径不应重复计算报价：直接验证 pending 分支只调用 complete。"""
    rules = QuoteRules.load()
    task, settings = _task(tmp_path)
    cloud = FlakyCloud()
    worker = Worker(cloud, SimpleLLM(), rules, QuoteEngine(rules), settings)

    worker.cache.save_pending(task.task_id, {"result": {"project_type": "cached"}})
    cloud.fail_next = False  # 重传路径只验证 complete
    result = worker.run_task(task)
    assert result["result"]["project_type"] == "cached"
    assert worker.cache.pending_for(task.task_id) is None
    assert len(cloud.completed) == 1
