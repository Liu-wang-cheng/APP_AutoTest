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


def test_log_frames_are_kept_for_fault_verification():
    """1001/0 文本帧进日志缓冲, 故障模拟用它验证语音/灯显。"""
    s = McuStream()
    text = ("11-11 7:14:37.944/SW D/application_frame.cpp:241 "
            "event:error_fan_stuck, beep:066,\r\n"
            '11-11 7:14:37.950/SW D/led_show_base.cpp:261 '
            '"rgb_led__home" : "red_slow_flick"\r\n')
    frame = struct.pack("<HH", 1001, 0) + b"\x00" * 16 + text.encode()
    s._decode(frame)
    lines = s.log_since(0)
    assert any("beep:066" in line for _t, line in lines)
    assert any("rgb_led__home" in line for _t, line in lines)


def test_parse_fault_verification_beep_led_code():
    from core.apos.faults import parse_verification
    lines = [
        (1.0, "id:error_fan_stuck len:3"),          # 无关键信息的回声, 应被过滤
        (1.0, "application_frame.cpp:241 event:error_fan_stuck, beep:066, "
              "ismust_play:1"),
        (1.0, 'led_show_base.cpp:49 event_id: error_fan_stuck, '
              'silentmode: 0'),
        (1.0, 'led_show_base.cpp:261 event_id: error_fan_stuck, data: '
              '"rgb_led__home" : "red_slow_flick"'),
        (1.0, 'led_show_base.cpp:261 event_id: error_fan_stuck, data: '
              '"rgb_led__start" : "red_flick_3"'),
        (1.0, "apos_adapter.cpp:3481 msg=error_fan_stuck code=926"),
    ]
    res = parse_verification(lines, "error_fan_stuck")
    assert res["beep"] == "066"
    assert res["led"]["rgb_led__home"] == "red_slow_flick"
    assert res["led"]["rgb_led__start"] == "red_flick_3"
    assert res["code"] == "926"
    assert len(res["matched"]) == 6
    # 事件/语音/灯显/故障码关键行保留, id 回声被过滤
    ev = "\n".join(res["evidence"])
    assert "beep:066" in ev and "rgb_led__home" in ev
    assert "code=926" in ev and "id:error_fan_stuck" not in ev


def test_log_frames_join_chunks_keep_full_timestamp():
    """文本帧按字节切块, 跨帧的行要拼回完整时间戳(用户 2026-11-11)。"""
    s = McuStream()

    def chunk(text):
        return struct.pack("<HH", 1001, 0) + b"\x00" * 16 + text.encode()

    # 首帧从某行中间开始: 残行丢掉, 完整行保留开头时间
    s._decode(chunk(":5:11.100/SW D/tail_of_previous\r\n"
                    "11-11 7:5:11.103/SW D/led_show_base.cpp:49 "
                    "event_id: back_charger_fail\r\n"))
    lines = [line for _t, line in s.log_since(0)]
    assert lines == ["11-11 7:5:11.103/SW D/led_show_base.cpp:49 "
                     "event_id: back_charger_fail"]
    # 一行被拆到两帧: 拼起来后时间戳完整
    s._decode(chunk("11-11 7:5:12.001/SW D/application_frame.cpp:241 "
                    "event:error_fan"))
    assert len(s.log_since(0)) == 1
    s._decode(chunk("_stuck, beep:066\r\n"))
    lines = [line for _t, line in s.log_since(0)]
    assert lines[-1] == ("11-11 7:5:12.001/SW D/application_frame.cpp:241 "
                         "event:error_fan_stuck, beep:066")


def test_log_frame_payload_starts_at_20():
    """原始帧实测：文本从第 20 字节开始，行首 "11-11 16" 不能丢。"""
    text = ("11-11 16:11:14.580/SW D/application_frame.cpp:362 "
            "Recv event_id error_fan_stuck\n")
    frame = struct.pack("<HH", 1001, 0) + b"\x00" * 16 + text.encode()
    s = McuStream()
    s._decode(frame)
    line = s.log_since(0)[0][1]
    assert line.startswith("11-11 16:11:14.580/SW D/")


def test_highlight_log_line_marks_parameters():
    """日志里的事件名/语音序号/灯显/故障码要带颜色(用户 2026-11-11)。"""
    from core.apos.faults import highlight_log_line
    line = ('11-11 16:11:14.580/SW D/application_frame.cpp:362 '
            'Recv event_id error_fan_stuck, beep:066 '
            '"rgb_led__home" : "red_slow_flick" code=926')
    out = highlight_log_line(line)
    assert "color:#2563eb" in out and "event_id error_fan_stuck" in out
    assert "color:#d97706" in out and "beep:066" in out
    assert "color:#7c3aed" in out and "rgb_led__home" in out
    assert "color:#dc2626" in out and "code=926" in out
    # 普通文本不标色, HTML 会转义
    plain = highlight_log_line("hello <b>world</b>")
    assert plain == "hello &lt;b&gt;world&lt;/b&gt;"


def test_topic_for_uses_vendor_mapping_not_guess():
    """主题由工具自动判断: 厂商模拟表的准确主题优先于按名猜测。"""
    from core.apos.faults import has_simulation, topic_for
    # 按名猜会猜错的几个
    assert topic_for("distribution_network_timeout") == "/event/network"
    assert topic_for("base_station_sewage_tank_block") == "/event/onboard_ui"
    assert topic_for("low_bat_need_recharge") == "/event/robot_monitor"
    assert topic_for("mop_out") == "/event/robot_monitor"
    assert topic_for("error_drop") == "/hardware/error"
    # 厂商表里没有的事件: 退回按名猜
    assert has_simulation("error_drop") is True
    assert has_simulation("some_unknown_event") is False
    assert topic_for("some_unknown_event") == "/event/onboard_ui"


def test_parse_alternate_log_formats():
    """兼容 application_frame.cpp:225 / beep 为空 / 语音号只在 fileName 里的日志。"""
    from core.apos.faults import parse_verification
    lines = [
        (1.0, "11-11 7:14:38.150/SW D/application_frame.cpp:225 "
              "event:drainage_water_with_mop, beep:0923, ismust_play:1"),
        (1.0, "11-11 7:14:38.355/SW D/led_music_handle.cpp:347 "
              "<MusicPlay> event_id: drainage_water_with_mop, "
              "fileName: 0923.mp3"),
        (1.0, "11-11 7:14:37.944/SW D/application_frame.cpp:225 "
              "event:app_sweeper_not_low_battery_back_chager, beep:, "
              "ismust_play:0"),
        (1.0, "11-11 7:14:42.226/SW D/application_frame.cpp:225 "
              "event:charging, beep:0310, ismust_play:0"),
    ]
    res = parse_verification(lines, "drainage_water_with_mop")
    assert res["beep"] == "0923"
    # 只有 fileName、beep 为空的 event
    lines2 = [
        (1.0, "application_frame.cpp:225 event:some_event, beep:, "
              "ismust_play:1"),
        (1.0, "led_music_handle.cpp:347 <MusicPlay> event_id: some_event, "
              "fileName: 0123.mp3"),
    ]
    assert parse_verification(lines2, "some_event")["beep"] == "0123"
    # beep 优先: 即使音乐播放行先出现, 后面的 beep 也覆盖文件名兜底
    lines3 = [
        (1.0, "led_music_handle.cpp:347 <MusicPlay> event_id: evt, "
              "fileName: 0123.mp3"),
        (1.0, "application_frame.cpp:225 event:evt, beep:0310, "
              "ismust_play:1"),
    ]
    assert parse_verification(lines3, "evt")["beep"] == "0310"
    # beep 为空且没有音乐文件: 保持未捕获
    assert parse_verification(lines, "app_sweeper_not_low_battery_back_chager"
                              )["beep"] is None
    assert parse_verification(lines, "charging")["beep"] == "0310"


def test_event_config_load_and_compare(tmp_path):
    """按出包事件配置验证: 语音/灯显一致/不一致/配置里没有(未配置)。"""
    import json
    from core.apos import faults
    p = tmp_path / "events.json"
    p.write_text(json.dumps([{
        "identifier": "error_fan_stuck", "name": "风机卡住",
        "rgb_hardware_function_set": [
            {"identifier": "rgb_led__start", "name": "RGB电源灯设备",
             "rgb_light_effect": {"identifier": "red_slow_flick",
                                  "name": "红灯慢闪"}}],
        "sound": {"identifier": "066"},
    }], ensure_ascii=False), encoding="utf-8")
    expect = faults.load_event_config(str(p))
    assert expect["error_fan_stuck"]["sound"] == "066"
    # 灯显显示中文名
    assert faults.led_display(
        {"rgb_led__start": "red_slow_flick"}, expect) == \
        "电源灯=红灯慢闪"
    assert faults.led_display({"rgb_led__start": "all_off"}, expect) == \
        "电源灯=all_off"
    actual = {"beep": "066", "led": {"rgb_led__start": "red_slow_flick"}}
    assert faults.compare(actual, expect, "error_fan_stuck")["ok"] is True
    bad = {"beep": "999", "led": {"rgb_led__start": "all_off"}}
    cmp = faults.compare(bad, expect, "error_fan_stuck")
    assert cmp["ok"] is False and len(cmp["problems"]) == 2
    assert faults.compare(actual, expect, "error_drop")["ok"] is None
    # 配置里有事件、但没配语音/灯显 → 也是未配置(不算失败)
    p2 = tmp_path / "events2.json"
    p2.write_text(json.dumps([{
        "identifier": "empty_evt", "name": "频繁集尘",
        "rgb_hardware_function_set": [], "sound": {}}],
        ensure_ascii=False), encoding="utf-8")
    expect2 = faults.load_event_config(str(p2))
    cmp2 = faults.compare({}, expect2, "empty_evt")
    assert cmp2["ok"] is None
    assert "未配置语音/灯显" in cmp2["problems"][0]


def test_parse_led_json_continuation_lines():
    """灯显明细在后续几行 JSON 里, 日志显示和解析都要带上(用户 2026-11-11)。"""
    from core.apos.faults import parse_verification
    lines = [
        (1.0, "11-11 16:23:57.061/SW D/application_frame.cpp:241 "
              "event:add_water_start, beep:290, ismust_play:0"),
        (1.0, "11-11 16:23:57.063/SW D/led_show_base.cpp:261 "
              "event_id: add_water_start, must_show: 1, silentmode: 0, "
              "show_led: 1 data: {"),
        (1.0, '"rgb_led__home" : "white1_breathing",'),
        (1.0, '"rgb_led__start" : "all_off"'),
        (1.0, "11-11 16:23:57.939/CA D/app_rgb_led.cpp:47 "
              "RgbLedManagerHandle rgb_led_json_end_"),
    ]
    res = parse_verification(lines, "add_water_start")
    assert res["beep"] == "290"
    assert res["led"] == {"rgb_led__home": "white1_breathing",
                          "rgb_led__start": "all_off"}
    ev = "\n".join(res["evidence"])
    assert '"rgb_led__home" : "white1_breathing"' in ev
    assert '"rgb_led__start" : "all_off"' in ev


def test_presets_cover_all_vendor_simulation_events():
    """常用故障下拉补齐厂商模拟指令里的全部事件(用户 2026-11-11)。"""
    from core.apos import faults
    ps = faults.presets()
    events = {p["event"] for p in ps}
    assert set(faults.EVENT_TOPICS) <= events
    assert len(ps) == len(faults.EVENT_TOPICS)
    # 配置里有中文名时优先显示中文名
    ps2 = faults.presets({"water_box_mop_in": {"name": "水箱拖布安装"}})
    item = next(p for p in ps2 if p["event"] == "water_box_mop_in")
    # 补充事件也带分类前缀
    assert item["label"] == "清洁/水箱 · 水箱拖布安装"
    assert item["group"] == "清洁/水箱"
    # 配置里没有中文名的事件也要有中文翻译, 不能直接显示 identifier
    assert all(p["name"] != p["event"] for p in ps)
    assert faults.event_name("error_sewage_tank_full") == "污水箱已满"
    # 被困属于状态故障, 不是硬件故障
    for ev in ("error_physical_trapped", "error_virtual_trapped"):
        item = next(p for p in ps if p["event"] == ev)
        assert item["group"] == "状态故障"


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
