# -*- coding: utf-8 -*-
"""spot_clean:点位不可达自动换分区;成功判据=面积或时长>0;点击点吸附进色块。

时序(用户指定):开始清扫 → 收面板(面板开着读不到清扫状态) → 验证状态 →
等 60s → 抓面积/时长判定;>0 结束重试循环继续往下,全 0 换下一个分区。
不做等回充。
"""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import core.actions.map_ops as map_ops  # noqa: E402
from core.runner import ActionRunner  # noqa: E402


# ── 点击点吸附 ──

def test_snap_inside_keeps_interior_point():
    mask = np.zeros((100, 100), np.uint8)
    mask[40:60, 40:60] = 255
    assert map_ops._snap_inside(mask, 50, 50) == (50, 50)


def test_snap_inside_pulls_blank_centroid_into_block():
    """L 形色块的质心附近是空白 —— 必须吸附到块内最近像素"""
    mask = np.zeros((100, 100), np.uint8)
    mask[80:100, 0:100] = 255      # 底部横条
    mask[0:100, 0:20] = 255        # 左侧竖条
    sx, sy = map_ops._snap_inside(mask, 50, 50)
    assert mask[sy, sx], "吸附后仍在色块外"
    assert sx < 25 or sy > 75      # 落到了最近的横条或竖条上


def test_ensure_point_keeps_floor_point():
    """点位已在色块内:原样返回"""
    frame = np.zeros((200, 200, 3), np.uint8)
    frame[100:150, 100:150] = (200, 180, 40)   # BGR 彩色块

    class D:
        serial = "fake"

        def screenshot(self, path=None, format=None):
            return frame

    r = ActionRunner(D(), {"step_interval": 0, "default_timeout": 1,
                           "click_timeout": 1}, case_name="t")
    assert map_ops._ensure_point_on_floor(r, 120, 120) == (120, 120)


def test_ensure_point_snaps_blank_to_floor():
    """点位落在空白:在分区外框内就近吸附到色块"""
    frame = np.zeros((200, 200, 3), np.uint8)
    frame[100:150, 100:150] = (200, 180, 40)

    class D:
        serial = "fake"

        def screenshot(self, path=None, format=None):
            return frame

    r = ActionRunner(D(), {"step_interval": 0, "default_timeout": 1,
                           "click_timeout": 1}, case_name="t")
    x, y = map_ops._ensure_point_on_floor(r, 30, 30, box=(90, 90, 160, 160))
    assert 100 <= x < 150 and 100 <= y < 150, f"吸附结果 ({x},{y}) 不在色块内"


# ── spot_clean 重试 ──

XML_AREA0 = ('<hierarchy>'
             '<node text="0" bounds="[234,284][276,385]"/>'
             '<node text="㎡" bounds="[276,290][305,329]"/>'
             '<node text="清扫面积" bounds="[212,402][328,441]"/>'
             '<node text="0" bounds="[766,284][808,385]"/>'
             '<node text="min" bounds="[808,290][856,329]"/>'
             '<node text="清扫时间" bounds="[753,402][869,441]"/>'
             '</hierarchy>')

# 房子刚扫干净时的成功形态:面积 0 但时长 1 分钟 —— 时长>0 就该算成功
XML_TIME_ONLY = ('<hierarchy>'
                 '<node text="0" bounds="[234,284][276,385]"/>'
                 '<node text="㎡" bounds="[276,290][305,329]"/>'
                 '<node text="清扫面积" bounds="[212,402][328,441]"/>'
                 '<node text="1" bounds="[766,284][808,385]"/>'
                 '<node text="min" bounds="[808,290][856,329]"/>'
                 '<node text="清扫时间" bounds="[753,402][869,441]"/>'
                 '</hierarchy>')

XML_AREA2 = ('<hierarchy>'
             '<node text="2" bounds="[234,284][276,385]"/>'
             '<node text="㎡" bounds="[276,290][305,329]"/>'
             '<node text="清扫面积" bounds="[212,402][328,441]"/>'
             '<node text="1" bounds="[766,284][808,385]"/>'
             '<node text="min" bounds="[808,290][856,329]"/>'
             '<node text="清扫时间" bounds="[753,402][869,441]"/>'
             '</hierarchy>')


class FakeSpotDevice:
    """文本定位一律命中;dump 按阶段返回(每次坐标点击=点分区,切下一份)。

    panel_from: 第几次查询"面板标志文本"起返回已展开(模拟展开时机)。
    """
    serial = "fake"

    def __init__(self, dumps, panel_from=2):
        self._dumps = list(dumps)
        self._phase = -1              # 第一次点分区后归 0 → 用第一份 XML
        self._panel_q = 0
        self._panel_from = panel_from
        self.clicked = []
        self.pressed = []

    def dump_hierarchy(self):
        i = max(0, min(self._phase, len(self._dumps) - 1))
        return self._dumps[i]

    def click(self, x, y):
        self.clicked.append((x, y))
        self._phase += 1          # 坐标点击 = 点分区,进入下一阶段

    def press(self, key):
        self.pressed.append(key)

    def screenshot(self, path=None, format=None):
        return np.zeros((100, 100, 3), dtype=np.uint8)   # 无色块 → 点位校验走兜底

    def __call__(self, **kw):
        owner = self
        tc = kw.get("textContains", "")

        class E:
            def exists(self, timeout=1):
                if kw.get("text") == "选择清扫模式":
                    return False            # 确认后模式选单已关闭
                if tc == "地图正在加载":
                    return False            # 桩里地图视为已就绪
                if tc == "清扫面积":
                    owner._panel_q += 1
                    return owner._panel_q >= owner._panel_from
                return True

            def click(self):
                owner.clicked.append(("text", kw))

            @property
            def info(self):
                # _click_by_locator 会检查 clickable,不给出会被当作 RN 行处理
                return {"clickable": True,
                        "text": kw.get("textContains") or kw.get("text") or "",
                        "bounds": {"left": 0, "top": 0,
                                   "right": 100, "bottom": 50}}

        return E()


def _runner(device):
    r = ActionRunner(device, {"step_interval": 0, "default_timeout": 1,
                              "click_timeout": 1}, case_name="t")
    r._stored_zones = [(100, 100), (400, 400), (700, 700)]
    r._next_room_idx = 0
    return r


def test_wait_clean_cycle_ignores_pre_departure(monkeypatch):
    """刚点完开始:充电文本在+状态文本在 → 不算回充;状态消失+充电在 → 算回充"""
    monkeypatch.setattr(ActionRunner, "_sleep", lambda self, s: None)

    class SeqDockDevice:
        """每次 __call__ 消耗一条 (充电文本存在, 状态文本存在)"""
        serial = "fake"

        def __init__(self, seq):
            self._seq = list(seq)
            self.i = 0

        def __call__(self, **kw):
            tc = kw.get("textContains", "")
            dock_now, state_now = self._seq[min(self.i, len(self._seq) - 1)]
            self.i += 1
            ok = dock_now if tc == "充电中" else state_now

            class E:
                def exists(self, timeout=1):
                    return ok

            return E()

    # 前 3 轮"未出发/清扫中",后 2 轮真回充
    r = ActionRunner(SeqDockDevice([(True, True), (True, True), (True, True),
                                    (True, False), (True, False)]),
                     {"step_interval": 0, "default_timeout": 1,
                      "click_timeout": 1}, case_name="t")
    assert map_ops._wait_clean_cycle(
        r, ["充电中"], ["指哪扫哪中"], dock_timeout=60) is True


def test_wait_clean_cycle_times_out_when_still_cleaning(monkeypatch):
    monkeypatch.setattr(ActionRunner, "_sleep", lambda self, s: None)

    class SeqDockDevice:
        serial = "fake"

        def __init__(self, seq):
            self._seq = list(seq)
            self.i = 0

        def __call__(self, **kw):
            tc = kw.get("textContains", "")
            dock_now, state_now = self._seq[min(self.i, len(self._seq) - 1)]
            self.i += 1
            ok = dock_now if tc == "充电中" else state_now

            class E:
                def exists(self, timeout=1):
                    return ok

            return E()

    # 永远"清扫中"(不在充电) → 超时返回 False
    r = ActionRunner(SeqDockDevice([(False, True)]),
                     {"step_interval": 0, "default_timeout": 1,
                      "click_timeout": 1}, case_name="t")
    assert map_ops._wait_clean_cycle(
        r, ["充电中"], ["指哪扫哪中"], dock_timeout=1) is False


def test_spot_clean_retries_next_room_on_zero_area(monkeypatch):
    """面积与时长全 0(点位不可达)→ 换下一分区;成功后数据进 store"""
    monkeypatch.setattr(map_ops, "_click_tpl",
                        lambda runner, name, timeout=10: (5, 5))
    monkeypatch.setattr(map_ops, "_wait_clean_cycle",
                        lambda runner, dt, st, dock_timeout=1200: True)
    monkeypatch.setattr(ActionRunner, "_sleep", lambda self, s: None)

    d = FakeSpotDevice([XML_AREA0, XML_AREA2])
    r = _runner(d)
    map_ops.do_spot_clean(r, {"spot_clean": True})

    assert r.store["面积"] == "2"
    assert r.store["时间"] == "1"
    # 第一次点了分区1(100,100),重试点了分区2(400,400)
    assert (100, 100) in d.clicked and (400, 400) in d.clicked
    assert r._next_room_idx == 2, "游标应推进到成功分区之后"


def test_spot_clean_success_when_only_time_positive(monkeypatch):
    """房子刚扫干净时面积就是 0 —— 时长>0 必须算成功,不得重试"""
    monkeypatch.setattr(map_ops, "_click_tpl",
                        lambda runner, name, timeout=10: (5, 5))
    monkeypatch.setattr(map_ops, "_wait_clean_cycle",
                        lambda runner, dt, st, dock_timeout=1200: True)
    monkeypatch.setattr(ActionRunner, "_sleep", lambda self, s: None)

    d = FakeSpotDevice([XML_TIME_ONLY])
    r = _runner(d)
    map_ops.do_spot_clean(r, {"spot_clean": True})

    assert r.store["面积"] == "0"
    assert r.store["时间"] == "1"
    assert (400, 400) not in d.clicked, "成功了就不该换分区重扫"


def test_spot_clean_collapses_panel_after_success(monkeypatch):
    """抓取成功(时长>0)后同样要把面板收回,恢复页面原状"""
    monkeypatch.setattr(map_ops, "_click_tpl",
                        lambda runner, name, timeout=10: (5, 5))
    monkeypatch.setattr(map_ops, "_wait_clean_cycle",
                        lambda runner, dt, st, dock_timeout=1200: True)
    monkeypatch.setattr(ActionRunner, "_sleep", lambda self, s: None)

    # 面板在抓取阶段才展开:点完开始时未展开(q1 False),抓取前未展开(q2 False
    # → 执行展开并记住位置),成功判定后已展开(q3 True → 收回)
    d = FakeSpotDevice([XML_AREA2], panel_from=2)
    r = _runner(d)
    map_ops.do_spot_clean(r, {"spot_clean": True})

    assert (5, 5) in d.clicked, "成功后应在原位收回数据面板"
    assert r._spot_expand_pos is None, "收回后应清掉记住的展开位置"


def test_spot_clean_raises_when_all_rooms_fail(monkeypatch):
    """所有分区都面积/时长全 0:报错且信息说明原因"""
    monkeypatch.setattr(map_ops, "_click_tpl",
                        lambda runner, name, timeout=10: (5, 5))
    monkeypatch.setattr(map_ops, "_wait_clean_cycle",
                        lambda runner, dt, st, dock_timeout=1200: True)
    monkeypatch.setattr(ActionRunner, "_sleep", lambda self, s: None)

    d = FakeSpotDevice([XML_AREA0])
    r = _runner(d)
    with pytest.raises(RuntimeError, match="均未产生有效清扫"):
        map_ops.do_spot_clean(r, {"spot_clean": True})
    # 3 个分区都试过
    assert (700, 700) in d.clicked
