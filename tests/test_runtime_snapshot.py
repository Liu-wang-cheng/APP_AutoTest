# -*- coding: utf-8 -*-
"""tools/runtime_snapshot.py 的纯函数测试(不连设备)。"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "runtime_snapshot.py"


def load_tool():
    spec = importlib.util.spec_from_file_location("runtime_snapshot", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


XML = """<hierarchy>
  <node package="com.app" text="正在清洁" resource-id="com.app:id/status" bounds="[0,0][100,100]"/>
  <node package="com.app" content-desc="菜单" resource-id="com.app:id/menu" bounds="[0,100][100,200]"/>
  <node package="com.android.systemui" text="12:00" resource-id="com.android.systemui:id/clock" bounds="[0,0][100,50]"/>
  <node package="com.app" text="隐藏节点" bounds="[0,0][0,0]"/>
  <node package="com.app" text="无id文本" bounds="[0,0][10,10]"/>
</hierarchy>"""


def test_normalize_text():
    sh = load_tool()
    assert sh.normalize_text("正在干燥拖布 2 小时 剩余") == "正在干燥拖布 # 小时 剩余"
    assert sh.normalize_text("今天 10:27") == "今天 #"
    assert sh.normalize_text("2m²") == "#m²"
    assert sh.normalize_text("100%") == ""      # 纯数值+符号, 不作为选择器
    assert sh.normalize_text("540") == ""
    assert sh.normalize_text("   ") == ""


def test_parse_xml_keeps_app_visible_nodes():
    sh = load_tool()
    texts, rids = sh.parse_xml(XML, "com.app")
    assert texts == ["正在清洁", "菜单", "无id文本"]
    assert rids == ["com.app:id/status", "com.app:id/menu"]


def test_diff_snapshots_removed_and_added():
    sh = load_tool()
    baseline = {"pages": {"device": {"normalized": ["正在清洁", "菜单"], "error": None}}}
    current = {"pages": {"device": {"normalized": ["正在清洁", "打扫报告"], "error": None}}}
    result = sh.diff_snapshots(baseline, current)
    assert result["device"]["removed"] == ["菜单"]
    assert result["device"]["added"] == ["打扫报告"]
    assert result["device"]["skipped"] is False


def test_diff_snapshots_skips_failed_page():
    sh = load_tool()
    baseline = {"pages": {"report": {"normalized": ["打扫报告"], "error": None}}}
    current = {"pages": {"report": {"normalized": [], "error": "点击「打扫报告」失败"}}}
    result = sh.diff_snapshots(baseline, current)
    assert result["report"]["skipped"] is True
    assert result["report"]["removed"] == []
