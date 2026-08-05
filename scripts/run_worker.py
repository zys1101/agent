"""Worker 入口（骨架）。

下一里程碑将实现：领取任务 -> 下载文件 -> 解析 -> LLM 提取 -> 规则报价 -> 回传云端。
"""

from __future__ import annotations


def main() -> int:
    print("Worker 骨架尚未实现。当前里程碑 M1 已完成：规则配置 + 确定性报价引擎 + 测试。")
    print("下一里程碑：接入 Ollama（qwen3-vl:8b）与云端 API 契约（06-api-contract.md）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
