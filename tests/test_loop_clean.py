# -*- coding: utf-8 -*-
"""循环清扫纯函数单测（不连设备）。"""
from core.loop_clean import (MAX_ERROR_RETRIES, next_combo, ready_to_start,
                             round_done, should_return_charge)


def test_round_done_statuses_cover_charge_dry_idle_dormant():
    assert round_done("work_status_charging") is True
    assert round_done("work_status_dry_mop") is True
    assert round_done("work_status_idle") is True
    assert round_done("work_status_dormant") is True
    assert round_done("work_status_total_clean") is False
    assert round_done("work_status_error") is False
    assert MAX_ERROR_RETRIES == 3


def test_return_and_start_thresholds():
    # 小于回充电量才回充（20 → 19 回充，20 不回）
    assert should_return_charge(19, 20) is True
    assert should_return_charge(20, 20) is False
    assert should_return_charge(25, 20) is False
    # 电量 ≥ 清扫电量就允许开始（充电中也可以）
    assert ready_to_start(30, "work_status_charging", 30) is True
    assert ready_to_start(29, "work_status_charging", 30) is False
    assert ready_to_start(40, "work_status_idle", 30) is True
    assert ready_to_start(40, "work_status_dormant", 30) is True
    # 回充途中/基站任务中不能开始
    assert ready_to_start(40, "work_status_back_charge", 30) is False
    assert ready_to_start(40, "work_status_go_charge", 30) is False
    assert ready_to_start(40, "work_status_base_station", 30) is False
    assert ready_to_start(40, "work_status_total_clean", 30) is False
    assert ready_to_start(40, "work_status_error", 30) is False


def test_combo_parts_supports_clean_types_and_room_ids():
    from core.loop_clean import combo_parts, combo_label, parse_area_ids
    assert combo_parts(("sweepOnly", 1, 0)) == ("sweepOnly", 1, 0, "total", [])
    # 旧版「原地定点(curpoint)」统一映射成「指定点定点(point)」
    assert combo_parts(("sweepOnly", 1, 0, "curpoint"))[3] == "point"
    assert combo_parts(("sweepOnly", 1, 0, "point", [500, -200]))[4] == [500, -200]
    mode, fan, water, ctype, ids = combo_parts(
        ("sweepMop", 2, 1, "rooms", "1, 3"))
    assert (mode, fan, water, ctype, ids) == (
        "sweepMop", 2, 1, "rooms", [1, 3])
    assert "选区" in combo_label(("sweepMop", 2, 1, "rooms", "1,3"))
    assert parse_area_ids("2，4;5") == [2, 4, 5]
    assert parse_area_ids("房间1 房间2") == [1, 2]
    assert parse_area_ids("房间 3、房间 1") == [3, 1]
    assert parse_area_ids("") == []


def test_point_clean_builds_1500_square_two_passes():
    """指定点定点 = 以点为中心 1.5m 见方、扫 2 遍(与 AP 定点参数一致)。"""
    from core.loop_clean import (POINT_CLEAN_COUNT, POINT_SIDE, parse_point,
                                 point_area_entry)
    assert parse_point([500, -200]) == [500, -200]
    assert parse_point("500,-200") == [500, -200]
    assert parse_point("定点(500, -200)") == [500, -200]
    assert parse_point("") == [] and parse_point("x,y") == []
    assert POINT_SIDE == 1500 and POINT_CLEAN_COUNT == 2
    e = point_area_entry([500, -200])
    assert e["cleanCount"] == 2 and e["id"] == 9001
    assert e["vertexs"] == [[-250, -950], [1250, -950],
                            [1250, 550], [-250, 550]]


def test_next_combo_rotates():
    combos = [("sweepOnly", 2, 0), ("sweepMop", 3, 3)]
    assert next_combo(combos, 0) == ("sweepOnly", 2, 0)
    assert next_combo(combos, 1) == ("sweepMop", 3, 3)
    assert next_combo(combos, 2) == ("sweepOnly", 2, 0)
    assert next_combo([], 0) is None


def test_zone_combo_dispatch_payload_matches_ap_upper_computer():
    """划区: SetAreaInfo 的 Areainfo 与 AP 上位机抓包格式一致(2026-11-28)。"""
    from core.loop_clean import (CLEAN_TYPE_ZONES, combo_label, combo_parts,
                                 parse_zones, zone_area_entries)
    assert parse_zones("") == []
    # 矩形无论怎么拖都归一化成 [x_min, y_min, x_max, y_max]
    assert parse_zones("119,242,768,-861") == [[119, -861, 768, 242]]
    # 中文逗号 / 多区域 / 过小区域丢弃
    assert parse_zones("800,900,100,200；50,60,70,80") == [
        [100, 200, 800, 900]]
    assert parse_zones([[0, 0, 1500, 1500], [1, 2, 3, 4]]) == [
        [0, 0, 1500, 1500]]
    entries = zone_area_entries([[119, 242, 768, -861],
                                 [0, 0, 1500, 1500]])
    assert entries[0] == {
        "id": 9001, "cleanCount": 1,
        "vertexs": [[119, -861], [768, -861], [768, 242], [119, 242]]}
    assert [e["id"] for e in entries] == [9001, 9002]
    combo = ("sweepOnly", 1, 0, CLEAN_TYPE_ZONES, [[119, 242, 768, -861]])
    assert combo_parts(combo)[4] == [[119, -861, 768, 242]]
    assert "划区 1 个" in combo_label(combo)


def test_should_stop_existing_cleaning_or_paused_task():
    """开测时已有清扫/暂停任务 → 需要先停止旧任务再按测试组合重开。"""
    from core.loop_clean import should_stop_existing
    assert should_stop_existing("work_status_total_clean") is True
    assert should_stop_existing("work_status_clean") is True
    assert should_stop_existing("work_status_pause") is True
    assert should_stop_existing("work_status_base_station_pause") is True
    assert should_stop_existing("work_status_charging") is False
    assert should_stop_existing("work_status_idle") is False
    assert should_stop_existing("") is False


def test_fault_chinese_name_available():
    from core.loop_clean import _fault_cn
    assert _fault_cn("error_fan_stuck") == "风机卡住"


def test_loop_report_style_matches_other_reports(tmp_path):
    """循环清扫 Excel：标题/表头配色与自动化/续航报告一致。"""
    import openpyxl
    from tools.loop_report import build_report

    result = {
        "ip": "10.0.0.9", "clean_battery": 30, "recharge_battery": 20,
        "max_rounds": 0,
        "combos": [("sweepOnly", 1, 0), ("sweepMop", 1, 1)],
        "rounds": [{"no": 1, "combo": ("sweepOnly", 1, 0),
                    "start_time": "2026-11-27 10:00:00",
                    "end_time": "2026-11-27 10:30:00",
                    "start_battery": 30, "end_battery": 20,
                    "end_status": "work_status_charging", "seconds": 1800}],
        "errors": [{"time": "2026-11-27 10:10:00", "round": 1,
                    "event": "error_fan_stuck", "name": "风机卡住",
                    "status": "work_status_error", "detail": "x"}],
        "events": [
            {"ts": 1, "time": "2026-11-27 10:00:00", "round": 1,
             "kind": "开始清扫", "detail": "sweepOnly 吸力1 水量0"},
            {"ts": 2, "time": "2026-11-27 10:10:00", "round": 1,
             "kind": "报错", "detail": "error_fan_stuck（风机卡住），状态 error"},
            {"ts": 3, "time": "2026-11-27 10:30:00", "round": 1,
             "kind": "开始回充", "detail": "电量 19% < 20%"},
        ],
        "end_reason": "用户停止", "start_time": "2026-11-27 10:00:00",
        "end_time": "2026-11-27 11:00:00",
    }
    out = build_report(result, str(tmp_path / "loop.xlsx"))
    wb = openpyxl.load_workbook(out)
    assert wb.sheetnames == ["汇总", "轮次明细", "事件明细"]
    assert wb["汇总"]["A1"].value == "循环清扫报告"
    head = wb["轮次明细"]["A1"]
    assert str(head.fill.fgColor.rgb).endswith("4472C4")
    ev = wb["事件明细"]
    assert ev["D2"].value == "开始清扫"
    assert ev["D3"].value == "报错"
    assert ev["D3"].font.color.rgb.endswith("C00000")      # 报错行红字
    assert ev["D4"].value == "开始回充"


def test_start_accept_statuses_include_wash_and_base_station():
    """启动清扫后进入回站/洗拖布也算任务已接受（扫拖先洗拖布）。"""
    from core.loop_clean import START_ACCEPT_STATUSES
    assert "work_status_wash_mop" in START_ACCEPT_STATUSES
    assert "work_status_base_station" in START_ACCEPT_STATUSES
    assert "work_status_collect_dust" in START_ACCEPT_STATUSES
