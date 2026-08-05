"""文件解析器测试：TXT / XLSX / 图片。"""

import openpyxl

from quote_agent.file_parser import FileParser


def test_parse_txt(tmp_path):
    path = tmp_path / "req.txt"
    path.write_text("客户需求：压装工装", encoding="utf-8")
    result = FileParser().parse(path, "f1", "req.txt", "text/plain")
    assert "压装工装" in result.text
    assert not result.errors


def test_parse_xlsx_bom_detection(tmp_path):
    path = tmp_path / "bom.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["图号", "名称", "数量", "材料"])
    ws.append(["P-001", "连接板", 2, "Q235"])
    ws.append(["P-002", "轴套", 4, "45钢"])
    wb.save(path)

    result = FileParser().parse(path, "f2", "bom.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert "疑似BOM表" in result.table_summary
    assert "P-001" in result.text
    assert not result.errors


def test_parse_image_marks_ocr_pending(tmp_path):
    path = tmp_path / "photo.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n fake image")
    result = FileParser().parse(path, "f3", "photo.png", "image/png")
    assert result.ocr_pending is True
    assert result.image_path == str(path)
