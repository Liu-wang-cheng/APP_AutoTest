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


# ── 未渲染的隐藏节点不算"看到"/"点到"(2026-10-11 真机假通过的根治) ──

import re as _re

_NODE = _re.compile(r'<node text="([^"]*)" bounds="\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]"')

#: 真机现场(2026-10-11 12:31 的截图 + 层级): 页面只有「正在清洁」等可见文本,
#: 而收起后的清洗拖布面板在无障碍树里**留了个未渲染的节点**(bounds 全 0)
_XML_HIDDEN_RESIDUE = (
    '<hierarchy>'
    '<node text="正在清洁" bounds="[400,300][680,370]"/>'
    '<node text="填充0" bounds="[10,10][20,20]"/>'
    '<node text="填充1" bounds="[10,30][20,40]"/>'
    '<node text="填充2" bounds="[10,50][20,60]"/>'
    '<node text="填充3" bounds="[10,70][20,80]"/>'
    '<node text="清洗拖布 已完成" bounds="[0,0][0,0]"/>'
    '</hierarchy>')


class _R:
    def __init__(self, present, bounds=None, clickable=True):
        self._present, self._bounds, self._clickable = present, bounds, clickable

    def exists(self, timeout=1):
        return self._present

    def click(self):
        pass

    @property
    def info(self):
        return {"clickable": self._clickable, "bounds": self._bounds or {},
                "text": "", "className": "android.view.View"}


class _BoundsDev:
    """按 xml 回答元素查询(**带 bounds**) —— 模拟真机: 隐藏节点也能被 u2 查到"""
    serial = "fake"

    def __init__(self, xml):
        self._xml = xml

    def dump_hierarchy(self):
        return self._xml

    def screenshot(self, path=None, format=None):
        return np.zeros((1920, 1080, 3), dtype=np.uint8)

    def window_size(self):
        return (1080, 1920)

    def __call__(self, **kw):
        q = kw.get("textContains") or kw.get("text") or ""
        for text, x1, y1, x2, y2 in _NODE.findall(self._xml):
            if q and q in text:
                return _R(True, {"left": int(x1), "top": int(y1),
                                 "right": int(x2), "bottom": int(y2)})
        return _R(False, None)


def _bounds_runner(xml):
    r = ActionRunner(_BoundsDev(xml), {"step_interval": 0, "default_timeout": 1,
                                       "click_timeout": 1}, case_name="t")
    r._sleep = lambda s: None
    r._poll_sleep = lambda n: None
    return r


def test_assert_ignores_unrendered_hidden_node():
    """★ 真机现场: 断言候选「已完成」只出现在**未渲染**的残留节点里 → **不算通过**。

    改前就是这里判通过(截图上页面只有「正在清洁」), 于是"判断是否清扫完成"形同虚设。
    """
    with pytest.raises(AssertionError):
        _bounds_runner(_XML_HIDDEN_RESIDUE)._execute(
            {"assert": "干燥拖布,已完成,已取消", "timeout": 1})


def test_assert_still_matches_when_that_node_is_visible():
    """对照: 同一个节点只要是**可见**的(bounds 正常), 断言该怎么通过还怎么通过"""
    visible = _XML_HIDDEN_RESIDUE.replace('bounds="[0,0][0,0]"', 'bounds="[100,900][600,960]"')
    _bounds_runner(visible)._execute({"assert": "干燥拖布,已完成,已取消", "timeout": 1})


def test_click_does_not_click_unrendered_node():
    """点击同理: 隐藏节点点不动(点了也没效果) → 继续等, 等不到就报超时"""
    with pytest.raises(AssertionError, match="超时"):
        _bounds_runner(_XML_HIDDEN_RESIDUE)._execute({"click": "已完成", "timeout": 1})


def test_text_present_detail_reports_source_and_position():
    """判断详情: 来源(节点文本/描述/OCR) + 命中的原文 + 坐标 —— 假通过排查全靠它"""
    from core import session
    hit, src, raw, pos = session.text_present_detail(
        None, ["干燥拖布", "正在清洁"], xml=_XML_HIDDEN_RESIDUE)
    assert (hit, src, raw) == ("正在清洁", "text", "正在清洁")
    assert pos == (540, 335), pos
    # 隐藏节点里的词(已完成)不该被算命中
    assert session.text_present_detail(None, ["已完成"], xml=_XML_HIDDEN_RESIDUE)[0] is None


def test_desc_only_hit_is_reported_as_desc():
    """只出现在**无障碍描述**里的命中, 来源要写成 desc(真机上出现过图标按钮顶掉断言)"""
    from core import session
    xml = ('<hierarchy><node text="填充0"/>'
           '<node content-desc="暂停清扫" bounds="[900,300][1020,420]"/></hierarchy>')
    hit, src, raw, pos = session.text_present_detail(None, ["暂停"], xml=xml)
    assert (hit, src, raw) == ("暂停", "desc", "暂停清扫"), (hit, src, raw)
    assert pos == (960, 360), pos


def test_visible_nodes_skips_zero_bounds_only():
    """过滤只针对"未渲染"(bounds 全 0); 没有 bounds 属性的节点**保持原样**(宁可不误杀)"""
    from core import session
    xml = ('<hierarchy><node text="甲" bounds="[0,0][0,0]"/>'
           '<node text="乙" bounds="[1,1][2,2]"/><node text="丙"/></hierarchy>')
    texts = [t for t, _d, _p in session.visible_nodes(xml)]
    assert texts == ["乙", "丙"], texts


def test_exact_prefix_is_strict_and_blocks_desc_hits():
    """★ 提示里承诺的写法要真管用: `=` 前缀 = 整段文本相等, 且**不看图标描述、不走 OCR**。

    B 方案(2026-10-11 用户定)下, 这就是"我要严格"的正规用法 —— 没有开关, 靠它在用例里表达。
    """
    xml = ('<hierarchy>'
           '<node text="正在清洁" bounds="[400,300][680,370]"/>'
           '<node text="填充0" bounds="[10,10][20,20]"/>'
           '<node text="填充1" bounds="[10,30][20,40]"/>'
           '<node text="填充2" bounds="[10,50][20,60]"/>'
           '<node text="填充3" bounds="[10,70][20,80]"/>'
           '<node content-desc="暂停清扫" bounds="[900,300][1020,420]"/>'
           '</hierarchy>')
    # 不带 = : 会被"暂停清扫"这个**描述**顶通过(现状语义)
    _bounds_runner(xml)._execute({"assert": "暂停", "timeout": 1})
    # 带 = : 严格 → 描述不算, 页面文本里也没有整段等于"暂停"的 → 必须失败
    with pytest.raises(AssertionError):
        _bounds_runner(xml)._execute({"assert": "=暂停", "timeout": 1})
