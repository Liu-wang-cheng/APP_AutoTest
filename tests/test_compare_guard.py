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
    """扫 Test_cases/ 下所有用例,产出 (用例名, 键, 阈值)"""
    import os
    from core.driver import BASE_DIR, load_yaml_file
    from tests.test_yaml_runner import iter_modules
    cases_dir = os.path.join(BASE_DIR, "Test_cases")
    if not os.path.isdir(cases_dir):
        return
    for fn in sorted(os.listdir(cases_dir)):
        if not fn.endswith((".yaml", ".yml")):
            continue
        data = load_yaml_file(os.path.join(cases_dir, fn))
        for module, cases in iter_modules(data, fn):
            for case in cases:
                label = f"{module}/{case.get('name', '')}"
                for step in case.get("steps") or []:
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
