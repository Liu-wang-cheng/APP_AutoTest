# -*- coding: utf-8 -*-
"""清扫链路集成测试(假推送流, 不连真机)。

覆盖 run_loop_clean / run_endurance_suite 的真实主流程:
  · 四种清扫类型各自的下发事件(全局/定点/选区/划区);
  · 下发前转空闲、定位成功后下发挡位、临时区域结束时清理;
  · 一轮结束、报告生成。
"""
import json
import os
import time

import pytest

import core.endurance as endurance
import core.loop_clean as loop_clean

CLEAN = ("work_status_area_clean", "work_status_total_clean")


class FakeStream:
    """内存推送流: values() 返回状态/电量, 记录所有下发; 清扫几次后自动结束。"""

    running = True

    def __init__(self, status="work_status_charging", battery=100,
                 flip_at=12):
        self.status = status
        self.battery = battery
        self.flip_at = flip_at
        self.reads = 0
        self.events = []          # ("post"/"json"/"binpack", ...)
        self._last = time.time()

    # ── 读取 ──
    def values(self):
        if self.status in CLEAN:
            self.reads += 1
            if self.reads >= self.flip_at:
                # 一轮结束: 回充 + 低电(续航的低电结束条件)
                self.status = "work_status_charging"
                self.battery = 20
        self._last = time.time()
        return {
            "/CleanTask/CleanAppStatus": {"CleanAppStatus": self.status},
            "/BatteryBattery/BatteryLevel": self.battery,
            "/BatteryBattery/BatteryVol": 24000,
            "/MotorFan/Speed": 3000,
        }

    def last_event(self):
        return time.time()

    def read(self, paths=None, timeout_ms=1500):
        return {p: 1 for p in (paths or [])}

    def start(self, ip=None, paths=None, preserve=False):
        self.running = True
        return True

    def stop(self):
        self.running = False

    # ── 下发 ──
    def _enter_clean(self):
        if self.status not in CLEAN:
            self.status = "work_status_area_clean"

    def post_event(self, topic, event, params=None):
        self.events.append(("post", topic, event, params))
        if event in ("app_sweeper_start_total_clean",
                     "app_sweeper_start_area_clean",
                     "app_sweeper_start_curpoint_clean"):
            self._enter_clean()
        elif event in ("stop_clean", "kill_all_nodes"):
            self.status = "work_status_idle"
        return 0

    def send_json_event(self, topic, text):
        data = json.loads(text)
        self.events.append(("json", topic, data))
        if topic in ("/CleanTask/StartAreaClean",
                     "/CleanTask/StartSegmentClean"):
            self._enter_clean()
        return 0

    def send_binpack_event(self, event_id, params=None):
        self.events.append(("binpack", event_id, params))
        return 0

    # ── 断言辅助 ──
    def json_events(self, topic):
        return [e[2] for e in self.events if e[0] == "json" and e[1] == topic]

    def post_events(self, event):
        return [e for e in self.events
                if e[0] == "post" and e[2] == event]


class FakeMcu:
    """调试流: 每次都给「定位成功」事件(引擎每轮开始时会把 location_ok 清掉)。"""

    def __init__(self, *_a, **_k):
        pass

    def start(self, ip=None):
        return True

    def stop(self):
        pass

    def log_since(self, ts):
        return [(time.time(), "clean_location_success")]


@pytest.fixture
def fast_sleep(monkeypatch):
    """把两个引擎里的 time.sleep 调快(真机流程用例会等 0.5~1s 一轮)。"""
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)


@pytest.fixture
def fakes(monkeypatch, fast_sleep):
    monkeypatch.setattr(loop_clean, "McuStream", FakeMcu)
    monkeypatch.setattr(endurance, "McuStream", FakeMcu)


def _run_loop(tmp_path, combo):
    st = FakeStream()
    result, base = loop_clean.run_loop_clean(
        "10.0.0.1", [combo], clean_battery=30, recharge_battery=20,
        max_rounds=1, sample_interval=60, out_dir=str(tmp_path),
        stream=st)
    return st, result, base


def test_loop_total_clean_flow(fakes, tmp_path):
    st, result, base = _run_loop(tmp_path, ("sweepOnly", 1, 0))
    assert st.post_events("app_sweeper_start_total_clean")
    assert len(result["rounds"]) == 1
    assert os.path.isfile(os.path.join(base, "循环清扫_报告.xlsx"))
    # 定位成功后下发挡位, 且第一件事不是 stop_clean(不需要转空闲)
    assert st.post_events("change_fan_level")
    assert st.events[0][2] != "stop_clean"


def test_loop_point_clean_flow(fakes, tmp_path):
    st, result, base = _run_loop(
        tmp_path, ("sweepOnly", 1, 0, "point", [500, -200]))
    areas = st.json_events("/MapData/SetAreaInfo")
    # 先写 1.5m 见方、扫 2 遍的临时区域
    assert areas and areas[0]["Areainfo"][0]["cleanCount"] == 2
    vs = areas[0]["Areainfo"][0]["vertexs"]
    assert (vs[1][0] - vs[0][0]) == 1500 and (vs[2][1] - vs[1][1]) == 1500
    starts = st.json_events("/CleanTask/StartAreaClean")
    assert starts and starts[0]["areaFillType"] == "FILL_TYPE_AREA"
    assert starts[0]["cleanId"] == [9001]
    # 结束时清掉临时区域
    assert areas[-1] == {"Areainfo": []}
    assert len(result["rounds"]) == 1


def test_loop_rooms_clean_flow(fakes, tmp_path):
    st, _result, _base = _run_loop(tmp_path, ("sweepOnly", 1, 0, "rooms",
                                              "房间1 房间2"))
    starts = st.json_events("/CleanTask/StartSegmentClean")
    assert starts and starts[0]["areaFillType"] == "FILL_TYPE_AUTO"
    assert starts[0]["cleanId"] == [1, 2]
    # 选区不写临时区域
    assert not st.json_events("/MapData/SetAreaInfo")


def test_loop_zones_clean_flow(fakes, tmp_path):
    zones = [[-500, -300, 0, 200], [0, -300, 500, 200]]
    st, _result, _base = _run_loop(
        tmp_path, ("sweepOnly", 1, 0, "zones", zones))
    areas = st.json_events("/MapData/SetAreaInfo")
    assert len(areas[0]["Areainfo"]) == 2
    starts = st.json_events("/CleanTask/StartAreaClean")
    assert starts and starts[0]["cleanId"] == [9001, 9002]
    assert areas[-1] == {"Areainfo": []}


def _run_endurance(tmp_path, combo):
    st = FakeStream()
    entries, base, _report = endurance.run_endurance_suite(
        "10.0.0.1", combos=[combo], out_dir=str(tmp_path),
        low_battery=20, sample_interval=60, report=False, stream=st)
    return st, entries, base


def test_endurance_suite_dispatches_zones_and_rooms(fakes, tmp_path):
    st, entries, base = _run_endurance(
        tmp_path, ("sweepOnly", 1, 0, "zones", [[-500, -300, 0, 200]]))
    assert entries and entries[0]["clean_type"] == "zones"
    areas = st.json_events("/MapData/SetAreaInfo")
    assert areas and areas[0]["Areainfo"][0]["cleanCount"] == 1
    assert st.json_events("/CleanTask/StartAreaClean")
    assert areas[-1] == {"Areainfo": []}      # 结束清临时区域
    assert os.path.isdir(base)

    st2, entries2, _b2 = _run_endurance(
        tmp_path / "r", ("sweepMop", 2, 1, "rooms", "1,2"))
    starts = st2.json_events("/CleanTask/StartSegmentClean")
    assert starts and starts[0]["areaFillType"] == "FILL_TYPE_AUTO"
    assert starts[0]["cleanId"] == [1, 2]
    assert entries2[0]["clean_type"] == "rooms"


def test_endurance_point_combo(fakes, tmp_path):
    st, entries, _base = _run_endurance(
        tmp_path, ("sweepOnly", 1, 0, "point", [500, -200]))
    areas = st.json_events("/MapData/SetAreaInfo")
    assert areas and areas[0]["Areainfo"][0]["cleanCount"] == 2
    assert entries[0]["clean_type"] == "point"
