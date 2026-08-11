"""SQLite 离线缓存与回传恢复测试。"""

import pytest

from quote_agent.cache import QuoteCache
from quote_agent.cloud import ClaimedTask, CloudError, TaskFile
from quote_agent.config import QuoteRules
from quote_agent.models import ProjectClassification, ProjectRequirement, ReviewLLMOutput
from quote_agent.quote_engine import QuoteEngine
from quote_agent.worker import Worker, WorkerSettings, build_result


class FakeRag:
    def __init__(self):
        self.calls = []

    def search(self, query, project_type=None, category=None, top_k=3):
        self.calls.append((project_type, category, query))
        return []


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


class SchemaRejectCloud:
    """云端拒绝 classification_confidence 为 null 的 payload（模拟 RESULT_SCHEMA_INVALID 422）。"""

    def __init__(self):
        self.completed = []

    def claim(self):
        return None

    def heartbeat(self, *args, **kwargs):
        pass

    def complete(self, task_id, lease_token, result):
        confidence = result.get("result", {}).get("classification_confidence")
        if confidence is None or not isinstance(confidence, (int, float)):
            raise CloudError("cloud error 422: RESULT_SCHEMA_INVALID", retryable=False, status=422)
        self.completed.append(result)
        return {"task_id": task_id, "quote_id": "quote_ok"}

    def fail(self, *args, **kwargs):
        pass


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
        pricing_mode="qrs",
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


def test_retrieve_cases_falls_back_to_candidate(tmp_path):
    rules = QuoteRules.load()
    task, settings = _task(tmp_path)
    rag = FakeRag()
    worker = Worker(None, SimpleLLM(), rules, QuoteEngine(rules), settings, rag=rag)

    requirement = ProjectRequirement(
        project_type_candidate="pneumatic_press_fixture",
        completeness_score=0.5,
        function_description="轴承压装工装",
    )
    # 并行路径：还没出分类结果，先按候选类型 + 规则归属的大类检索
    worker._retrieve_cases(task.task_id, requirement, None)
    assert rag.calls == [("pneumatic_press_fixture", "tooling_fixture", "轴承压装工装 pneumatic_press_fixture")]

    # 分类结果可用时，按分类结果检索
    classification = ProjectClassification(project_type="assembly_fixture", confidence=0.9, category="test_fixture")
    worker._retrieve_cases(task.task_id, requirement, classification)
    assert rag.calls[-1] == ("assembly_fixture", "test_fixture", "轴承压装工装 assembly_fixture")


def test_classify_and_retrieve_parallel_path(tmp_path):
    rules = QuoteRules.load()
    task, settings = _task(tmp_path)
    rag = FakeRag()
    worker = Worker(None, SimpleLLM(), rules, QuoteEngine(rules), settings, rag=rag)

    requirement = ProjectRequirement(
        project_type_candidate="simple_part",
        completeness_score=0.9,
        function_description="简单支架",
    )
    classification, cases = worker._classify_and_retrieve(task.task_id, worker.llm, requirement, [])
    assert classification.project_type == "simple_part"
    assert classification.category == "product_structure"  # 归属推导
    assert cases == []
    assert len(rag.calls) == 1


def test_review_skipped_when_engine_does_not_require(tmp_path):
    """引擎未判定人工审核时不再调用 LLM 审核（SimpleLLM 被调用会抛错）。"""
    rules = QuoteRules.load()
    task, settings = _task(tmp_path)
    cloud = FlakyCloud()
    cloud.fail_next = False
    worker = Worker(cloud, SimpleLLM(), rules, QuoteEngine(rules), settings)

    result = worker.run_task(task)
    assert result["result"]["reviewer_notes"] == []
    assert result["result"]["manual_review_required"] is False
    assert len(cloud.completed) == 1


def test_result_confidence_is_number_when_classification_omits(tmp_path):
    """分类模型缺失 confidence 时，回传结果里的 classification_confidence 必须仍是数字。"""
    rules = QuoteRules.load()
    engine = QuoteEngine(rules)
    requirement = ProjectRequirement(
        project_type_candidate="simple_part",
        requested_deliverables=["two_d_part_drawing"],
        completeness_score=0.95,
    )
    classification = ProjectClassification(project_type="simple_part", confidence=None)
    calc = engine.calculate(requirement, classification)
    task, settings = _task(tmp_path)
    result = build_result(
        task=task,
        requirement=requirement,
        classification=classification,
        calc=calc,
        settings=settings,
        missing_key_fields=[],
        review=ReviewLLMOutput(),
        rag_cases=None,
    )
    confidence = result["result"]["classification_confidence"]
    assert isinstance(confidence, float)
    assert confidence == 0.5


def test_pending_schema_invalid_is_discarded_and_reprocessed(tmp_path):
    """旧 pending 被云端以 422 拒绝时，应作废并重新报价，而不是循环重传坏数据。"""
    rules = QuoteRules.load()
    task, settings = _task(tmp_path)
    cloud = SchemaRejectCloud()
    worker = Worker(cloud, SimpleLLM(), rules, QuoteEngine(rules), settings)

    worker.cache.save_pending(
        task.task_id,
        {"result": {"classification_confidence": None, "project_type": "simple_part"}},
    )
    result = worker.run_task(task)

    assert worker.cache.pending_for(task.task_id) is None
    assert len(cloud.completed) == 1
    assert isinstance(cloud.completed[0]["result"]["classification_confidence"], float)
    assert result["result"]["project_type"] == "simple_part"
