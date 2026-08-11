"""脱敏历史案例（ai_quote_historical_cases.json）完整性测试。"""

import json
import re
import subprocess
import sys
from pathlib import Path

from quote_agent.rag import CaseRecord

CASES_PATH = (
    Path(__file__).resolve().parents[1]
    / "examples"
    / "knowledge_cases"
    / "ai_quote_historical_cases.json"
)


def load_raw_cases() -> list[dict]:
    data = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, list)
    return data


def test_all_cases_validate_as_case_record():
    raw = load_raw_cases()
    assert len(raw) == 14
    for record in raw:
        case = CaseRecord.model_validate(record)
        assert case.case_id
        assert case.project_type
        assert case.category
        assert case.summary
        assert case.typical_hours is not None and case.typical_hours > 0
        assert case.price_range_cny is not None


def test_cases_pass_seed_pipeline():
    result = subprocess.run(
        [
            sys.executable,
            "scripts/seed_rag_cases.py",
            "--data-dir",
            str(CASES_PATH.parent),
            "--glob",
            "ai_quote_historical_cases.json",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=Path(__file__).resolve().parents[1],
    )
    assert result.returncode == 0, result.stderr
    assert "共 14 条案例" in result.stdout


def test_golden_case_values():
    records = {c["case_id"]: c for c in load_raw_cases()}

    assert records["aiq_0001"]["typical_hours"] == 2
    assert records["aiq_0001"]["price_range_cny"] == [100, 100]

    assert records["aiq_0003"]["typical_hours"] == 8
    assert records["aiq_0003"]["price_range_cny"] == [2400, 2400]

    assert records["aiq_0007"]["typical_hours"] == 0.5
    assert records["aiq_0007"]["price_range_cny"] == [50, 50]

    assert records["aiq_0013"]["typical_hours"] == 0.5
    assert records["aiq_0013"]["price_range_cny"] == [100, 100]


def test_type_mapping_consistency():
    records = {c["case_id"]: c for c in load_raw_cases()}
    # 机构类案例统一映射到 mechanism_design，建模类统一映射到 drawing_modeling
    for case_id in ("aiq_0002", "aiq_0006", "aiq_0007", "aiq_0008", "aiq_0009", "aiq_0010", "aiq_0011"):
        assert records[case_id]["project_type"] == "mechanism_design"
        assert records[case_id]["category"] == "mechanism_design"
    for case_id in ("aiq_0012", "aiq_0013", "aiq_0014"):
        assert records[case_id]["project_type"] == "drawing_modeling"
        assert records[case_id]["category"] == "drawing_modeling"


def test_no_sensitive_or_media_content():
    """脱敏检查：不允许 URL、图片路径、图片文件后缀或外部图床痕迹。"""
    sensitive = re.compile(
        r"https?://|images/|chatglm|\.png|\.jpe?g|\.gif|\.bmp|\.webp",
        re.IGNORECASE,
    )
    fields = ("case_id", "summary", "deliverables", "risk_notes", "project_type", "category")
    for record in load_raw_cases():
        for field in fields:
            value = record.get(field)
            if isinstance(value, list):
                value = " ".join(str(v) for v in value)
            assert not sensitive.search(str(value)), (
                f"{record['case_id']}.{field} 含有图片/URL痕迹：{value}"
            )


def test_duplicate_ids():
    raw = load_raw_cases()
    ids = [r["case_id"] for r in raw]
    assert len(ids) == len(set(ids))
