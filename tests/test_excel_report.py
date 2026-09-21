# -*- coding: utf-8 -*-
"""Excel 报告: 懒建 sheet / 两段式步骤名 / 汇总统计 / 截图嵌入。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.excel_report import ExcelReport


def test_two_phase_desc_and_stats(tmp_path):
    p = tmp_path / "r.xlsx"
    r = ExcelReport(path=p)
    r.set_step_desc("全局清扫", "点击开始清扫")
    r.add_result("全局清扫", True)
    r.set_step_desc("全局清扫", "检查清扫中")
    r.add_result("全局清扫", False, error="超时")
    r.save()
    assert p.exists()

    from openpyxl import load_workbook
    wb = load_workbook(p)
    ws = wb["全局清扫"]
    assert ws.cell(row=2, column=1).value == "点击开始清扫"
    assert ws.cell(row=2, column=2).value == "PASS"
    assert ws.cell(row=3, column=1).value == "检查清扫中"
    assert ws.cell(row=3, column=2).value == "FAIL"
    assert ws.cell(row=3, column=3).value == "超时"


def test_summary_sheet_has_pass_rate(tmp_path):
    p = tmp_path / "r.xlsx"
    r = ExcelReport(path=p)
    r.add_result("用例A", True)
    r.add_result("用例B", False)
    r.save()
    from openpyxl import load_workbook
    wb = load_workbook(p)
    ws = wb["汇总"]
    texts = [str(c.value) for row in ws.iter_rows() for c in row if c.value]
    assert any("通过率" in t for t in texts)
    assert any("设备SN" in t for t in texts)


def test_screenshot_embedded(tmp_path):
    from PIL import Image
    img = tmp_path / "shot.png"
    Image.new("RGB", (40, 30), "red").save(img)
    p = tmp_path / "r2.xlsx"
    r = ExcelReport(path=p)
    r.set_step_desc("用例A", "截图步骤")
    r.add_result("用例A", True, screenshot=str(img))
    r.save()
    from openpyxl import load_workbook
    wb = load_workbook(p)
    assert len(wb["用例A"]._images) == 1


def test_device_info_filled(tmp_path):
    p = tmp_path / "r3.xlsx"
    r = ExcelReport(path=p)
    r.set_device_info(sn="SN12345", app_version="1.8.45.24")
    r.add_result("用例A", True)
    r.save()
    from openpyxl import load_workbook
    wb = load_workbook(p)
    texts = [str(c.value) for row in wb["汇总"].iter_rows() for c in row if c.value]
    assert any("SN12345" in t for t in texts)


def test_case_name_sanitized(tmp_path):
    """用例名含 / \\ 时必须清洗,否则 openpyxl 建 sheet 报错"""
    p = tmp_path / "r4.xlsx"
    r = ExcelReport(path=p)
    r.add_result("清扫/记录", True)
    r.save()
    from openpyxl import load_workbook
    wb = load_workbook(p)
    assert "清扫_记录" in wb.sheetnames
