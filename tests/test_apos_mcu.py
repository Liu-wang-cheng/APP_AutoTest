# -*- coding: utf-8 -*-
"""MCU 反馈（挡位）解码：7809 调试流字段位置与参数服务合并。"""
import struct
import time

from core.apos.mcu import McuStream
from core.apos.service import ParamService


def _frame(typ, length, fields):
    buf = bytearray(length)
    struct.pack_into("<HH", buf, 0, typ, 2001)
    for off, val in fields.items():
        buf[off] = val
    return bytes(buf)


def test_decode_control_frame():
    """1111/82：55=风机控制量，64=边刷控制量，65=中扫控制量。"""
    f = _frame(1111, 82, {55: 248, 64: 160, 65: 2})
    s = McuStream()
    s._decode(f)
    vals = s.values()
    assert vals["/Mcu/FanControl"] == 248
    assert vals["/Mcu/SideBrushControl"] == 160
    assert vals["/Mcu/MidBrushControl"] == 2
    assert s.last_event() > 0


def test_decode_value_frame():
    """1009/134：充电状态、超声波、墙距离、风机/中刷/侧刷值。"""
    f = bytearray(_frame(1009, 134, {57: 9, 65: 0, 66: 255,
                                     82: 1, 91: 18, 92: 50, 93: 100}))
    struct.pack_into("<HHH", f, 40, 1, 768, 0)   # 超声2/3/4
    f = bytes(f)
    s = McuStream()
    s._decode(f)
    vals = s.values()
    assert vals["/Mcu/ChargeStatus"] == 1
    assert vals["/Mcu/CliffTrigger"] == 9
    assert vals["/Mcu/WallDistance"] == 255
    assert vals["/Mcu/Ultrasonic2"] == 1
    assert vals["/Mcu/Ultrasonic3"] == 768
    assert vals["/Mcu/Ultrasonic4"] == 0
    assert vals["/Mcu/FanValue"] == 18
    assert vals["/Mcu/MidBrushValue"] == 50
    assert vals["/Mcu/SideBrushValue"] == 100


def test_decode_other_sensor_frame():
    """1008/94：偏航角度、左右里程计(mm)、光流x轴偏移量。"""
    buf = bytearray(94)
    struct.pack_into("<HH", buf, 0, 1008, 2001)
    struct.pack_into("<III", buf, 40, 553571, 1147149, 1265990)
    struct.pack_into("<h", buf, 78, -8649)
    s = McuStream()
    s._decode(bytes(buf))
    vals = s.values()
    assert vals["/Mcu/YawAngle"] == 553571
    assert vals["/Mcu/LeftOdometer"] == 1147149
    assert vals["/Mcu/RightOdometer"] == 1265990
    assert vals["/Mcu/OpticalFlowX"] == -8649


def test_ignores_other_frames():
    s = McuStream()
    s._decode(_frame(1111, 82, {55: 1})[:3])          # 太短
    bad = bytearray(_frame(1111, 82, {55: 248}))
    struct.pack_into("<H", bad, 2, 0)                  # 子类型不是 2001
    s._decode(bytes(bad))
    s._decode(_frame(1001, 115, {}))                   # 日志帧
    assert s.values() == {}


def test_service_merges_mcu_values():
    class _FakeMcu:
        running = True

        def values(self):
            return {"/Mcu/FanControl": 248, "/Mcu/SideBrushControl": 160}

        def last_event(self):
            return 123.0

    class _C:
        def read(self, paths):
            return {}

    svc = ParamService(_C(), ["/A"], mcu=_FakeMcu())
    snap = svc.latest()
    assert snap["ok"]["/Mcu/FanControl"] == 248
    assert snap["ok"]["/Mcu/SideBrushControl"] == 160
    assert snap["ts"] == 123.0


def test_service_drops_stale_mcu_values():
    """调试流停了以后旧值不能被当成实时值一直显示（审查 2026-11-09）。"""
    now = time.time()

    class _FakeMcu:
        running = True

        def values_with_ts(self):
            return {"/Mcu/FanControl": (248, now - 60),
                    "/Mcu/ChargeStatus": (1, now)}

        def last_event(self):
            return now

    class _C:
        def read(self, paths):
            return {}

    svc = ParamService(_C(), ["/A"], mcu=_FakeMcu())
    snap = svc.latest()
    assert "/Mcu/FanControl" not in snap["ok"]
    assert snap["ok"]["/Mcu/ChargeStatus"] == 1


def test_control_frame_layout():
    """7813 唤醒帧：厂商 SdkHeader 是 short/ushort/uint，共 8 字节。"""
    from core.apos.mcu import control_frame
    assert control_frame() == b"\xf1\x00\x00\x00\x00\x00\x00\x00"
    assert control_frame(payload=b"\x02\x00\x00\x00\x58\x1b\x00\x00") == (
        b"\xf1\x00\x00\x00\x08\x00\x00\x00\x02\x00\x00\x00\x58\x1b\x00\x00")


def test_mcu_values_with_ts_keeps_per_path_time():
    s = McuStream()
    s._decode(_frame(1111, 82, {55: 248, 64: 160, 65: 2}))
    stamped = s.values_with_ts()
    assert stamped["/Mcu/FanControl"][0] == 248
    assert stamped["/Mcu/FanControl"][1] > 0


def test_cliff_trigger_param_and_curve():
    """地检触发按四个方向拆位：左/前左/前右/右。"""
    from core.apos import catalog
    group = next(g for g in catalog.PARAM_GROUPS if g["name"] == "地检")
    names = [it["name"] for it in group["items"]]
    assert names[:4] == ["左边跌落", "右边跌落", "前左跌落", "前右跌落"]
    assert "/Mcu/CliffTrigger" not in catalog.all_param_paths()

    def val(name, mask):
        it = next(it for it in group["items"] if it["name"] == name)
        return catalog.display_value(it, {"ok": {"/Mcu/CliffTrigger": mask}})

    # 用户逐个方向实测的位序：bit0=前左 bit1=左 bit2=右 bit3=前右
    for name, bit in (("前左跌落", 0), ("左边跌落", 1),
                      ("右边跌落", 2), ("前右跌落", 3)):
        assert val(name, 1 << bit) == "1"
        for other, obit in (("前左跌落", 0), ("左边跌落", 1),
                            ("右边跌落", 2), ("前右跌落", 3)):
            if other != name:
                assert val(other, 1 << bit) == "0"
    # 12 = 1100 → 右边+前右
    assert val("右边跌落", 12) == "1" and val("前右跌落", 12) == "1"
    assert val("左边跌落", 12) == "0" and val("前左跌落", 12) == "0"
    page = next(p for p in catalog.CURVE_PAGES if p["name"] == "地检")
    titles = {c["title"] for g in page["groups"] for c in g["curves"]}
    assert {"左边跌落", "右边跌落", "前左跌落", "前右跌落"} <= titles


def test_odometer_displayed_in_meters():
    from core.apos import catalog
    group = next(g for g in catalog.PARAM_GROUPS if g["name"] == "主动轮")
    item = next(it for it in group["items"] if it["name"] == "左主动轮里程计")
    assert item["unit"] == "m" and item["scale"] == 0.001
    assert catalog.display_value(
        item, {"ok": {"/Mcu/LeftOdometer": 1147149}}) == "1147.15"


def test_realtime_table_has_drive_wheel_group():
    from core.apos import catalog
    group = next(g for g in catalog.PARAM_GROUPS if g["name"] == "主动轮")
    names = [it["name"] for it in group["items"]]
    assert "左主动轮速度" in names
    assert "左主动轮里程计" in names
    assert "打滑系数" in names
    assert "/Mcu/LeftOdometer" not in catalog.all_param_paths()


def test_catalog_keeps_mcu_paths_out_of_apos_reads():
    from core.apos import catalog
    assert "/Mcu/FanControl" not in catalog.all_param_paths()
    assert "/Mcu/SideBrushControl" not in catalog.all_monitor_paths()
    page = next(p for p in catalog.CURVE_PAGES if p["name"] == "边刷")
    assert "/Mcu/SideBrushControl" not in catalog.page_curve_paths(page)
    # 但曲线定义里仍然有，用于画图
    titles = [c["path"] for g in page["groups"] for c in g["curves"]]
    assert "/Mcu/SideBrushControl" in titles
