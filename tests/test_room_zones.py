# -*- coding: utf-8 -*-
"""room_zones 识别失败时的诊断信息。

识别不出分区是最需要诊断信息的时刻(地图没加载 / 深色主题 / 灰度地图 /
房间还没划分),这些场景下报错必须给出可分辨的线索:彩色像素数、色调峰值数、
轮廓数分别指向不同的原因。这里守住这条 —— 不能因为错误处理本身的问题
(比如 f-string 引用了不存在的变量)把真因吞掉。
"""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import core.actions.map_ops  # noqa: F401
from core.runner import ActionRunner


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    """room_zones 识别不到时会重试等待 —— 测试里跳过真实 sleep,保持秒级"""
    monkeypatch.setattr(ActionRunner, "_sleep", lambda self, s: None)


def solid(bgr):
    """整屏纯色 —— 没有色调峰值,正是灰度/深色/未加载地图的样子"""
    return np.full((1920, 1080, 3), bgr, dtype=np.uint8)


class FakeDevice:
    serial = "fake"

    def __init__(self, screen):
        self._screen = screen

    def info(self):
        return {}

    def screenshot(self, path=None, format=None):
        return self._screen.copy()


def make_runner(screen):
    return ActionRunner(FakeDevice(screen),
                        {"step_interval": 0, "default_timeout": 1, "click_timeout": 1},
                        case_name="room_zones")


@pytest.mark.parametrize("bgr", [(128, 128, 128), (255, 255, 255), (0, 0, 0)])
def test_纯色地图报RuntimeError(bgr):
    """灰度/白/黑地图都要报 RuntimeError,而不是别的异常类型"""
    runner = make_runner(solid(bgr))
    with pytest.raises(RuntimeError, match="未识别到房间分区"):
        runner._execute({"room_zones": True})


def test_报错带诊断计数():
    """错误信息要能区分「没找到颜色」和「找到颜色但面积太小」"""
    runner = make_runner(solid((128, 128, 128)))
    with pytest.raises(RuntimeError) as ei:
        runner._execute({"room_zones": True})
    msg = str(ei.value)
    assert "彩色像素" in msg, f"缺少彩色像素计数: {msg}"
    assert "色调峰值" in msg, f"缺少峰值计数: {msg}"


def test_识别失败不污染已存分区():
    """识别失败时不能改动 _stored_zones 和游标,否则后续步骤会点到旧坐标"""
    runner = make_runner(solid((128, 128, 128)))
    runner._stored_zones = [(11, 22)]
    runner._next_room_idx = 3
    with pytest.raises(RuntimeError):
        runner._execute({"room_zones": True})
    assert runner._stored_zones == [(11, 22)]
    assert runner._next_room_idx == 3


def test_识别成功重置游标(tmp_path, monkeypatch):
    """识别成功后游标归零 —— 否则沿用上一轮的位置会漏点房间"""
    import cv2
    # 造一张有彩色房间块的地图: 中部两个色块
    screen = np.full((1920, 1080, 3), 240, dtype=np.uint8)
    cv2.rectangle(screen, (200, 600), (400, 800), (60, 60, 200), -1)   # 红
    cv2.rectangle(screen, (600, 600), (800, 800), (200, 60, 60), -1)   # 蓝

    runner = make_runner(screen)
    runner._next_room_idx = 5
    monkeypatch.setattr("core.driver.BASE_DIR", str(tmp_path))

    runner._execute({"room_zones": True})
    assert runner._stored_zones, "应识别出至少一个分区"
    assert runner._next_room_idx == 0
