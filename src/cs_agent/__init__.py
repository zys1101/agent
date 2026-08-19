"""本地 AI 客服 Agent：Ollama 本地模型 + 云端任务队列（对应 11-ai-customer-service-local.md）。"""

from .cloud import ClaimedCsTask, CsCloudClient, MockCsCloudServer, MockCsCloudStore
from .config import CsAgentSettings
from .service import CustomerService
from .worker import CsWorker

__all__ = [
    "ClaimedCsTask",
    "CsCloudClient",
    "MockCsCloudServer",
    "MockCsCloudStore",
    "CsAgentSettings",
    "CustomerService",
    "CsWorker",
]
