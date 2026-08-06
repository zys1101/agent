"""云端 API 客户端与内存版 Mock 云端（开发/测试用）。

对应 06-api-contract.md：
- 本地 Worker 主动 HTTPS 出站领取任务；
- 完成回传必须带 Idempotency-Key（用 task_id）；
- 租约 + 心跳。
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


class TaskFile(BaseModel):
    file_id: str
    original_name: str
    mime_type: str = "application/octet-stream"
    size_bytes: int = 0
    download_url: str = ""
    local_path: str = ""
    download_url_expires_at: str | None = None


class ClaimedTask(BaseModel):
    task_id: str
    project_id: str
    lease_token: str
    lease_expires_at: str
    customer_form: dict[str, Any]
    files: list[TaskFile] = Field(default_factory=list)
    rule_set_version: str = ""


class CloudClient:
    """对接真实腾讯云后端的客户端；也可指向 MockCloudServer。"""

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

    def _request(self, method: str, path: str, json_body: dict | None = None, headers: dict | None = None):
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
        try:
            return resp.json()
        except ValueError as exc:
            raise CloudError(
                f"cloud returned non-JSON response (status={resp.status_code}): {resp.text[:200]!r}",
                status=resp.status_code,
            ) from exc

    def claim(self, capabilities: list[str] | None = None) -> ClaimedTask | None:
        body = {
            "agent_id": self.agent_id,
            "agent_version": self.agent_version,
            "capabilities": capabilities or ["pdf_parser", "image_ocr", "excel_parser", "local_llm", "quote_engine"],
            "max_file_size_mb": 100,
        }
        data = self._request("POST", "/tasks/claim", body)
        if data is None:
            return None
        return ClaimedTask.model_validate(data)

    def heartbeat(self, task_id: str, lease_token: str, stage: str, progress_percent: int, message: str = "") -> None:
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

    def fail(self, task_id: str, lease_token: str, stage: str, retryable: bool, error_code: str, message: str) -> None:
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


class MockCloudStore:
    """线程安全的内存版任务库，模拟 06-api-contract 的状态流转。"""

    LEASE_MINUTES = 30

    def __init__(self):
        self._lock = threading.Lock()
        self._tasks: dict[str, dict] = {}
        self._results: dict[str, dict] = {}
        self._quotes: dict[str, str] = {}
        self._task_seq = 0

    def seed_task(
        self,
        customer_form: dict,
        files: list[dict],
        project_id: str | None = None,
    ) -> str:
        with self._lock:
            self._task_seq += 1
            task_id = f"qt_{uuid.uuid4().hex[:10]}"
            self._tasks[task_id] = {
                "task_id": task_id,
                "project_id": project_id or f"prj_{uuid.uuid4().hex[:10]}",
                "status": "queued",
                "customer_form": customer_form,
                "files": files,
                "rule_set_version": "0.1.0-draft",
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
                    "project_id": task["project_id"],
                    "lease_token": task["lease_token"],
                    "lease_expires_at": task["lease_expires_at"],
                    "customer_form": task["customer_form"],
                    "files": task["files"],
                    "rule_set_version": task["rule_set_version"],
                }
            return None

    def heartbeat(self, task_id: str, lease_token: str) -> dict:
        with self._lock:
            task = self._require_task(task_id)
            self._check_lease(task, lease_token)
            task["lease_expires_at"] = _now_iso(minutes=self.LEASE_MINUTES)
            return {"task_id": task_id, "lease_expires_at": task["lease_expires_at"]}

    def complete(self, task_id: str, lease_token: str, idempotency_key: str, result: dict) -> dict:
        with self._lock:
            if idempotency_key in self._quotes:
                quote_id = self._quotes[idempotency_key]
                task = self._require_task(task_id)
                return {"task_id": task_id, "status": task["status"], "quote_id": quote_id}
            task = self._require_task(task_id)
            self._check_lease(task, lease_token)
            task["status"] = "completed"
            task["result"] = result
            quote_id = f"quote_{uuid.uuid4().hex[:10]}"
            self._quotes[idempotency_key] = quote_id
            return {
                "task_id": task_id,
                "status": "completed",
                "quote_id": quote_id,
                "project_status": "pending_review" if result.get("result", {}).get("manual_review_required") else "quoted",
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

    def all_tasks(self) -> list[dict]:
        with self._lock:
            return [
                {k: v for k, v in task.items() if k not in ("result", "lease_token")}
                for task in self._tasks.values()
            ]

    def _require_task(self, task_id: str) -> dict:
        if task_id not in self._tasks:
            raise KeyError(task_id)
        return self._tasks[task_id]

    def _check_lease(self, task: dict, lease_token: str) -> None:
        if task.get("lease_token") != lease_token:
            raise PermissionError("lease token mismatch")


def _now_iso(minutes: int = 0) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(time.time() + minutes * 60))


class _MockHandler(BaseHTTPRequestHandler):
    store: MockCloudStore = None

    def log_message(self, *args):  # 静默访问日志
        pass

    def do_GET(self):
        if self.path == "/api/internal/agent/tasks":
            self._json(200, {"tasks": self.store.all_tasks()})
        elif self.path == "/health":
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
        path = self.path.split("?")[0]
        if path == "/api/internal/agent/tasks/claim":
            task = self.store.claim(self._body().get("agent_id", "unknown"))
            if task is None:
                self.send_response(204)
                self.end_headers()
            else:
                self._json(200, task)
            return
        parts = [p for p in path.strip("/").split("/") if p]
        if len(parts) == 6 and parts[0] == "api" and parts[3] == "tasks":
            task_id, action = parts[4], parts[5]
            body = self._body()
            if action == "heartbeat":
                self._json(200, self.store.heartbeat(task_id, body["lease_token"]))
            elif action == "complete":
                idem = self.headers.get("Idempotency-Key") or task_id
                self._json(200, self.store.complete(task_id, body["lease_token"], idem, body))
            elif action == "fail":
                self._json(200, self.store.fail(task_id, body["lease_token"], body))
            else:
                self._json(404, {"error": {"code": "NOT_FOUND", "message": "unknown action"}})
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


class MockCloudServer:
    """开发/测试用：在随机端口启动内存版云端。"""

    def __init__(self, store: MockCloudStore | None = None, host: str = "127.0.0.1", port: int = 0):
        self.store = store or MockCloudStore()
        _MockHandler.store = self.store
        self._server = ThreadingHTTPServer((host, port), _MockHandler)
        self.host = host
        self.port = self._server.server_address[1]
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/api/internal/agent"

    def start(self):
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._server.shutdown()
        self._server.server_close()
