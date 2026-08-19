"""本地 AI 客服 Worker 入口。

用法：
  python scripts/run_cs_worker.py --local-test "如何发布设计需求？"   # 不连云端，直接问本地模型
  python scripts/run_cs_worker.py --once                              # 处理一个云端任务后退出
  python scripts/run_cs_worker.py --demo                              # 本地 mock 云端 + 样例问题，全链路演示
  python scripts/run_cs_worker.py                                     # 常驻轮询（每 2 秒领取一次）
"""

from __future__ import annotations

import argparse
import os
import time

from dotenv import load_dotenv

from cs_agent.cloud import CsCloudClient, MockCsCloudServer, MockCsCloudStore
from cs_agent.config import CsAgentSettings
from cs_agent.service import CustomerService
from cs_agent.worker import CsWorker
from quote_agent.llm import OllamaClient

DEMO_QUESTIONS = [
    "你们平台是做什么的？",
    "如何发布设计需求？",
    "AI 智能报价怎么用？",
]


def _make_llm(settings: CsAgentSettings) -> OllamaClient:
    return OllamaClient(
        base_url=f"http://{settings.ollama_host}:{settings.ollama_port}",
        model=settings.ollama_model,
        timeout_s=settings.timeout_s,
        temperature=settings.temperature,
    )


def _run_local_test(settings: CsAgentSettings, question: str) -> int:
    service = CustomerService(_make_llm(settings), settings)
    print(f"模型: {settings.ollama_model} @ {settings.ollama_host}:{settings.ollama_port}")
    print(f"问题: {question}\n")
    result = service.reply(question, [])
    print("回复:")
    print("-" * 60)
    print(result["reply"])
    print("-" * 60)
    if result.get("suggestions"):
        print("快捷追问:", " | ".join(result["suggestions"]))
    return 0


def _run_demo(settings: CsAgentSettings) -> int:
    store = MockCsCloudStore()
    for q in DEMO_QUESTIONS:
        store.seed_task(q)
    server = MockCsCloudServer(store).start()
    try:
        cloud = CsCloudClient(server.base_url, "dev-token", settings.agent_id)
        worker = CsWorker(cloud, CustomerService(_make_llm(settings), settings), settings)
        while True:
            task = cloud.claim()
            if task is None:
                print(f"{time.strftime('%H:%M:%S')} 无排队任务，2s 后重试")
                time.sleep(2)
                continue
            try:
                result = worker.run_task(task)
                print("=" * 60)
                print(f"任务 {task.task_id} 完成")
                print(f"问题: {task.message}")
                print(f"回复: {result['reply'][:120]}")
                print("=" * 60)
            except Exception as exc:
                print(f"任务 {task.task_id} 失败: {exc}")
            if all(t["status"] in ("completed", "failed", "retryable_failed") for t in store._tasks.values()):
                return 0
    finally:
        server.stop()


def _run_loop(settings: CsAgentSettings, once: bool, interval_s: float) -> int:
    base_url = os.environ.get("CLOUD_API_BASE_URL", "")
    token = os.environ.get("AGENT_API_TOKEN", "")
    if not base_url or not token:
        raise SystemExit("缺少 CLOUD_API_BASE_URL / AGENT_API_TOKEN，请检查 .env（或用 --demo / --local-test）")
    cloud = CsCloudClient(base_url, token, settings.agent_id)
    worker = CsWorker(cloud, CustomerService(_make_llm(settings), settings), settings)
    while True:
        task = cloud.claim()
        if task is None:
            if once:
                return 0
            print(f"{time.strftime('%H:%M:%S')} 无排队任务，{interval_s}s 后重试")
            time.sleep(interval_s)
            continue
        try:
            result = worker.run_task(task)
            print(f"{time.strftime('%H:%M:%S')} 任务 {task.task_id} 完成: {result['reply'][:80]}")
        except Exception as exc:
            print(f"{time.strftime('%H:%M:%S')} 任务 {task.task_id} 失败: {exc}")
        if once:
            return 0
        time.sleep(interval_s)


def main() -> int:
    parser = argparse.ArgumentParser(description="AI 客服本地 Worker")
    parser.add_argument("--once", action="store_true", help="处理一个任务后退出")
    parser.add_argument("--demo", action="store_true", help="使用本地 mock 云端 + 样例问题")
    parser.add_argument("--local-test", nargs="?", const="你好，请介绍一下你们平台。", help="不连云端，直接问本地模型")
    parser.add_argument("--interval", type=float, default=None, help="无任务时轮询间隔（秒）")
    args = parser.parse_args()

    load_dotenv()
    settings = CsAgentSettings.from_env()
    settings.snapshot_dir.mkdir(parents=True, exist_ok=True)
    interval = args.interval if args.interval is not None else settings.poll_interval_s

    if args.local_test is not None:
        return _run_local_test(settings, args.local_test)
    if args.demo:
        return _run_demo(settings)
    return _run_loop(settings, args.once, interval)


if __name__ == "__main__":
    raise SystemExit(main())
