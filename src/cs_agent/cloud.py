"""本地 AI 客服云端客户端 + 内存版 Mock 云端（开发/测试用）。

对应 11-ai-customer-service-local.md 的 Worker 契约：
- 本地 Worker 主动 HTTPS 出站领取聊天任务；
- 完成回传必须带 Idempotency-Key（用 task_id）；
- 租约 + 心跳与 quote-agent 保持一致。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
from pydantic import BaseModel, Field


class CloudError(Exception):
    def __init__(self, message: str, retryable: bool = False, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class ClaimedCsTask(BaseModel):
    task_id: str
    session_id: str = ""
    lease_token: str
    lease_expires_at: str
    customer_form: dict[str, Any] = Field(default_factory=dict)

    @property
    def message(self) -> str:
        return str(self.customer_form.get("message", "") or "")

    @property
    def history(self) -> list[dict]:
        raw = self.customer_form.get("history") or []
        return [
            {"role": str(m.get("role", "user")), "content": str(m.get("content", ""))}
            for m in raw
            if isinstance(m, dict)
        ]


class CsCloudClient:
    """对接后端 /api/v1/internal/ai-service/tasks/*（与 quote-agent 的 Worker 契约一致）。"""

    def __init__(
        self,
        base_url: str,
        token: str,
        agent_id: str,
        agent_version: str = "0.1.0",
        timeout_s: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.agent_id = agent_id
        self.agent_version = agent_version
        self.timeout_s = timeout_s

    def _headers(self, extra: dict | None = None) -> dict:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "X-Agent-Id": self.agent_id,
            "X-Agent-Version": self.agent_version,
            "X-Request-Id": str(uuid.uuid4()),
        }
        if extra:
            headers.update(extra)
        return headers

    def _request(
        self, method: str, path: str, json_body: dict | None = None, headers: dict | None = None
    ):
        try:
            resp = httpx.request(
                method,
                f"{self.base_url}{path}",
                json=json_body,
                headers=self._headers(headers),
                timeout=self.timeout_s,
            )
        except httpx.HTTPError as exc:
            raise CloudError(f"cloud request failed: {exc}", retryable=True) from exc
        if resp.status_code == 204:
            return None
        if resp.status_code >= 400:
            detail = resp.text[:300]
            retryable = resp.status_code in (429, 500, 502, 503)
            raise CloudError(
                f"cloud error {resp.status_code}: {detail}",
                retryable=retryable,
                status=resp.status_code,
            )
        return resp.json()

    def claim(self) -> ClaimedCsTask | None:
        body = {
            "agent_id": self.agent_id,
            "agent_version": self.agent_version,
            "capabilities": ["local_llm", "chat"],
        }
        data = self._request("POST", "/tasks/claim", body)
        if data is None:
            return None
        return ClaimedCsTask.model_validate(data)

    def heartbeat(
        self,
        task_id: str,
        lease_token: str,
        stage: str,
        progress_percent: int,
        message: str = "",
    ) -> None:
        self._request(
            "POST",
            f"/tasks/{task_id}/heartbeat",
            {
                "lease_token": lease_token,
                "stage": stage,
                "progress_percent": progress_percent,
                "message": message,
            },
        )

    def complete(self, task_id: str, lease_token: str, result: dict) -> dict:
        data = self._request(
            "POST",
            f"/tasks/{task_id}/complete",
            {"lease_token": lease_token, **result},
            headers={"Idempotency-Key": task_id},
        )
        return data or {}

    def fail(
        self,
        task_id: str,
        lease_token: str,
        stage: str,
        retryable: bool,
        error_code: str,
        message: str,
    ) -> None:
        self._request(
            "POST",
            f"/tasks/{task_id}/fail",
            {
                "lease_token": lease_token,
                "stage": stage,
                "retryable": retryable,
                "error_code": error_code,
                "message": message[:2000],
            },
        )


# ---------------------------------------------------------------- Mock 云端


class MockCsCloudStore:
    """线程安全的内存版客服任务库，模拟 11-ai-customer-service-local.md 的状态流转。"""

    LEASE_MINUTES = 30

    def __init__(self):
        self._lock = threading.Lock()
        self._tasks: dict[str, dict] = {}
        self._seq = 0

    def seed_task(
        self,
        message: str,
        history: list[dict] | None = None,
        session_id: str | None = None,
    ) -> str:
        with self._lock:
            self._seq += 1
            task_id = f"cs_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
            self._tasks[task_id] = {
                "task_id": task_id,
                "session_id": session_id or f"cs_demo_{uuid.uuid4().hex[:8]}",
                "status": "queued",
                "customer_form": {
                    "message": message,
                    "history": history or [],
                },
            }
            return task_id

    def claim(self, agent_id: str) -> dict | None:
        with self._lock:
            for task in self._tasks.values():
                if task["status"] != "queued":
                    continue
                task["status"] = "claimed"
                task["agent_id"] = agent_id
                task["lease_token"] = f"lease-{uuid.uuid4().hex}"
                task["lease_expires_at"] = _now_iso(minutes=self.LEASE_MINUTES)
                return {
                    "task_id": task["task_id"],
                    "session_id": task["session_id"],
                    "lease_token": task["lease_token"],
                    "lease_expires_at": task["lease_expires_at"],
                    "customer_form": task["customer_form"],
                }
            return None

    def heartbeat(self, task_id: str, lease_token: str) -> dict:
        with self._lock:
            task = self._require_task(task_id)
            self._check_lease(task, lease_token)
            task["lease_expires_at"] = _now_iso(minutes=self.LEASE_MINUTES)
            return {"task_id": task_id, "lease_expires_at": task["lease_expires_at"]}

    def complete(self, task_id: str, lease_token: str, payload: dict) -> dict:
        with self._lock:
            task = self._require_task(task_id)
            if task["status"] == "completed":
                return {"task_id": task_id, "status": "completed", "session_id": task["session_id"]}
            self._check_lease(task, lease_token)
            task["status"] = "completed"
            task["result"] = payload.get("result", {})
            return {
                "task_id": task_id,
                "status": "completed",
                "session_id": task["session_id"],
            }

    def fail(self, task_id: str, lease_token: str, payload: dict) -> dict:
        with self._lock:
            task = self._require_task(task_id)
            self._check_lease(task, lease_token)
            task["status"] = "retryable_failed" if payload.get("retryable") else "failed"
            task["error"] = payload
            return {"task_id": task_id, "status": task["status"]}

    def task(self, task_id: str) -> dict:
        with self._lock:
            return self._require_task(task_id)

    def _require_task(self, task_id: str) -> dict:
        if task_id not in self._tasks:
            raise KeyError(task_id)
        return self._tasks[task_id]

    def _check_lease(self, task: dict, lease_token: str) -> None:
        if task.get("lease_token") != lease_token:
            raise PermissionError("lease token mismatch")


def _now_iso(minutes: int = 0) -> str:
    return time.strftime(
        "%Y-%m-%dT%H:%M:%S+08:00", time.localtime(time.time() + minutes * 60)
    )


class _MockCsHandler(BaseHTTPRequestHandler):
    store: MockCsCloudStore = None

    def log_message(self, *args):  # 静默访问日志
        pass

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "ok"})
        else:
            self._json(404, {"error": {"code": "NOT_FOUND", "message": "unknown path"}})

    def do_POST(self):
        try:
            self._route()
        except KeyError:
            self._json(404, {"error": {"code": "TASK_NOT_FOUND", "message": "task not found"}})
        except PermissionError:
            self._json(409, {"error": {"code": "TASK_LEASE_CONFLICT", "message": "lease token mismatch"}})
        except Exception as exc:
            self._json(400, {"error": {"code": "BAD_REQUEST", "message": str(exc)[:300]}})

    def _route(self):
        parts = [p for p in self.path.strip("/").split("/") if p]
        if len(parts) < 6 or parts[:5] != ["api", "v1", "internal", "ai-service", "tasks"]:
            self._json(404, {"error": {"code": "NOT_FOUND", "message": "unknown endpoint"}})
            return

        body = self._body()
        if len(parts) == 6 and parts[5] == "claim":
            task = self.store.claim(body.get("agent_id", "unknown"))
            if task is None:
                self.send_response(204)
                self.end_headers()
            else:
                self._json(200, task)
            return

        if len(parts) == 7:
            task_id, action = parts[5], parts[6]
            if action == "heartbeat":
                self._json(200, self.store.heartbeat(task_id, body["lease_token"]))
                return
            if action == "complete":
                self._json(200, self.store.complete(task_id, body["lease_token"], body))
                return
            if action == "fail":
                self._json(200, self.store.fail(task_id, body["lease_token"], body))
                return

        self._json(404, {"error": {"code": "NOT_FOUND", "message": "unknown endpoint"}})

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _json(self, code: int, payload: dict):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class MockCsCloudServer:
    """开发/测试用：在随机端口启动内存版客服云端。"""

    def __init__(self, store: MockCsCloudStore | None = None, host: str = "127.0.0.1", port: int = 0):
        self.store = store or MockCsCloudStore()
        _MockCsHandler.store = self.store
        self._server = ThreadingHTTPServer((host, port), _MockCsHandler)
        self.host = host
        self.port = self._server.server_address[1]
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/api/v1/internal/ai-service"

    def start(self):
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._server.shutdown()
        self._server.server_close()
