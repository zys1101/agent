"""Worker 入口：领取任务 -> 处理 -> 回传（AWF-001）。

用法：
  python scripts/run_worker.py --once              # 处理一个云端任务后退出
  python scripts/run_worker.py --demo --once       # 本地 mock 云端 + 样例任务，全链路演示
  python scripts/run_worker.py                     # 常驻轮询（每 15 秒领取一次）
"""

from __future__ import annotations

import argparse
import os
import tempfile
import time
from pathlib import Path

from dotenv import load_dotenv

from quote_agent.cloud import CloudClient, MockCloudServer, MockCloudStore
from quote_agent.config import QuoteRules
from quote_agent.llm import OllamaClient
from quote_agent.quote_engine import QuoteEngine
from quote_agent.worker import Worker, WorkerSettings

SAMPLE_TEXT = """客户需求：设计一套用于电机壳体压装的工装。
使用场景：产线上将轴承压入电机壳体，单件节拍约 45 秒，每日产量 600 件。
工件：电机壳体，最大外径 180mm，高度 120mm，重量约 3kg，材料为压铸铝。
要求：包含气缸选型与安装接口预留，定位精度 ±0.05mm，需要 3D 装配、8 张 2D 零件图和 BOM。
交期：客户希望 10 个工作日内完成。压装力暂未确认，安装接口尺寸待客户提供。
仅需设计服务，不包含制造与调试。"""


def _seed_demo(store: MockCloudStore, tmp: Path) -> str:
    txt = tmp / "requirements_sample.txt"
    txt.write_text(SAMPLE_TEXT, encoding="utf-8")
    return store.seed_task(
        customer_form={
            "project_name": "气动压装工装设计（样例）",
            "customer_description": "设计一套用于电机壳体压装的工装。",
            "deadline_date": "2026-08-20",
            "requested_deliverables": ["3D模型", "2D工程图", "BOM"],
            "currency": "CNY",
        },
        files=[
            {
                "file_id": "file_01",
                "original_name": "requirements_sample.txt",
                "mime_type": "text/plain",
                "size_bytes": txt.stat().st_size,
                "local_path": str(txt),
            }
        ],
    )


def _run_demo(settings: WorkerSettings, interval_s: float) -> int:
    rules = QuoteRules.load()
    engine = QuoteEngine(rules)
    with tempfile.TemporaryDirectory() as tmp:
        store = MockCloudStore()
        task_id = _seed_demo(store, Path(tmp))
        server = MockCloudServer(store).start()
        try:
            cloud = CloudClient(server.base_url, "dev-token", settings.agent_id)
            worker = Worker(cloud, OllamaClient.from_env(), rules, engine, settings)
            task = cloud.claim()
            assert task is not None
            result = worker.run_task(task)
            summary = result["result"]
            print("=" * 60)
            print(f"任务 {task.task_id} 完成 -> quote_id={result['_cloud'].get('quote_id')}")
            print(f"项目类型: {summary['project_type']}  完整度: {summary['completeness_score']}")
            print(f"价格区间: {summary['price']}")
            print(f"强制审核: {summary['manual_review_required']}")
            for reason in summary["manual_review_reasons"]:
                print(f"  - {reason}")
            if summary["clarification_questions"]:
                print("澄清问题:")
                for q in summary["clarification_questions"]:
                    print(f"  ? {q}")
            print("=" * 60)
            print("云端任务状态:", store.task(task_id)["status"])
            return 0
        finally:
            server.stop()


def _run_loop(settings: WorkerSettings, once: bool, interval_s: float) -> int:
    rules = QuoteRules.load()
    engine = QuoteEngine(rules)
    base_url = os.environ.get("CLOUD_API_BASE_URL", "")
    token = os.environ.get("AGENT_API_TOKEN", "")
    if not base_url or not token:
        raise SystemExit("缺少 CLOUD_API_BASE_URL / AGENT_API_TOKEN，请检查 .env（或用 --demo）")
    cloud = CloudClient(base_url, token, settings.agent_id)
    worker = Worker(cloud, OllamaClient.from_env(), rules, engine, settings)
    while True:
        task = cloud.claim()
        if task is None:
            print(f"{time.strftime('%H:%M:%S')} 无排队任务，{interval_s}s 后重试")
            if once:
                return 0
            time.sleep(interval_s)
            continue
        try:
            result = worker.run_task(task)
            print(
                f"任务 {task.task_id} 完成 quote_id={result.get('_cloud', {}).get('quote_id')} "
                f"审核={result['result']['manual_review_required']}"
            )
        except Exception as exc:
            print(f"任务 {task.task_id} 失败: {exc}")
        if once:
            return 0
        time.sleep(interval_s)


def main() -> int:
    parser = argparse.ArgumentParser(description="Quote Agent Worker")
    parser.add_argument("--once", action="store_true", help="处理一个任务后退出")
    parser.add_argument("--demo", action="store_true", help="使用本地 mock 云端 + 样例任务")
    parser.add_argument("--interval", type=float, default=15.0, help="无任务时轮询间隔（秒）")
    args = parser.parse_args()

    load_dotenv()
    settings = WorkerSettings.from_env()
    settings.work_dir.mkdir(parents=True, exist_ok=True)
    settings.snapshot_dir.mkdir(parents=True, exist_ok=True)

    if args.demo:
        return _run_demo(settings, args.interval)
    return _run_loop(settings, args.once, args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
