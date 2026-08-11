"""批量导入脚本（seed_rag_cases.py）的数据加载与 dry-run 校验测试。

不依赖 Qdrant/Ollama：只验证 CLI 的格式解析、统计与错误处理。
"""

import json
import subprocess
import sys
from pathlib import Path


def _run(data_dir: Path, *extra: str):
    return subprocess.run(
        [
            sys.executable,
            "scripts/seed_rag_cases.py",
            "--data-dir",
            str(data_dir),
            "--dry-run",
            *extra,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=Path(__file__).resolve().parents[1],
    )


def test_dry_run_supports_json_array_single_and_jsonl(tmp_path: Path):
    # JSON 数组
    (tmp_path / "batch.json").write_text(
        json.dumps(
            [
                {
                    "case_id": "t1",
                    "project_type": "assembly_fixture",
                    "category": "tooling_fixture",
                    "summary": "压装工装 气缸 定位精度",
                },
                {
                    "case_id": "t2",
                    "project_type": "sheet_metal_part",
                    "category": "sheet_metal_cabinet",
                    "summary": "机柜钣金件 折弯",
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    # JSON 单条
    (tmp_path / "single.json").write_text(
        json.dumps(
            {
                "case_id": "t3",
                "project_type": "design_review",
                "category": "technical_consulting",
                "summary": "设计评审 技术咨询",
                "price_range_cny": [3000, 5000],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    # JSONL
    (tmp_path / "batch.jsonl").write_text(
        "# 注释行\n"
        '{"case_id": "t4", "project_type": "pneumatic_press_fixture", "category": "tooling_fixture", "summary": "轴承压装"}\n'
        '{"case_id": "t5", "project_type": "welding_fixture", "category": "tooling_fixture", "summary": "焊接夹具"}\n',
        encoding="utf-8",
    )

    proc = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "共 5 条案例" in proc.stdout
    assert "tooling_fixture=3" in proc.stdout
    assert "dry-run 校验通过" in proc.stdout


def test_dry_run_rejects_invalid_record(tmp_path: Path):
    (tmp_path / "bad.json").write_text(
        json.dumps({"case_id": "x"}),  # 缺少必填 project_type / summary
        encoding="utf-8",
    )
    proc = _run(tmp_path)
    assert proc.returncode == 1
    assert "[错误]" in proc.stderr


def test_dry_run_rejects_invalid_jsonl_line(tmp_path: Path):
    (tmp_path / "bad.jsonl").write_text('{"case_id": "ok", "project_type": "x", "summary": "y"}\nnot-json\n', encoding="utf-8")
    proc = _run(tmp_path)
    assert proc.returncode == 1
    assert "不是合法 JSON" in proc.stderr


def test_dry_run_no_matching_files(tmp_path: Path):
    proc = _run(tmp_path)
    assert proc.returncode == 1
    assert "未找到匹配" in proc.stderr
