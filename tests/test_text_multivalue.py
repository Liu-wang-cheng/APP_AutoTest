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

import numpy as np
import pytest

from core.driver import split_texts
from core.runner import ActionRunner

ROOT = Path(__file__).resolve().parent.parent


# ── 多值断言的**执行**语义(2026-10-11: 只守分隔符不够, 要看真的能跑) ──

class _Dev:
    """页面文本固定几条的设备桩(≥5 条 → 判断类不走 OCR 兜底)"""
    serial = "fake"

    def __init__(self, texts=()):
        self.texts = list(texts) + ["填充%d" % i for i in range(5)]

    def dump_hierarchy(self):
        return "".join(f'<node text="{t}"/>' for t in self.texts)

    def screenshot(self, path=None, format=None):
        return np.zeros((1920, 1080, 3), dtype=np.uint8)

    def window_size(self):
        return (1080, 1920)

    def __call__(self, **kw):
        q = kw.get("textContains") or kw.get("text") or ""
        hit = bool(q) and any(q in t for t in self.texts)

        class R:
            def exists(self, timeout=1):
                return hit

            def click(self):
                pass
        return R()


def _runner(texts=()):
    r = ActionRunner(_Dev(texts), {"step_interval": 0, "default_timeout": 1,
                                   "click_timeout": 1}, case_name="t")
    r._sleep = lambda s: None
    return r


@pytest.mark.parametrize("value", ["清洁中,正在吸尘", "清洁中，正在吸尘", "清洁中、正在吸尘",
                                   "清洁中;正在吸尘", "清洁中\n正在吸尘"])
def test_multivalue_assert_passes_when_any_value_matches(value):
    """★ 多值断言 = **任一命中即通过**(第二个值命中也要通过)"""
    _runner(("正在吸尘",))._execute({"assert": value, "timeout": 1})


def test_multivalue_assert_fails_loudly_when_none_matches():
    with pytest.raises(AssertionError) as e:
        _runner(("正在吸尘",))._execute({"assert": "清洁中，已暂停", "timeout": 1})
    assert "清洁中" in str(e.value) and "已暂停" in str(e.value), str(e.value)


def test_generic_candidate_matches_anywhere_on_the_page():
    """★ 这是**语义, 不是 bug**, 但必须知道它: 断言是"页面上任意文本包含该值就算命中"。

    所以 `assert: 干燥拖布,已完成,已取消` 里的「已完成」会被**任何**含"已完成"的文本顶掉
    —— 真机上「清洗拖布 已完成」这种进度文案就让断言**瞬间通过**, 与清扫是否结束无关
    (2026-10-11 用户报"第二个断言没有生效", 就是这个)。候选词不能太通用。
    """
    _runner(("清洗拖布 已完成",))._execute({"assert": "干燥拖布,已完成,已取消", "timeout": 1})


@pytest.mark.parametrize("value", ["干燥拖布,完成.png", "完成.png,干燥拖布"])
def test_assert_mixing_a_template_no_longer_crashes(value, monkeypatch):
    """★ 多值里混模板图: 以前「图片在最后」会被整串当成模板文件名 → FileNotFoundError;
    「图片在前」则把图名当文本找(静默忽略)。现在按多值语义: 文本命中也算通过。"""
    _runner(("干燥拖布",))._execute({"assert": value, "timeout": 1})


def test_assert_in_multivalue_can_match_by_template(monkeypatch):
    """多值里的模板候选也要真的能命中(文本都不在时)"""
    from core import vision
    from core.actions import asserts
    monkeypatch.setattr(vision, "find_in_gray", lambda s, t, **kw: (300, 400))
    monkeypatch.setattr(vision, "resolve_template", lambda n: n)
    monkeypatch.setattr(asserts.time, "sleep", lambda s: None)
    _runner(("正在吸尘",))._execute({"assert": "干燥拖布,完成.png", "timeout": 1})


def test_assert_none_of_text_or_template_reports_all_candidates(monkeypatch):
    from core import vision
    monkeypatch.setattr(vision, "find_in_gray", lambda s, t, **kw: None)
    monkeypatch.setattr(vision, "resolve_template", lambda n: n)
    with pytest.raises(AssertionError) as e:
        _runner(("正在吸尘",))._execute({"assert": "干燥拖布,完成.png", "timeout": 1})
    assert "干燥拖布" in str(e.value) and "完成.png" in str(e.value), str(e.value)

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
