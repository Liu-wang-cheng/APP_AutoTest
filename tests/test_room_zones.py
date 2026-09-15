# -*- coding: utf-8 -*-
"""_do_room_zones 的失败诊断

识别不出分区时(地图没加载 / 深色主题 / 灰度地图 / 房间还没划分),
原来报的是一段 NameError —— f-string 里引用了两个不存在的变量,
把真正该看到的诊断信息全吞了。识别失败恰恰是最需要诊断信息的时刻。
"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.action_runner import ActionRunner


def make_runner(screen):
    class FakeD:
        serial = "fake"

        def screenshot(self, path=None, format=None):
            return screen

    r = ActionRunner.__new__(ActionRunner)
    r.d = FakeD()
    r.case_name = "room_zones"
    r.store = {}
    r._stored_zones = []
    r._next_room_idx = 0
    r._last_compare_msg = None
    r._debug_map_path = None
    return r


def solid(bgr):
    return np.full((1920, 1080, 3), bgr, dtype=np.uint8)


class TestNoZoneFound:
    """识别不到分区时的报错行为"""

    def test_纯灰地图_报RuntimeError而非NameError(self):
        """灰度地图没有色调峰值 —— 这正是最深色主题/未加载场景"""
        r = make_runner(solid((128, 128, 128)))
        with pytest.raises(RuntimeError, match="未识别到房间分区"):
            r._do_room_zones()

    def test_纯白地图_报RuntimeError(self):
        r = make_runner(solid((255, 255, 255)))
        with pytest.raises(RuntimeError, match="未识别到房间分区"):
            r._do_room_zones()

    def test_纯黑地图_报RuntimeError(self):
        r = make_runner(solid((0, 0, 0)))
        with pytest.raises(RuntimeError, match="未识别到房间分区"):
            r._do_room_zones()

    def test_报错带诊断计数(self):
        """错误信息要能区分"没找到颜色"和"找到颜色但面积太小"""
        r = make_runner(solid((128, 128, 128)))
        with pytest.raises(RuntimeError) as ei:
            r._do_room_zones()
        msg = str(ei.value)
        assert "彩色像素" in msg, f"缺少彩色像素计数: {msg}"
        assert "色调峰值" in msg, f"缺少峰值计数: {msg}"

    def test_识别失败不污染已存分区(self):
        """识别失败时不应改动 _stored_zones / 游标"""
        r = make_runner(solid((128, 128, 128)))
        r._stored_zones = [(11, 22)]
        r._next_room_idx = 3
        with pytest.raises(RuntimeError):
            r._do_room_zones()
        assert r._stored_zones == [(11, 22)]
        assert r._next_room_idx == 3
