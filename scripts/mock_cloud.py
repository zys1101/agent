"""开发用 Mock 云端：内存版任务库 + HTTP 服务（06-api-contract 的子集）。

用法：
  python scripts/mock_cloud.py --port 8000 --seed-dir data/mock_tasks
"""

from __future__ import annotations

import argparse
from pathlib import Path

from quote_agent.cloud import MockCloudServer, MockCloudStore


def _seed_from_dir(store: MockCloudStore, seed_dir: Path) -> None:
    if not seed_dir.is_dir():
        return
    for path in sorted(seed_dir.iterdir()):
        if not path.is_file():
            continue
        store.seed_task(
            customer_form={
                "project_name": f"项目 {path.stem}",
                "customer_description": f"见附件 {path.name}",
                "deadline_date": "",
                "requested_deliverables": [],
                "currency": "CNY",
            },
            files=[
                {
                    "file_id": f"file_{path.stem[:12]}",
                    "original_name": path.name,
                    "mime_type": "text/plain",
                    "size_bytes": path.stat().st_size,
                    "local_path": str(path),
                }
            ],
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Mock quote cloud server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--seed-dir", default="data/mock_tasks")
    args = parser.parse_args()

    store = MockCloudStore()
    _seed_from_dir(store, Path(args.seed_dir))
    server = MockCloudServer(store, host=args.host, port=args.port)
    print(f"Mock cloud listening on {args.host}:{args.port} (seeded tasks={len(store.all_tasks())})")
    server.start()
    try:
        import time

        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
