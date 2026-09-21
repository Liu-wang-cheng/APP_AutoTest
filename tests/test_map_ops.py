# -*- coding: utf-8 -*-
"""map_ops: 合并候选对按几何相邻排序 / room_click 游标语义。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import core.actions.map_ops  # noqa: F401
from core.actions.map_ops import merge_pair_order, zone_pair_gap
from core.runner import ActionRunner


class FakeDevice:
    serial = "fake"

    def __init__(self):
        self.clicks = []

    def info(self):
        return {}

    def click(self, x, y):
        self.clicks.append((x, y))


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    """room_click 每次点击后 time.sleep(2) —— 短路掉,否则连点 3 个分区要等 6 秒。

    这里 patch 全局 time.sleep 是安全的: 本文件的用例不走 runner._sleep
    (那条路径依赖 time.sleep 推进时间,patch 后会死循环)。
    """
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda s: None)
    monkeypatch.setattr(ActionRunner, "_sleep", lambda s, sec: None)


@pytest.fixture
def runner():
    return ActionRunner(FakeDevice(), {"step_interval": 0, "default_timeout": 1,
                                       "click_timeout": 1}, case_name="t")


def test_zone_pair_gap_horizontal():
    assert zone_pair_gap((0, 0, 100, 100), (200, 0, 300, 100)) == 100


def test_zone_pair_gap_overlap_is_zero():
    assert zone_pair_gap((0, 0, 100, 100), (50, 0, 150, 100)) == 0


def test_merge_pair_sorted_by_gap():
    zones = [(100, 100, 5), (900, 900, 4), (120, 110, 3)]
    boxes = [(50, 50, 200, 200), (800, 800, 1000, 1000), (60, 60, 220, 220)]
    pairs = merge_pair_order(zones, boxes)
    assert pairs[0] == (0, 2)          # 几何相邻的对排最前


def test_merge_pair_order_fallback_without_boxes():
    """没有外框信息时退回原枚举顺序,行为不变"""
    zones = [(0, 0, 1), (1, 1, 1), (2, 2, 1)]
    pairs = merge_pair_order(zones, [])
    assert pairs == [(0, 1), (0, 2), (1, 2)]


def test_room_click_sequential_cursor(runner):
    """count<=分区数: 按游标依次点单个"""
    runner._stored_zones = [(10, 10), (20, 20), (30, 30)]
    runner._next_room_idx = 0
    runner._execute({"room_click": 1})
    runner._execute({"room_click": 1})
    assert runner.d.clicks == [(10, 10), (20, 20)]
    assert runner._next_room_idx == 2


def test_room_click_overflow_clicks_remaining(runner):
    """count>分区数: 连点剩余全部"""
    runner._stored_zones = [(10, 10), (20, 20), (30, 30)]
    runner._next_room_idx = 0
    runner._execute({"room_click": 99})
    assert runner.d.clicks == [(10, 10), (20, 20), (30, 30)]
    assert runner._next_room_idx == 3


def test_room_click_no_zones_raises(runner):
    runner._stored_zones = []
    with pytest.raises(RuntimeError, match="未识别到任何房间分区"):
        runner._execute({"room_click": 1})


def test_room_click_exhausted_raises(runner):
    runner._stored_zones = [(10, 10)]
    runner._next_room_idx = 1
    with pytest.raises(RuntimeError, match="无未点击分区可用"):
        runner._execute({"room_click": 1})
