# -*- coding: utf-8 -*-
"""电量监测纯逻辑: 采样线程 / 统计 / mm:ss 格式(无设备)。"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.battery_monitor import (BatteryMonitor, BatterySample,
                                  build_points, build_summary, format_mmss)


def _samples(*pairs, label=""):
    return [BatterySample(t=t, battery=b, label=label) for t, b in pairs]


def test_format_mmss():
    assert format_mmss(0) == "00:00"
    assert format_mmss(65) == "01:05"
    assert format_mmss(3600) == "60:00"
    assert format_mmss(-1) == "--:--"
    assert format_mmss(None) == "--:--"


def test_build_points_raw_and_milestones():
    # 起点 87 → 每隔一段降 1; 里程碑步长 5 → 82/77/72
    s = _samples((0, 87), (10, 87), (30, 86), (60, 85), (90, 84),
                 (120, 83), (150, 82), (200, 81))
    pts = build_points(s, step_pct=5)
    assert pts["valid"] == 8 and pts["invalid"] == 0
    assert pts["start_battery"] == 87 and pts["end_battery"] == 81
    # 原始变化: 86→81 共 6 次(87→86 起都算变化)
    assert [p["battery"] for p in pts["raw"]] == [86, 85, 84, 83, 82, 81]
    # 里程碑: 只有一个 —— 降到 82(相对 87 累计 -5)
    assert len(pts["milestones"]) == 1
    m = pts["milestones"][0]
    assert m["target"] == 82 and m["battery"] == 82
    # 里程碑用时 = 上一个锚点(起点 0s) 到达成时刻
    assert m["dt"] == 150


def test_build_points_multi_drop_single_row():
    """一次采样跨过多个阈值只记一行, 下一锚点从实际电量重算(避免 dt=0 噪声行)。"""
    s = _samples((0, 90), (10, 84))          # 一次掉 6, 跨过 85 与 80 两个阈值
    pts = build_points(s, step_pct=5)
    assert len(pts["milestones"]) == 1
    assert pts["milestones"][0]["battery"] == 84
    assert pts["milestones"][0]["target"] == 85
    # 后续锚点从 84 重新算: 79
    s2 = _samples((0, 90), (10, 84), (20, 79))
    pts2 = build_points(s2, step_pct=5)
    assert [m["target"] for m in pts2["milestones"]] == [85, 79]


def test_build_points_counts_invalid_and_empty():
    s = _samples((0, -1), (10, -1))
    pts = build_points(s, 5)
    assert pts["valid"] == 0 and pts["invalid"] == 2
    assert pts["start_battery"] is None and pts["raw"] == [] \
        and pts["milestones"] == []


def test_build_summary_totals_and_cases():
    a = _samples((0, 80), (60, 79), (120, 78), label="用例A(第1次)")
    b = _samples((120, 78), (240, 76), label="用例B(第1次)")
    sm = build_summary(a + b, step_pct=5)
    assert sm["start_battery"] == 80 and sm["end_battery"] == 76
    assert sm["total_drop"] == 4
    assert sm["total_seconds"] == 240
    assert sm["average_per_1pct"] == 60.0
    assert [c["label"] for c in sm["cases"]] == ["用例A(第1次)", "用例B(第1次)"]
    ca, cb = sm["cases"]
    assert ca["drop"] == 2 and ca["seconds"] == 120 and ca["average_per_1pct"] == 60
    assert cb["drop"] == 2 and cb["seconds"] == 120


def test_build_summary_charging_gives_no_per_pct():
    """电量上升(充电)时平均每 1% 不计算, 总耗电如实为负。"""
    sm = build_summary(_samples((0, 50), (100, 60)), 5)
    assert sm["total_drop"] == -10
    assert sm["average_per_1pct"] is None
    assert sm["milestone_average"] is None


def test_build_summary_longest_shortest_segment():
    """★ 汇总要有每 N% 段的最长/最短用时(用户 2026-11-04), 并带分段区间。"""
    # 起点 90: 85 用 100s, 80 用 300s, 75 用 50s
    s = _samples((0, 90), (100, 85), (400, 80), (450, 75))
    sm = build_summary(s, step_pct=5)
    assert sm["milestone_longest"]["dt"] == 300
    assert (sm["milestone_longest"]["from"], sm["milestone_longest"]["to"]) == (85, 80)
    assert sm["milestone_shortest"]["dt"] == 50
    assert (sm["milestone_shortest"]["from"], sm["milestone_shortest"]["to"]) == (80, 75)


def test_build_points_resets_segment_on_charge():
    """★ 电量回升(回充/充电)后重新锚定: 充电时间不能算进任何一个 N% 段
    (代码审查 2026-11-04: 否则最长段会被充电的几十分钟撑爆, 结论失真)。"""
    # 50 →(100s) 45 → 充电到 60(t=1000) → 55(t=1100)
    s = _samples((0, 50), (100, 45), (1000, 60), (1100, 55), (1300, 50))
    pts = build_points(s, step_pct=5)
    # 第一段: 50→45, 用时 100s
    assert pts["milestones"][0]["from_battery"] == 50
    assert pts["milestones"][0]["battery"] == 45
    assert pts["milestones"][0]["dt"] == 100
    # 充电后重新锚定 60: →55 用时 100s(而不是 1100s), →50 用时 200s
    assert pts["milestones"][1]["from_battery"] == 60
    assert pts["milestones"][1]["battery"] == 55
    assert pts["milestones"][1]["dt"] == 100
    assert pts["milestones"][2]["from_battery"] == 55
    assert pts["milestones"][2]["dt"] == 200


def test_build_summary_empty():
    sm = build_summary([], 5)
    assert sm["valid"] == 0 and sm["cases"] == []
    assert sm["start_battery"] is None


def test_monitor_thread_samples_and_stops():
    """线程启动立即采一次, 停止后干净退出(daemon 线程不留僵尸)。"""
    calls = {"n": 0}

    def reader(_d):
        calls["n"] += 1
        return 50 + calls["n"]

    m = BatteryMonitor(device=None, interval=0.05, reader=reader,
                       label_getter=lambda: "用例A(第1次)")
    m.start()
    deadline = time.time() + 2.0               # 线程调度有抖动, 轮询等第 2 个点
    while calls["n"] < 2 and time.time() < deadline:
        time.sleep(0.02)
    m.stop(timeout=2.0)
    assert not m.is_alive()
    assert calls["n"] >= 2                      # 启动即采 + 间隔后再采
    assert all(s.battery >= 51 for s in m.samples)
    assert m.samples[0].label == "用例A(第1次)"
    assert m.samples[0].t < 0.05                # 第一个点相对时间≈0
    m.stop()                                    # 幂等
    m.stop(timeout=1)


def test_monitor_waits_for_first_valid_battery_to_start_timing():
    """★ 未进设备页前读不到电量 → 不开始计时; 第一个有效点才是 t=0 的起点
    (用户 2026-11-04: 起始电量/总时长不能把前置导航算进去)。"""
    seq = iter([-1, -1, 100, 99, 98])
    m = BatteryMonitor(device=None, interval=1, reader=lambda _d: next(seq))
    for _ in range(5):                 # 直接驱动采样(线程间隔被下限钳到 1s, 不适合测)
        m._sample_once()
        time.sleep(0.01)
    assert [s.battery for s in m.samples] == [100, 99, 98]
    assert m.samples[0].t < 0.05, "第一个有效点应为 t=0"
    assert m.samples[1].t >= 0.005


def test_monitor_reader_failure_records_invalid_after_arming():
    """开始计时之后的读取失败要如实记 -1(计入"读取失败"); 开始之前的不记。"""
    seq = iter([100, -1, 98])
    m = BatteryMonitor(device=None, interval=1, reader=lambda _d: next(seq))
    for _ in range(3):
        m._sample_once()
    assert [s.battery for s in m.samples] == [100, -1, 98]


def test_monitor_accepts_param_dict_with_voltage_and_temp():
    """扫地机参数读取返回 dict 时，电量/电压(V)/温度一起入样本。"""
    m = BatteryMonitor(
        device=None, interval=1,
        reader=lambda _d: {"battery": 80, "voltage_v": 15.8,
                           "temp_c": 31.5, "source": "param"})
    m._sample_once()
    s = m.samples[0]
    assert (s.battery, s.voltage_v, s.temp_c, s.source) == (80, 15.8, 31.5, "param")
    sm = build_summary(m.samples, 5)
    assert sm["start_voltage"] == 15.8
    assert sm["start_temp"] == 31.5
