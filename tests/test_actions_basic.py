# -*- coding: utf-8 -*-
"""basic 动作: click 坐标/文本/优先级, swipe 映射, if_click, back。FakeDevice 无设备。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import core.actions.basic  # noqa: F401 触发注册
from core import registry as reg
from core.runner import ActionRunner


class FakeElement:
    def __init__(self, exists=True, bounds=None):
        self._exists = exists
        self.bounds = bounds or {"left": 0, "top": 0, "right": 100, "bottom": 50}
        self.clicked = False

    def exists(self, timeout=1):
        return self._exists

    def click(self):
        self.clicked = True

    @property
    def info(self):
        return {"bounds": self.bounds}


class FakeDevice:
    serial = "fake"

    def __init__(self, present=("确认",)):
        self.present = set(present)
        self.last_kw = {}
        self.clicked_at = None
        self.pressed = None
        self.swiped = None

    def info(self):
        return {}

    def dump_hierarchy(self):
        # 判断类(if 条件/wait_for/wait_loading)现在读层级: 桩把 present 体现在 XML 里
        return "".join(f'<node text="{p}"/>' for p in sorted(self.present))

    def window_size(self):
        return (1080, 1920)

    def click(self, x, y):
        self.clicked_at = (x, y)

    def press(self, key):
        self.pressed = key

    def swipe(self, sx, sy, ex, ey, duration=None):
        self.swiped = (sx, sy, ex, ey)

    def __call__(self, **kw):
        self.last_kw = kw
        # textContains/text/description 命中 present 集合
        for k in ("text", "textContains", "description"):
            if k in kw:
                return FakeElement(exists=kw[k] in self.present)
        return FakeElement(exists=False)


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    """swipe 后会 runner._sleep(1)。

    只换 _sleep 方法本身,**不能**去 patch time.sleep —— _sleep 靠
    time.sleep 推进时间,替换成空操作后 remain 永不归零会死循环。
    """
    monkeypatch.setattr(ActionRunner, "_sleep", lambda self, s: None)


@pytest.fixture
def runner():
    return ActionRunner(FakeDevice(), {"step_interval": 0, "default_timeout": 1,
                                       "click_timeout": 1}, case_name="t")


def test_click_coordinates(runner):
    runner._execute({"click": [531, 1900]})
    assert runner.d.clicked_at == (531, 1900)


def test_click_text(runner):
    runner._execute({"click": "确认"})
    # 引擎对非 "=" 前缀走 textContains(找不到才回退 description),不是精确 text
    assert runner.d.last_kw.get("textContains") == "确认"


def test_back_presses_key(runner):
    runner._execute({"back": True})
    assert runner.d.pressed == "back"


def test_swipe_direction_left(runner):
    runner._execute({"swipe": "left"})
    sx, sy, ex, ey = runner.d.swiped
    assert ex < sx and sy == ey


def test_swipe_fast_left(runner):
    runner._execute({"swipe": "fast-left"})
    sx, sy, ex, ey = runner.d.swiped
    # 从中心向两侧展开: 起点在中心右侧、终点在中心左侧,行程 = 2 * (屏宽 * 0.3)
    w = 1080
    dist = int(w * 0.3)
    assert (sx, ex) == (w // 2 + dist, w // 2 - dist)
    assert sy == ey


def test_swipe_quad(runner):
    runner._execute({"swipe": [82, 2101, 82, 1601]})
    assert runner.d.swiped == (82, 2101, 82, 1601)


def test_swipe_bad_direction(runner):
    with pytest.raises(ValueError):
        runner._execute({"swipe": "diagonal"})


def test_if_click_hits_first_present(runner):
    runner._execute({"if_click": "不存在,确认"})
    assert runner.d.last_kw.get("textContains") == "确认"


def test_if_click_absent_is_not_error(runner):
    runner._execute({"if_click": "不存在的按钮"})   # 不抛异常即通过


def test_find_click_raises_when_none(runner):
    with pytest.raises(AssertionError):
        runner._execute({"find_click": "全都没有1,全都没有2"})


def test_click_priority_over_assert(runner):
    """同一步里 click(100) 应先于 assert(95) 命中并屏蔽它"""
    assert reg.ACTIONS["click"].priority < reg.ACTIONS["assert"].priority
    order = [a.key for a in reg.dispatch_order()]
    assert order.index("click") < order.index("assert")


def test_if_else_branch_runs_when_condition_absent(runner):
    """条件不成立 → 执行 else 分支(源项目反直觉语义)"""
    runner.steps_hit = []
    runner._execute({
        "if": "不存在的文本",
        "else": [{"desc": "else步骤1", "click": [10, 20]}],
    })
    assert runner.d.clicked_at == (10, 20)
    assert runner._pending_sub_results[0]["desc"] == "else步骤1"


def test_if_branch_skips_when_condition_present(runner):
    """条件成立 → 直接跳过,不执行 else"""
    runner._execute({
        "if": "确认",
        "else": [{"desc": "不该执行", "click": [99, 99]}],
    })
    assert runner.d.clicked_at is None
