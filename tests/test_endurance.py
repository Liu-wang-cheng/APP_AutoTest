# -*- coding: utf-8 -*-
"""续航测试纯函数单测（不连设备）。"""
import os

from core.endurance import (DEFAULT_COMBOS, charge_decision,
                            compute_percent_steps, compute_ten_percent,
                            parse_combos, prune_runs, render_report,
                            render_shutdown_report, shutdown_reached)


def test_compute_ten_percent_picks_crossings():
    samples = [
        {"ts": 0, "battery": 100, "clean_seconds": 0,
         "status": "work_status_total_clean"},
        {"ts": 10, "battery": 95, "clean_seconds": 10,
         "status": "work_status_total_clean"},
        {"ts": 20, "battery": 90, "clean_seconds": 20,
         "status": "work_status_total_clean"},
        {"ts": 30, "battery": 88, "clean_seconds": 30,
         "status": "work_status_total_clean"},
        {"ts": 40, "battery": 80, "clean_seconds": 40,
         "status": "work_status_total_clean"},
        {"ts": 50, "battery": 20, "clean_seconds": 50,
         "status": "work_status_back_charge"},
    ]
    ten = compute_ten_percent(samples, 100, 20)
    levels = [t["level"] for t in ten]
    assert levels == [100, 90, 80, 70, 60, 50, 40, 30, 20]
    assert ten[0]["battery"] == 100
    assert ten[1]["battery"] == 90
    assert ten[2]["battery"] == 80
    assert ten[-1]["battery"] == 20


def test_render_report_has_core_sections():
    result = {
        "title": "仅扫模式 · 吸力4", "ip": "1.2.3.4", "sn": "SN1",
        "firmware": "M 1.0", "mode": "sweepOnly", "fan_level": 4,
        "water_level": 0, "low_battery": 20, "low_battery_config": 20,
        "start_battery": 100, "start_status": "work_status_full_charging",
        "completed": True, "start_time": "2026-11-24 10:00:00",
        "end_time": "2026-11-24 12:00:00", "clean_seconds": 7200,
        "wall_seconds": 7300, "task_seconds": 7200, "return_battery": 20,
        "return_status": "work_status_back_charge", "restarts": 0,
        "ten_percent": [
            {"level": 100, "battery": 100, "ts": 0, "clean_seconds": 0,
             "status": "work_status_total_clean"},
            {"level": 90, "battery": 90, "ts": 600, "clean_seconds": 600,
             "status": "work_status_total_clean"},
        ],
        "events": [{"ts": 0, "name": "启动清扫", "detail": "fan=4"}],
        "sample_interval": 15,
        "samples": [
            {"ts": 0, "battery": 100, "status": "clean", "clean_seconds": 0,
             "fan_speed": 730, "water_level": 0, "water_on": False},
            {"ts": 60, "battery": 99, "status": "clean", "clean_seconds": 60,
             "fan_speed": 735, "water_level": 0, "water_on": False},
        ],
        "fan_changes": [{"ts": 0, "kind": "设备设置", "value": 4}],
        "mode_changes": [{"ts": 0, "value": "CLEAN_TYPE_SWEEP"}],
        "water_changes": [{"ts": 0, "kind": "用户挡位", "value": 0}],
        "water_outflows": [{"start_ts": 10, "stop_ts": 11,
                            "duration_s": 1.0, "interval_s": None,
                            "level": 0}],
        "water_sets": [{"ts": 10, "level": 0, "open_ms": 0,
                        "period_ms": 0}],
        "faults": [{"ts": 30, "event": "error_fan_stuck",
                    "status": "work_status_error", "detail": "fan stuck"}],
    }
    text = render_report(result)
    assert "续航测试报告" in text
    assert "续航时间（开始清扫→低电回充" in text
    assert "2:00:00" in text
    assert "每 10% 电量监测" in text
    assert "吸力挡位全程记录" in text
    assert "风机转速" in text
    assert "清扫模式全程记录" in text
    assert "水量挡位与出水记录" in text
    assert "故障事件" in text
    assert "error_fan_stuck" in text
    assert "start_clean_from_chg_bat_value" in text


def test_shutdown_reached_needs_low_battery_and_offline():
    assert shutdown_reached(4, "work_status_back_charge", 60) is True
    assert shutdown_reached(4, "work_status_back_charge", 10) is False
    assert shutdown_reached(8, "work_status_back_charge", 600) is False
    # 最后一次推送停在 6% 也要能判关机（离线够久）
    assert shutdown_reached(6, "work_status_back_charge", 100) is False
    assert shutdown_reached(6, "work_status_back_charge", 200) is True
    assert shutdown_reached(15, "work_status_shutdown", 0) is True
    assert shutdown_reached(-1, "work_status_back_charge", 60) is True


def test_compute_percent_steps_20_to_5():
    samples = [
        {"ts": 0, "battery": 20}, {"ts": 600, "battery": 15},
        {"ts": 1200, "battery": 10}, {"ts": 1800, "battery": 5},
    ]
    steps = compute_percent_steps(samples, 20, 5)
    assert [s["level"] for s in steps] == list(range(20, 4, -1))
    first = steps[0]
    last = steps[-1]
    assert first["battery"] == 20 and first["elapsed"] == 0
    assert last["level"] == 5 and last["battery"] == 5
    assert last["elapsed"] == 1800


def test_render_shutdown_report_has_key_sections():
    result = {
        "ip": "1.2.3.4", "sn": "SN1", "firmware": "M 1.0",
        "start_battery": 20, "shutdown_battery": 5, "end_battery": 4,
        "start_time": "2026-11-25 10:00:00", "end_time": "2026-11-25 12:05:00",
        "shutdown_seconds": 7500, "completed": True,
        "end_reason": "低电关机（4%）", "sample_interval": 15,
        "samples": [
            {"ts": 0, "battery": 20, "status": "work_status_back_charge"},
            {"ts": 7500, "battery": 4, "status": "work_status_back_charge"},
        ],
        "percent_steps": [
            {"level": 20, "battery": 20, "ts": 0, "elapsed": 0},
            {"level": 5, "battery": 5, "ts": 7000, "elapsed": 7000},
        ],
    }
    text = render_shutdown_report(result)
    assert "低电至关机续航报告" in text
    assert "低电至关机续航" in text
    assert "125 分钟" in text
    assert "每 1% 用时" in text
    assert "低电关机（4%）" in text


def test_default_combos_cover_sweep_and_sweepmop():
    assert len(DEFAULT_COMBOS) == 8
    assert list(DEFAULT_COMBOS[:4]) == [("sweepOnly", 1, 0),
                                       ("sweepOnly", 2, 0),
                                       ("sweepOnly", 3, 0),
                                       ("sweepOnly", 4, 0)]
    assert DEFAULT_COMBOS[-1] == ("sweepMop", 4, 3)


def test_charge_decision_attempts_three_times_then_fails():
    # 满电且不在清扫 → 可以开测
    assert charge_decision(100, "work_status_full_charging", 0, 0, 0) == "ready"
    # 电量不足但在充电/回充 → 继续等
    assert charge_decision(50, "work_status_charging", 0, 0, 0) == "wait"
    assert charge_decision(50, "work_status_back_charge", 0, 0, 0) == "wait"
    # 电量不足且不在充电/回充 → 发回充
    assert charge_decision(50, "work_status_idle", 0, 100, 0) == "attempt"
    # 未到下一次尝试时间 → 等
    assert charge_decision(50, "work_status_idle", 1, 50, 100) == "wait"
    # 已尝试 3 次仍不在充电/回充 → 失败
    assert charge_decision(50, "work_status_idle", 3, 100, 0) == "fail"
    # 100% 但还在清扫/暂停 → 可开测（调用方会先停掉旧任务再新开）
    assert charge_decision(100, "work_status_total_clean", 0, 0, 0) == "ready"
    assert charge_decision(100, "work_status_pause", 0, 0, 0) == "ready"
    # 电量/状态未知 → 等
    assert charge_decision(-1, "", 0, 0, 0) == "wait"


def test_parse_combos_accepts_short_and_full_forms():
    assert parse_combos("") is None
    assert parse_combos("sweepOnly:4,sweepMop:3:3") == [
        ("sweepOnly", 4, 0), ("sweepMop", 3, 3)]
    assert parse_combos("sweepThenMop:4:3") == [("sweepThenMop", 4, 3)]
    try:
        parse_combos("bad:1")
    except ValueError as e:
        assert "未知清扫模式" in str(e)
    else:
        raise AssertionError("should reject unknown mode")


def test_prune_runs_keeps_latest_ten(tmp_path):
    dirs = []
    for i in range(13):
        d = tmp_path / ("endurance_20260101_%06d" % i)
        d.mkdir()
        (d / "samples.csv").write_text("x", encoding="utf-8")
        os.utime(d, (1000 + i, 1000 + i))
        dirs.append(d)
    removed = prune_runs(root=str(tmp_path), keep=10)
    left = {p.name for p in tmp_path.iterdir()
            if p.name.startswith("endurance_")}
    assert removed == 3
    assert len(left) == 10
    assert dirs[0].name not in left and dirs[2].name not in left
    assert dirs[3].name in left and dirs[12].name in left


def test_prune_runs_never_deletes_protected_run(tmp_path):
    for i in range(12):
        d = tmp_path / ("endurance_suite_%02d" % i)
        d.mkdir()
        os.utime(d, (1000 + i, 1000 + i))
    oldest = str(tmp_path / "endurance_suite_00")
    prune_runs(root=str(tmp_path), keep=10, protect=[oldest])
    assert os.path.isdir(oldest)
    left = [p for p in tmp_path.iterdir()
            if p.is_dir() and p.name.startswith("endurance_suite_")]
    assert len(left) == 11


def test_prune_runs_drops_excel_older_than_kept_run(tmp_path):
    d = tmp_path / "endurance_20260101_000001"
    d.mkdir()
    os.utime(d, (5000, 5000))
    old_xlsx = tmp_path / "续航测试_旧_20250101.xlsx"
    old_xlsx.write_text("x", encoding="utf-8")
    os.utime(old_xlsx, (1000, 1000))
    new_xlsx = tmp_path / "续航测试_新_20260101.xlsx"
    new_xlsx.write_text("x", encoding="utf-8")
    os.utime(new_xlsx, (6000, 6000))
    prune_runs(root=str(tmp_path), keep=10)
    assert not old_xlsx.exists()
    assert new_xlsx.exists()


def test_keepalive_backoff_and_retry_gate(monkeypatch):
    """重连按 0/3/5 秒退避；整轮失败后 30 秒内不再重试（2026-11-26）。"""
    from core import endurance as en
    sleeps, logs = [], []
    monkeypatch.setattr(en.time, "sleep", lambda s: sleeps.append(s))

    class St:
        running = False

        def last_event(self):
            return 0

        def start(self, ip, paths, preserve=False):
            return False

    state = {}
    assert en._keepalive(St(), state, logs.append,
                         "1.2.3.4", ["/A"]) is False
    assert sleeps == [3.0, 5.0]
    assert logs[-1] == "[推送] 重连未成功，30 秒后重试"
    # 30 秒内不再重试，避免每个采样周期都去撞一次机器人会话
    assert en._keepalive(St(), state, logs.append,
                         "1.2.3.4", ["/A"]) is False
    assert logs == ["[推送] 重连未成功，30 秒后重试"]


def test_keepalive_logs_success():
    from core import endurance as en
    logs = []

    class St:
        running = False

        def last_event(self):
            return 0

        def start(self, ip, paths, preserve=False):
            return True

    state = {}
    assert en._keepalive(St(), state, logs.append,
                         "1.2.3.4", ["/A"]) is True
    assert logs == ["[推送] 数据流已重新订阅"]


def test_keepalive_waits_for_first_push_before_restart(monkeypatch):
    """刚订阅还没收到第一条推送时给宽限，不能立刻判死重启（2026-11-26）。

    否则会陷入「开流 → 立刻重启 → 又没等到第一条」的循环，电量一直是 -1。
    """
    from core import endurance as en
    logs, sleeps = [], []
    monkeypatch.setattr(en.time, "sleep", lambda s: sleeps.append(s))

    class St:
        running = True

        def last_event(self):
            return 0

        def start(self, *a, **k):
            raise AssertionError("宽限期内不应重启")

    state = {}
    assert en._keepalive(St(), state, logs.append,
                         "1.2.3.4", ["/A"]) is False
    assert not logs and not sleeps
    # 再调一次仍在宽限期内
    assert en._keepalive(St(), state, logs.append,
                         "1.2.3.4", ["/A"]) is False
    assert not logs and not sleeps


def test_keepalive_restarts_after_grace_expires(monkeypatch):
    from core import endurance as en
    import time as _t
    logs, sleeps = [], []
    monkeypatch.setattr(en.time, "sleep", lambda s: sleeps.append(s))

    class St:
        running = True

        def last_event(self):
            return 0          # 订阅很久了仍然一条推送都没有

        def start(self, ip, paths, preserve=False):
            return True

    state = {"since": _t.time() - 100}
    assert en._keepalive(St(), state, logs.append,
                         "1.2.3.4", ["/A"]) is True
    assert sleeps == []                       # 第一次就成功, 无需退避睡眠
    assert logs == ["[推送] 数据流已重新订阅"]
    assert state["since"] > _t.time() - 5      # 重连后重新计宽限


def test_line_ts_uses_log_time_with_clock_offset():
    """设备日志时间带机器人时钟偏差校准；间隔按行内时间计算（2026-11-26）。"""
    import time as _t
    from core.endurance import _line_ts
    now = _t.time()
    lt = _t.localtime(now + 3600)      # 机器人时钟快 1 小时
    line = "%02d-%02d %02d:%02d:%02d.000/SW D/x" % (
        lt.tm_mon, lt.tm_mday, lt.tm_hour, lt.tm_min, lt.tm_sec)
    state = {}
    ts = _line_ts(line, now, state)
    assert abs(ts - now) < 2
    lt2 = _t.localtime(now + 3603)
    line2 = "%02d-%02d %02d:%02d:%02d.000/SW D/x" % (
        lt2.tm_mon, lt2.tm_mday, lt2.tm_hour, lt2.tm_min, lt2.tm_sec)
    ts2 = _line_ts(line2, now + 3, state)
    assert abs((ts2 - ts) - 3) < 0.01


def test_pct_text_hides_unknown_battery():
    from core.endurance import _pct_text
    assert _pct_text(-1) == "未知"
    assert _pct_text(None) == "未知"
    assert _pct_text(20) == "20%"


def test_stream_fresh_only_counts_recent_events():
    """静默会话不能当成可用通道（下发挡位会假成功，2026-11-26）。"""
    import time as _t
    from core.endurance import _stream_fresh

    class St:
        def __init__(self, running, last):
            self.running = running
            self._last = last

        def last_event(self):
            return self._last

    now = _t.time()
    assert _stream_fresh(St(True, now - 5)) is True
    assert _stream_fresh(St(True, now - 30)) is False
    assert _stream_fresh(St(False, now - 1)) is False
    assert _stream_fresh(St(True, 0)) is False


def test_poke_stream_reads_at_most_every_interval(monkeypatch):
    """保活读按 interval 限频（上位机同款，防止机器人 35 秒后停推）。"""
    import time as _t
    from core import endurance as en

    calls = []

    class St:
        def read(self, paths, timeout_ms=0):
            calls.append((list(paths), timeout_ms))
            return {}

    clock = {"now": _t.time()}
    monkeypatch.setattr(en.time, "time", lambda: clock["now"])
    state = {}
    en._poke_stream(St(), ["/A"], state)
    en._poke_stream(St(), ["/A"], state)
    assert len(calls) == 1
    clock["now"] += 25
    en._poke_stream(St(), ["/A"], state)
    assert len(calls) == 2
    assert calls[-1] == (["/A"], 1500)


def test_plan_settings_only_sends_changes():
    """吸力/水量只在下发值变化时发送（用户 2026-11-26）。"""
    from core.endurance import plan_settings
    applied = {"mode": "sweepMop", "fan": 4, "water": 3}
    assert plan_settings(applied, "sweepMop", 4, 3) == {}
    assert plan_settings(applied, "sweepMop", 2, 3) == {"fan": 2}
    assert plan_settings(applied, "sweepMop", 4, 1) == {"water": 1}
    assert plan_settings(applied, "sweepOnly", 4, 0) == {
        "mode": "sweepOnly", "water": 0}
    # 观测到偏离时定点强制恢复
    assert plan_settings(applied, "sweepMop", 4, 3, only=("fan",)) == {"fan": 4}
    assert plan_settings(applied, "sweepMop", 4, 3, only=("water",)) == {
        "water": 3}


def test_suite_total_timeout_scales_with_combos():
    """总超时 = 每组合 8 小时 × 组合数（用户 2026-11-26）。"""
    from core.endurance import suite_total_timeout
    assert suite_total_timeout(8 * 3600, 1) == 8 * 3600
    assert suite_total_timeout(8 * 3600, 8) == 64 * 3600
    assert suite_total_timeout(0, 3) == 3          # 至少 1 秒，避免 0 预算


def test_wash_wait_only_for_mop_modes():
    """拖地回站洗拖布不能当成任务完成去重启（用户 2026-11-26）。"""
    from core.endurance import RETURN_DONE_STATUSES, is_wash_wait
    assert is_wash_wait("sweepMop", "work_status_back_charge") is True
    assert is_wash_wait("mopOnly", "work_status_wash_mop") is True
    assert is_wash_wait("sweepThenMop", "work_status_base_station") is True
    # 仅扫不回洗；充电/低电回充是真正的结束条件，不是回洗等待
    assert is_wash_wait("sweepOnly", "work_status_back_charge") is False
    assert is_wash_wait("sweepMop", "work_status_charging") is False
    assert is_wash_wait("sweepMop", "work_status_total_clean") is False
    assert is_wash_wait("sweepMop", "work_status_error") is False
    # 回充成功进入充电/烘干 → 立即继续新的清扫任务
    assert is_wash_wait("sweepMop", "work_status_dry_mop") is False
    assert "work_status_dry_mop" in RETURN_DONE_STATUSES
    assert "work_status_charging" in RETURN_DONE_STATUSES
    assert "work_status_full_charging" in RETURN_DONE_STATUSES
