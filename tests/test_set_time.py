# -*- coding: utf-8 -*-
"""set_time 滚轮逻辑:自适应探测(分钟轮划不动时加大距离)、计数滑动、最终校验。

用虚拟滚轮仿真:滑动按 35px/格 改变数值;"粘滞滚轮"忽略低于阈值的轻扫
(复现勿扰分钟轮 35px 划不动的现象);OCR 返回仿真当前值。
"""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import core.actions.timer_ops as timer_ops  # noqa: E402
from core.runner import ActionRunner  # noqa: E402

_PICKER_XML = ('<hierarchy>'
               '<node content-desc="Timer_TimerPicker_Hour" bounds="[100,1400][500,1700]"/>'
               '<node content-desc="Timer_TimerPicker_Minute" bounds="[600,1400][1000,1700]"/>'
               '</hierarchy>')


class WheelDevice:
    """虚拟时间滚轮:35px/格;粘滞阈值以下的轻扫不响应(复现分钟轮划不动)"""
    serial = "fake"

    def __init__(self, start, sticky_below=0, grid=35):
        self.value = start
        self.grid = grid
        self.sticky_below = sticky_below
        self.swipes = []
        self._xml = _PICKER_XML

    def dump_hierarchy(self):
        return self._xml

    def swipe(self, x1, y1, x2, y2, duration=None):
        self.swipes.append((x1, y1, x2, y2, duration))
        drag = y1 - y2                     # 上滑为正
        if abs(drag) < self.sticky_below:
            return                         # 轻扫太短,滚轮不响应
        grids = int(abs(drag) // self.grid)
        if grids == 0:
            return
        step = 1 if drag > 0 else -1       # 上滑=数值增大
        self.value = (self.value + step * grids) % 60

    def window_size(self):
        return (1080, 1920)

    def screenshot(self, path=None, format=None):
        return np.zeros((1920, 1080, 3), dtype=np.uint8)


class FakeOCR:
    """OCR 直接读仿真滚轮的当前值"""
    def __init__(self, device):
        self.device = device

    def classification(self, img):
        return str(self.device.value)


def _runner(device):
    r = ActionRunner(device, {"step_interval": 0, "default_timeout": 1,
                              "click_timeout": 1}, case_name="t")
    r._ocr = FakeOCR(device)               # 预置,跳过 ddddocr 加载
    return r


def test_sticky_wheel_found_by_adaptive_probe(monkeypatch):
    """分钟轮 35px 划不动 → 探测自动加大距离找到 70px,仍能精确到位"""
    monkeypatch.setattr(timer_ops.time, "sleep", lambda s: None)
    dev = WheelDevice(start=50, sticky_below=40)     # ≥40px 才响应
    r = _runner(dev)
    timer_ops.adjust_wheel(r, "minute", cx=800, target=5, mod=60,
                           step=35, crop_w=30, circular=False)
    assert abs(dev.value - 5) <= 1, f"最终值 {dev.value},距目标 5 超过 ±1"
    # 用过的滑动必须带 duration(慢速拖动是滚轮响应的前提)
    assert dev.swipes and all(s[4] == 0.35 for s in dev.swipes)


def test_responsive_wheel_counts_exact(monkeypatch):
    """正常滚轮:探测 35px/格 → 计数滑动精确到目标值"""
    monkeypatch.setattr(timer_ops.time, "sleep", lambda s: None)
    dev = WheelDevice(start=50)
    r = _runner(dev)
    timer_ops.adjust_wheel(r, "minute", cx=800, target=15, mod=60,
                           step=35, crop_w=30, circular=False)
    assert dev.value == 15, f"最终值 {dev.value},应为 15"


def test_stuck_wheel_raises_instead_of_silent_pass(monkeypatch):
    """滚轮完全划不动:最终校验必须报错——静默放行会拿错误时间点确认"""
    monkeypatch.setattr(timer_ops.time, "sleep", lambda s: None)
    dev = WheelDevice(start=50, sticky_below=9999)   # 全部轻扫都不响应
    r = _runner(dev)
    with pytest.raises(RuntimeError, match="未能设置到 05"):
        timer_ops.adjust_wheel(r, "minute", cx=800, target=5, mod=60,
                               step=35, crop_w=30, circular=False)
