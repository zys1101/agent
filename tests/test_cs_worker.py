"""本地 AI 客服 Worker 的离线测试（不依赖真实 Ollama/云端）。"""

from __future__ import annotations

from cs_agent.cloud import CsCloudClient, MockCsCloudServer, MockCsCloudStore
from cs_agent.config import CsAgentSettings
from cs_agent.service import CustomerService
from cs_agent.worker import CsWorker
from quote_agent.llm import LLMUnavailableError


class FakeLLM:
    def __init__(self, text: str = "您好，发布需求请点击「发布需求」按钮并填写描述。", raise_error: bool = False):
        self.text = text
        self.raise_error = raise_error

    def generate(self, prompt: str, images: list[str] | None = None) -> str:
        if self.raise_error:
            raise LLMUnavailableError("ollama down")
        return self.text


def _settings() -> CsAgentSettings:
    return CsAgentSettings()


def test_worker_completes_task():
    store = MockCsCloudStore()
    task_id = store.seed_task("如何发布设计需求？", [])
    server = MockCsCloudServer(store).start()
    try:
        cloud = CsCloudClient(server.base_url, "dev-token", "office-4070super-01")
        task = cloud.claim()
        assert task is not None and task.task_id == task_id
        worker = CsWorker(cloud, CustomerService(FakeLLM(), _settings()), _settings())
        result = worker.run_task(task)
        assert result["reply"]
        assert result["_cloud"]["status"] == "completed"
        assert store.task(task_id)["status"] == "completed"
        assert store.task(task_id)["result"]["reply"] == result["reply"]
    finally:
        server.stop()


def test_worker_fails_when_llm_unavailable():
    store = MockCsCloudStore()
    task_id = store.seed_task("你好", [])
    server = MockCsCloudServer(store).start()
    try:
        cloud = CsCloudClient(server.base_url, "dev-token", "office-4070super-01")
        task = cloud.claim()
        assert task is not None
        worker = CsWorker(cloud, CustomerService(FakeLLM(raise_error=True), _settings()), _settings())
        try:
            worker.run_task(task)
        except LLMUnavailableError:
            pass
        assert store.task(task_id)["status"] == "retryable_failed"
    finally:
        server.stop()


def test_suggestions_exclude_repeated_keyword():
    service = CustomerService(FakeLLM(), _settings())
    suggestions = service.build_suggestions("您可以发布需求到接单大厅。")
    assert "如何发布设计需求？" not in suggestions
    assert suggestions
