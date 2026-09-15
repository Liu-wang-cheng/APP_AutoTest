# -*- coding: utf-8 -*-
"""房间合并的候选对排序

_do_merge_zones 原来盲枚举 (i,j) 对,直到撞上相邻的那一对。
每次失败尝试要付 2 次点击 + 5 次 sleep + 重进合并模式,约 10 秒;
6 个房间的最坏情况是 15 对 × 10s ≈ 2.5 分钟,而且期间一直在动地图。

只有几何上相邻的房间才能合并,所以按分区框的边距升序排候选对,
第一对就基本是对的。验证逻辑不变,只改尝试顺序。
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.action_runner import ActionRunner


class FakeEl:
    def __init__(self, dev, kw):
        self.dev = dev
        self.kw = kw

    def _key(self):
        for k in ("text", "textContains", "description"):
            if k in self.kw:
                return self.kw[k]
        return ""

    def exists(self, timeout=0):
        return self.dev._exists(self._key())

    def click(self):
        self.dev.actions.append(("click", self._key()))


class FakeD:
    """模拟合并模式:只有 ok_pair 指定的那对分区能合并成功"""

    def __init__(self, zones, ok_pair):
        self.zones = zones
        self.ok_pair = set(ok_pair)
        self.clicks = []
        self.actions = []
        self._picked = []

    def __call__(self, **kw):
        return FakeEl(self, kw)

    def click(self, x, y):
        self.clicks.append((x, y))
        if (x, y) in self.zones:
            self._picked.append(self.zones.index((x, y)))
            self._picked = self._picked[-2:]

    def window_size(self):
        return (1080, 1920)

    def _exists(self, key):
        picked = set(self._picked)
        if key == "合并":
            return len(self._picked) == 2
        if key == "提示":                       # 不相邻提示
            return len(self._picked) == 2 and picked != self.ok_pair
        if key in ("请选择", "区域分割"):        # 成功标志
            return picked == self.ok_pair
        return False                            # 加载中/确认等一律不存在

    @property
    def tried_pairs(self):
        """从点击序列还原尝试过的分区对

        要过滤掉非分区点击:不相邻时会走 _click_dialog_confirm 的兜底,
        往屏幕中部点一下,那不是候选对的一部分。
        """
        picked = [self.zones.index(c) for c in self.clicks if c in self.zones]
        return [(picked[n], picked[n + 1]) for n in range(0, len(picked) - 1, 2)]


def make_runner(d):
    r = ActionRunner.__new__(ActionRunner)
    r.d = d
    r.case_name = "merge"
    r.store = {}
    r._stored_zones = []
    r._stored_zone_boxes = []
    r._next_room_idx = 0
    r._last_compare_msg = None
    r._trace = []
    r._trace_limit = 0
    return r


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *a: None)


ZONES = [(100, 100), (900, 100), (500, 900), (120, 140)]
BOXES = [(60, 60, 140, 140),          # 0
         (860, 60, 940, 140),         # 1
         (460, 860, 540, 940),        # 2
         (160, 60, 240, 140)]         # 3 —— 与 0 相邻


class TestPairGap:
    def test_相邻_边贴边_间距为0(self):
        assert ActionRunner._zone_pair_gap((0, 0, 10, 10), (10, 0, 20, 10)) == 0

    def test_重叠_间距为0(self):
        assert ActionRunner._zone_pair_gap((0, 0, 20, 20), (5, 5, 25, 25)) == 0

    def test_水平分离_取水平边距(self):
        assert ActionRunner._zone_pair_gap((0, 0, 10, 10), (30, 0, 40, 10)) == 20

    def test_斜对角_取欧氏距离(self):
        gap = ActionRunner._zone_pair_gap((0, 0, 10, 10), (13, 14, 20, 20))
        assert abs(gap - 5.0) < 1e-6      # dx=3, dy=4


class TestPairOrder:
    def test_相邻对排在最前(self):
        r = make_runner(None)
        order = r._merge_pair_order(ZONES, BOXES)
        assert set(order[0]) == {0, 3}

    def test_覆盖所有组合(self):
        r = make_runner(None)
        order = r._merge_pair_order(ZONES, BOXES)
        assert len(order) == 6            # C(4,2)
        assert {frozenset(p) for p in order} == {
            frozenset(p) for p in [(0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3)]}

    def test_无框信息_退化为原顺序(self):
        """没有 bbox 时不能报错,退回 (i,j) 枚举顺序"""
        r = make_runner(None)
        assert r._merge_pair_order(ZONES, []) == [
            (0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3)]

    def test_框数量不符_退化为原顺序(self):
        r = make_runner(None)
        assert r._merge_pair_order(ZONES, [(0, 0, 1, 1)]) == [
            (0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3)]


class TestMergeTriesAdjacentFirst:
    def test_相邻对第一个尝试(self):
        """(0,3) 相邻 —— 修复前会先试 (0,1)(0,2),现在应该第一对就中"""
        d = FakeD(ZONES, ok_pair=(0, 3))
        r = make_runner(d)
        r._stored_zones = ZONES
        r._stored_zone_boxes = BOXES

        r._do_merge_zones()

        assert d.tried_pairs[0] == (0, 3), f"实际尝试顺序: {d.tried_pairs}"

    def test_尽量少尝试(self):
        d = FakeD(ZONES, ok_pair=(0, 3))
        r = make_runner(d)
        r._stored_zones = ZONES
        r._stored_zone_boxes = BOXES

        r._do_merge_zones()

        assert len(d.tried_pairs) == 1, f"多试了: {d.tried_pairs}"

    def test_无框信息时仍能完成(self):
        """旧数据路径(没有 bbox)必须保持可用"""
        d = FakeD(ZONES, ok_pair=(0, 3))
        r = make_runner(d)
        r._stored_zones = ZONES
        r._stored_zone_boxes = []

        r._do_merge_zones()          # 不应抛异常

        assert (0, 3) in d.tried_pairs

    def test_少于两个分区_报错(self):
        d = FakeD(ZONES, ok_pair=(0, 3))
        r = make_runner(d)
        r._stored_zones = [(100, 100)]
        with pytest.raises(RuntimeError, match="至少 2 个分区"):
            r._do_merge_zones()
