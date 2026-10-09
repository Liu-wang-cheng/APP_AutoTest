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


def test_map_ops_文本判断刻意不接OCR兜底():
    """★ 守护一个**有意为之的决策**: map_ops 的文本判断不使用 _text_present。

    理由(见 core/actions/map_ops.py 模块 docstring):
      · 这些动作跑在涂鸦智能的**原生页面**, 无障碍树正常, 用不着 OCR 兜底
        (那是为 SmartThings 插件页"整页读不到文本"设计的);
      · 而 _text_present 每次判断都要多 dump 一次层级, 本模块的测试桩用 dump
        次数驱动状态机 —— 接上去会让阶段错乱、判断永远不成立
        (实测: test_spot_clean 从 3 秒涨到 77 秒 + 5 个用例失败)。

    这条不是"实现细节不可改", 而是提醒: 要改请先读上面那段理由, 并准备好
    重做那些靠 dump 次数驱动状态的桩。
    """
    import ast
    import io
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = io.open(os.path.join(root, "core", "actions", "map_ops.py"),
                  encoding="utf-8").read()
    # ★ 用 AST 只看**真实调用**, 不看 docstring/注释 —— 模块 docstring 里正是
    #   用 "_text_present" 这个词来解释"为什么不接它", 纯文本匹配会误伤自己
    calls = [n for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.Attribute) and n.attr == "_text_present"]
    assert not calls, (
        "map_ops 里出现了 _text_present —— 若确实要接 OCR 兜底, 请先读 "
        "core/actions/map_ops.py 模块 docstring 记的两条理由, 并同步重做 "
        "tests/test_spot_clean.py 里靠 dump 次数驱动状态机的桩。")
