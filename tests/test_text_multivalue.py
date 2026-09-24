# -*- coding: utf-8 -*-
"""多值文本分隔符的一致性守护。

★ 用户要求(2026-09-24): 「检测下所有多文本判断是否都兼容不同格式的逗号」。

真机事故: 用例里写 `assert: 清洁中，正在吸尘`(**全角逗号**), 而断言只按半角逗号
`split(",")` 拆分 → 整串被当成一个文本去找, 必然失败:
    日志: [FAIL诊断] 步骤「判断是否处于清扫状态」失败: 超时(30s)未找到: 清洁中，正在吸尘

现在**所有多值文本统一走 `core.driver.split_texts`**(半角/全角逗号、顿号、分号、
换行都算分隔符)。本文件用一个"源码扫描"守住这条一致性 —— 只靠逐个功能的用例,
将来新增动作时很容易又写回各自为政的 `split(",")`。
"""
from pathlib import Path

from core.driver import split_texts

ROOT = Path(__file__).resolve().parent.parent

# core 里允许出现裸 .split(",") 的白名单(目前为空;确属非文本语义的在此登记并写明理由)
_ALLOWED_BARE_SPLIT = set()


def test_split_texts_accepts_all_comma_forms():
    """半角/全角逗号、顿号、分号、换行都能分隔;空项与多余空格自动丢弃"""
    assert split_texts("清洁中,正在吸尘") == ["清洁中", "正在吸尘"]
    assert split_texts("清洁中，正在吸尘") == ["清洁中", "正在吸尘"]
    assert split_texts("清洁中、正在吸尘") == ["清洁中", "正在吸尘"]
    assert split_texts("清洁中;正在吸尘；回充中") == ["清洁中", "正在吸尘", "回充中"]
    assert split_texts("清洁中\n正在吸尘") == ["清洁中", "正在吸尘"]
    assert split_texts(" 清洁中 ，， 正在吸尘 ") == ["清洁中", "正在吸尘"]
    assert split_texts("清洁中，") == ["清洁中"]
    assert split_texts("") == [] and split_texts([]) == []


def test_no_bare_comma_split_left_in_core():
    """★ core/ 里不许再有裸 `.split(",")` —— 多值文本一律走 split_texts

    这条是"漏改检测": 任何新增/遗留的裸拆分都会在这里被点名。
    """
    offenders = []
    for py in sorted((ROOT / "core").rglob("*.py")):
        rel = py.relative_to(ROOT).as_posix()
        for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if '.split(",")' in line or ".split(',')" in line:
                if f"{rel}:{lineno}" in _ALLOWED_BARE_SPLIT:
                    continue
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, (
        "这些地方仍按裸逗号拆分, 应改用 core.driver.split_texts(半角/全角逗号、"
        "顿号、分号、换行都算):\n  " + "\n  ".join(offenders))


def test_multivalue_sites_reference_shared_splitter():
    """关键多值判断点必须引用共用拆分器(防止将来又各写一套)"""
    expect = {
        "core/runner.py": "断言多值",
        "core/session.py": "前置(地图加载/文本检查)",
        "core/actions/basic.py": "if_click/find_click/wait_for/if 条件",
        "core/actions/data_ops.py": "grab/match 关键字",
        "core/actions/map_ops.py": "指哪扫哪 state_texts",
    }
    missing = []
    for rel, why in expect.items():
        src = (ROOT / rel).read_text(encoding="utf-8")
        if "split_texts" not in src:
            missing.append(f"{rel} ({why})")
    assert not missing, "以下位置未使用共用拆分器 split_texts:\n  " + "\n  ".join(missing)
