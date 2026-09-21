# -*- coding: utf-8 -*-
"""用例执行顺序(case_order):GUI 行内箭头维护 + CLI collect_cases 排序一致。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_collect_cases_sorted_by_case_order(tmp_path, monkeypatch):
    """CLI 全量收集按顶层 case_order 升序;没写的排末尾按文件名(与 GUI 一致)"""
    import tests.test_yaml_runner as tyr
    d = tmp_path / "Test_cases"
    d.mkdir()
    (d / "乙.yaml").write_text("case_order: 1\nmodule: 乙\ncases: [{name: a, steps: []}]\n", encoding="utf-8")
    (d / "甲.yaml").write_text("case_order: 2\nmodule: 甲\ncases: [{name: a, steps: []}]\n", encoding="utf-8")
    (d / "丙.yaml").write_text("module: 丙\ncases: [{name: a, steps: []}]\n", encoding="utf-8")
    monkeypatch.setattr(tyr, "BASE_DIR", str(tmp_path), raising=False)
    got = [m for m, *_ in tyr.collect_cases()]
    assert got == ["乙", "甲", "丙"]


def test_collect_cases_bad_order_falls_to_tail(tmp_path, monkeypatch):
    """case_order 非法(字符串/None)不炸,按缺失处理排末尾"""
    import tests.test_yaml_runner as tyr
    d = tmp_path / "Test_cases"
    d.mkdir()
    (d / "正常.yaml").write_text("case_order: 1\nmodule: 正常\ncases: [{name: a, steps: []}]\n", encoding="utf-8")
    (d / "坏序号.yaml").write_text("case_order: 第几\nmodule: 坏序号\ncases: [{name: a, steps: []}]\n", encoding="utf-8")
    monkeypatch.setattr(tyr, "BASE_DIR", str(tmp_path), raising=False)
    got = [m for m, *_ in tyr.collect_cases()]
    assert got == ["正常", "坏序号"]
