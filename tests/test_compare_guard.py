# -*- coding: utf-8 -*-
"""compare/diff threshold 越界必须显式报错(防 threshold:1 断言恒真而报告仍绿)。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.actions.asserts import check_threshold


def test_diff_threshold_1_rejected():
    with pytest.raises(ValueError, match="恒"):
        check_threshold(1.0, inverse=True)


def test_diff_threshold_above_1_rejected():
    with pytest.raises(ValueError, match="恒"):
        check_threshold(1.5, inverse=True)


def test_compare_threshold_above_1_rejected():
    with pytest.raises(ValueError, match="恒"):
        check_threshold(1.2, inverse=False)


def test_valid_thresholds_pass():
    check_threshold(0.99, inverse=True)
    check_threshold(0.6, inverse=False)
    check_threshold(0.0, inverse=False)
    check_threshold(1.0, inverse=False)   # compare 允许 =1(严格小于才失败)


# ── 对实际用例文件的静态检查 ──

def _iter_compare_steps():
    """扫 Test_cases/ 下所有用例(**含 APP 组子目录**), 产出 (用例名, 键, 阈值)

    ★ 原来用 `os.listdir(cases_dir)` 只扫**顶层** —— 而真实用例都在
      `Test_cases/<APP组>/` 子目录里(顶层 0 个 yaml), 于是循环体从不执行、
      checked 恒为 0, 这个守护一直空转(实测输出"共检查 0 处 compare/diff 阈值")。
      这里复用 collect_cases —— 它已正确处理组目录与 case_order;
      else 子步骤同样会执行, 一并展开(iter_all_steps)。
    """
    from tests.test_yaml_runner import collect_cases, iter_all_steps
    for module, name, steps, *_ in collect_cases():
        label = f"{module}/{name}"
        for step, _depth in iter_all_steps(steps):
            for key in ("compare", "diff"):
                if key in step:
                    default = 0.99 if key == "diff" else 0.6
                    yield label, key, float(step.get("threshold", default))


def test_所有用例的阈值都合法():
    """用例文件里的 compare/diff 阈值不能越界

    阈值越界会让断言恒真/恒假,而报告里完全看不出来 —— 这是最危险的静默失败,
    所以要在静态检查阶段就拦下来,而不是等真机跑完看到一片绿。
    """
    checked = 0
    for label, key, threshold in _iter_compare_steps():
        try:
            check_threshold(threshold, inverse=(key == "diff"))
        except ValueError as e:
            pytest.fail(f"用例「{label}」的 {key} 阈值非法: {e}")
        checked += 1
    print(f"共检查 {checked} 处 compare/diff 阈值")
    # ★ 防回归: 扫描逻辑一旦又只能看到 0 个用例, 这个守护就等于没有 —— 必须报红,
    #   而不是安静地"通过"(它此前空转了多久没人知道)
    assert checked > 0, (
        "没有扫描到任何 compare/diff 步骤 —— 用例布局变了? "
        "扫描逻辑必须跟着改, 否则这个守护会静默失效")
