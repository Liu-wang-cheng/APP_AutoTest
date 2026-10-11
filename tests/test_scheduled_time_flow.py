# -*- coding: utf-8 -*-
"""「预约时间」比对链路: set_time 写期望值 → match 在记录页比对。

★ 起因(2026-10-11): 「预约清扫」用例的 match 里带了 `预约时间`, 但整条用例**没有**
  `grab: 预约时间` 这一步 —— 看上去像漏了 grab(我一度记成"待改用例")。实际不是:
  `set_time` 动作一直把目标时间写进 `runner.store["预约时间"]`
  (core/actions/timer_ops.py, v1.0 起), match 那一步直接拿得到。差点把好用例改坏。

所以这里把链路两头都钉住:
  ① set_time 必须写 store(谁删了那行, 这条就红)
  ② match 必须能用它比对通过; 且 store 为空时**必须报错**(不能静默通过)
  ③ 只有前导零不同(09:35 / 9:35)算同一时刻 —— 真正不同时必须照旧报错
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import core.actions.data_ops as data_ops  # noqa: E402
import core.actions.timer_ops as timer_ops  # noqa: E402
from core.actions.data_ops import _same_clock  # noqa: E402
from core.runner import ActionRunner  # noqa: E402

_PICKER_XML = ('<hierarchy>'
               '<node content-desc="Timer_TimerPicker_Hour" bounds="[100,1400][500,1700]"/>'
               '<node content-desc="Timer_TimerPicker_Minute" bounds="[600,1400][1000,1700]"/>'
               '</hierarchy>')

#: 记录页层级: 状态栏时钟(y=20 ≤ 80, 必须被忽略) + 带日期的记录时间
_RECORD_PAGE = ('<hierarchy>'
                '<node text="9:20" bounds="[12,11][131,60]"/>'
                '<node text="2026-10-11 09:35" bounds="[72,303][1008,444]"/>'
                '</hierarchy>')


class PageDevice:
    """只按需提供 dump_hierarchy / window_size 的设备桩"""
    serial = "fake"

    def __init__(self, xml):
        self._xml = xml

    def dump_hierarchy(self):
        return self._xml

    def window_size(self):
        return (1080, 1920)


def _runner(xml, store=None):
    r = ActionRunner(PageDevice(xml), {"step_interval": 0, "default_timeout": 1,
                                       "click_timeout": 1}, case_name="t")
    r.store.update(store or {})
    return r


# ── ① set_time 写期望值 ──────────────────────────────────────────────

@pytest.mark.parametrize("cur,offset,expect", [
    ("10:00", 3, "10:03"),
    ("09:05", 60, "10:05"),
    ("23:58", 3, "00:01"),      # 跨零点必须环绕, 不能算出 24:01
])
def test_set_time_writes_target_into_store(monkeypatch, cur, offset, expect):
    """set_time 必须把目标时间写进 store['预约时间'] —— 这是那条用例的期望值来源"""
    monkeypatch.setattr(timer_ops.subprocess, "check_output",
                        lambda *a, **k: (cur + "\n").encode())
    monkeypatch.setattr(timer_ops.time, "sleep", lambda s: None)
    set_wheels = []
    monkeypatch.setattr(timer_ops, "adjust_wheel",
                        lambda r, name, cx, target, mod, **kw: set_wheels.append((name, target)))
    r = _runner(_PICKER_XML)
    r._ocr = object()                      # 预置, 跳过 ddddocr 加载

    timer_ops.do_set_time(r, {"set_time": offset, "circular": False})

    assert r.store["预约时间"] == expect
    # 写进 store 的必须**就是滚轮被设到的值**(两边不能各算一套)
    hh, mm = (int(x) for x in expect.split(":"))
    assert set_wheels == [("hour", hh), ("minute", mm)]


# ── ② match 比对(通过 / 必须失败) ────────────────────────────────────

@pytest.fixture
def fast_match(monkeypatch):
    """比对失败时 do_match 会轮询到超时 —— 测试里把窗口压成 0, 只跑一遍"""
    monkeypatch.setattr(data_ops, "MATCH_TIMEOUT", 0)


@pytest.mark.parametrize("page_time", ["2026-10-11 09:35", "2026-10-11 9:35"])
def test_match_compares_scheduled_time_from_store(fast_match, page_time):
    """store 里有期望值(记录页同样显示"9:20"这个状态栏时钟不能顶替它) → 比对通过"""
    r = _runner(_RECORD_PAGE.replace("2026-10-11 09:35", page_time),
                store={"预约时间": "09:35"})
    data_ops.do_match(r, "预约时间")
    assert "一致" in r._last_compare_msg


def test_match_fails_loudly_when_expectation_missing(fast_match):
    """没写过 store 就必须报"没有抓取过", 不能因为取到了记录页时间就算通过"""
    r = _runner(_RECORD_PAGE, store={})
    with pytest.raises(AssertionError, match="没有抓取过"):
        data_ops.do_match(r, "预约时间")


def test_match_still_fails_on_a_different_time(fast_match):
    """★ 反向: 容错只能容忍前导零 —— 时刻真不一样时必须照旧报错(防假通过)"""
    r = _runner(_RECORD_PAGE.replace("09:35", "10:35"), store={"预约时间": "09:35"})
    with pytest.raises(AssertionError, match="数据不一致"):
        data_ops.do_match(r, "预约时间")


# ── ③ 前导零容错本身 ────────────────────────────────────────────────

@pytest.mark.parametrize("a,b,same", [
    ("09:35", "09:35", True),
    ("09:35", "9:35", True),        # 只差前导零 = 同一时刻
    ("9:35", "09:35", True),
    ("09:35", "10:35", False),      # 不许把不同时刻判成一样
    ("09:35", "", False),
    ("", "", False),                # 空值不算"相同"(空断言的老毛病)
    ("abc", "09:35", False),
])
def test_same_clock_only_tolerates_leading_zeros(a, b, same):
    assert _same_clock(a, b) is same
