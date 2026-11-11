# -*- coding: utf-8 -*-
"""故障模拟目录（首批常用项）。

机器人侧没有“直接设置故障”的属性；厂商 AutopackDebugger 的模拟按钮都是发一个
APOS 事件：

    /Common/SetCmd = {Cmd: "PostEvent",
                      Params: {Topic: ..., Event: ..., Params: {}}}

`UploadStream.post_event()` 负责发送。这里只收录首批常用、可在设备参数里观察到
变化的故障；验证通过后再按厂商的 100+ 条指令补全。
"""

#: 小组名（UI 分组用）
GROUP_HARDWARE = "硬件故障"
GROUP_SENSOR = "传感器"
GROUP_CLEAN = "清洁/水箱"
GROUP_SYSTEM = "电量与状态"

#: key -> {name, group, topic, event, hint}
FAULTS = [
    {"key": "fan_stuck", "name": "风机堵转", "group": GROUP_HARDWARE,
     "topic": "/hardware/error", "event": "error_fan_stuck"},
    {"key": "left_wheel_stuck", "name": "左轮堵转", "group": GROUP_HARDWARE,
     "topic": "/hardware/error", "event": "error_left_wheel_stuck"},
    {"key": "right_wheel_stuck", "name": "右轮堵转", "group": GROUP_HARDWARE,
     "topic": "/hardware/error", "event": "error_right_wheel_stuck"},
    {"key": "side_brush_stuck", "name": "边刷堵转", "group": GROUP_HARDWARE,
     "topic": "/hardware/error", "event": "error_side_brush_stuck"},
    {"key": "middle_brush_stuck", "name": "中刷堵转", "group": GROUP_HARDWARE,
     "topic": "/hardware/error", "event": "error_middle_brush_stuck"},
    {"key": "drop", "name": "地检异常", "group": GROUP_SENSOR,
     "topic": "/hardware/error", "event": "error_drop"},
    {"key": "collision_stuck", "name": "前撞异常", "group": GROUP_SENSOR,
     "topic": "/hardware/error", "event": "error_collision_stuck"},
    {"key": "dust_box_water_box_out", "name": "尘盒/水箱取出",
     "group": GROUP_CLEAN,
     "topic": "/event/robot_monitor", "event": "dust_box_water_box_out"},
    {"key": "water_box_empty", "name": "水箱缺水", "group": GROUP_CLEAN,
     "topic": "/hardware/error", "event": "error_water_box_empty"},
    {"key": "garbage_box_full", "name": "垃圾盒已满", "group": GROUP_CLEAN,
     "topic": "/event/onboard_ui", "event": "garbage_box_full"},
    {"key": "low_battery_clean_on_charger",
     "name": "低电量提示清洁", "group": GROUP_SYSTEM,
     "topic": "/event/onboard_ui",
     "event": "low_battery_to_clean_on_charger"},
]

_BY_KEY = {f["key"]: f for f in FAULTS}

#: 厂商模拟指令里的准确主题(用户 2026-11-11：界面不再让用户选，工具自动判断)。
#: 从 AutopackDebugger 的 Simulation.PostEvent 反编译结果提取；不在表里的
#: 事件退回 infer_topic() 的按名猜测。
EVENT_TOPICS = {
    "app_sweeper_standby_timeout_go_back": "/event/onboard_ui",
    "back_charger_fail": "/event/onboard_ui",
    "back_charger_lost_pose": "/event/onboard_ui",
    "back_wash_paused": "/event/onboard_ui",
    "base_station_sewage_tank_block": "/event/onboard_ui",
    "base_station_without_dust_cover": "/event/onboard_ui",
    "charge_ok_resume_clean": "/event/onboard_ui",
    "charge_ok_resume_explore_map": "/event/onboard_ui",
    "clean_cannot_arrive": "/event/onboard_ui",
    "clean_finish": "/event/onboard_ui",
    "clean_lost_pose": "/event/onboard_ui",
    "collect_dust_done": "/event/onboard_ui",
    "collect_dust_full": "/event/onboard_ui",
    "collect_dust_no_bag": "/event/onboard_ui",
    "collect_dust_no_fan": "/event/onboard_ui",
    "distribution_network_timeout": "/event/network",
    "dry_mop_fan_error": "/event/onboard_ui",
    "dust_box_water_box_out": "/event/robot_monitor",
    "enter_silent_mode": "/event/onboard_ui",
    "error_battery_discharge_high_temperature": "/hardware/error",
    "error_battery_discharge_low_temperature": "/hardware/error",
    "error_battery_disconnect": "/hardware/error",
    "error_battery_high_temperture": "/hardware/error",
    "error_battery_low_temperture": "/hardware/error",
    "error_collision_stuck": "/hardware/error",
    "error_double_rotate_rag_motor_open_short_circuit": "/hardware/error",
    "error_double_rotate_rag_motor_stuck": "/hardware/error",
    "error_drop": "/hardware/error",
    "error_fan_stuck": "/hardware/error",
    "error_front_wall_dirty": "/hardware/error",
    "error_garbage_box_full_out": "/hardware/error",
    "error_kit_arm_motor_err": "/hardware/error",
    "error_kit_fan_err": "/hardware/error",
    "error_kit_l_rotate_rag_motor_err": "/hardware/error",
    "error_kit_left_wheel_err": "/hardware/error",
    "error_kit_lidar_err": "/hardware/error",
    "error_kit_lidar_obscured": "/hardware/error",
    "error_kit_lidar_speed": "/hardware/error",
    "error_kit_lidar_stuck": "/hardware/error",
    "error_kit_lidar_tilt": "/hardware/error",
    "error_kit_line_laser_error": "/hardware/error",
    "error_kit_middle_brush_err": "/hardware/error",
    "error_kit_mop_motor_err": "/hardware/error",
    "error_kit_pwm_water_pump_err": "/hardware/error",
    "error_kit_r_rotate_rag_motor_err": "/hardware/error",
    "error_kit_right_wheel_err": "/hardware/error",
    "error_kit_rotate_rag_motor_err": "/hardware/error",
    "error_kit_side_brush_err": "/hardware/error",
    "error_kit_universal_wheel_err": "/hardware/error",
    "error_kit_vibration_motor_err": "/hardware/error",
    "error_kit_water_pump_err": "/hardware/error",
    "error_l_rotate_rag_motor_stuck": "/hardware/error",
    "error_left_wheel_stuck": "/hardware/error",
    "error_lidar_protective_cover": "/hardware/error",
    "error_line_laser_wall_follow_error": "/hardware/error",
    "error_middle_brush_not_in_place": "/hardware/error",
    "error_middle_brush_stuck": "/hardware/error",
    "error_mop_motor_stuck": "/hardware/error",
    "error_physical_trapped": "/hardware/error",
    "error_psd_dirty": "/hardware/error",
    "error_pwm_water_pump_stuck": "/hardware/error",
    "error_r_rotate_rag_motor_stuck": "/hardware/error",
    "error_right_wheel_stuck": "/hardware/error",
    "error_rocker_switch_off": "/hardware/error",
    "error_rotate_rag_motor_stuck": "/hardware/error",
    "error_sewage_tank_full": "/hardware/error",
    "error_side_brush_stuck": "/hardware/error",
    "error_side_brush_telescoping_in": "/hardware/error",
    "error_side_brush_telescoping_out": "/hardware/error",
    "error_three_in_one_mop_motor_open_short_circuit": "/hardware/error",
    "error_three_in_one_mop_motor_stuck": "/hardware/error",
    "error_tilt_do_task": "/hardware/error",
    "error_ultrasonic_carpet_err": "/hardware/error",
    "error_vibration_motor_stuck": "/hardware/error",
    "error_virtual_trapped": "/hardware/error",
    "error_water_box_empty": "/hardware/error",
    "exit_silent_mode": "/event/onboard_ui",
    "explore_map_finish_back_charger": "/event/onboard_ui",
    "firmware_upgrade_fail": "/event/onboard_ui",
    "forbid_mode_not_resume_clean": "/event/onboard_ui",
    "garbage_box_full": "/event/onboard_ui",
    "incorrect_wifi_passwd": "/event/network",
    "line_laser_avoid_obstacle_dirty": "/hardware/error",
    "line_laser_wall_follow_dirty": "/hardware/error",
    "low_bat_need_recharge": "/event/robot_monitor",
    "low_bat_poweroff": "/event/onboard_ui",
    "low_bat_time_task_disable": "/event/onboard_ui",
    "low_battery_to_clean": "/event/onboard_ui",
    "low_battery_to_clean_on_charger": "/event/onboard_ui",
    "mop_in": "/event/robot_monitor",
    "mop_out": "/event/robot_monitor",
    "opt_during_upgrade": "/event/onboard_ui",
    "pickup": "/hardware/error",
    "restart_work_and_build_map": "/event/onboard_ui",
    "restart_work_and_explore_map": "/event/onboard_ui",
    "robot_battery_temperature_error": "/hardware/error",
    "rug_start_error": "/hardware/error",
    "start_form_forbid_area": "/hardware/error",
    "station_clean_water_pump_fault": "/event/onboard_ui",
    "station_clean_water_pump_open": "/event/onboard_ui",
    "station_clean_water_pump_short": "/event/onboard_ui",
    "station_detergent_not_enough": "/event/onboard_ui",
    "station_sewage_pump_fault": "/event/onboard_ui",
    "station_sewage_pump_open": "/event/onboard_ui",
    "station_sewage_pump_short": "/event/onboard_ui",
    "station_water_valve_open": "/event/onboard_ui",
    "station_water_valve_short": "/event/onboard_ui",
    "time_task_disable": "/event/onboard_ui",
    "time_task_disable_in_silent_mode": "/event/onboard_ui",
    "time_to_work_start": "/event/onboard_ui",
    "time_total_clean_start": "/event/onboard_ui",
    "try_poweroff_on_charger": "/event/onboard_ui",
    "water_box_mop_in": "/event/robot_monitor",
}

_LED_KEYS = ("rgb_led__home", "rgb_led__start", "rgb_led__power",
             "rgb_led__error", "rgb_led__ring")


def parse_verification(lines, event):
    """从设备日志里解析某故障的语音序号/灯显/故障码。

    lines: [(ts, text)]（McuStream.log_since 的返回）。
    返回 {beep, led: {key: effect}, code, matched: [相关日志行]}。
    """
    import re
    beep = None
    file_beep = None          # fileName: 0923.mp3 —— 仅兜底, beep 优先
    code = None
    led = {}
    matched = []
    evidence = []
    in_led_block = False
    for _ts, line in lines:
        low = line.lower()
        if event in line:
            matched.append(line)
            # 只保留能说明"事件名/语音/灯显/故障码"的日志行; 过滤 id/JSON 回声
            if ("application_frame" in low
                    or "led_show_base" in low or "rgb_led" in low
                    or "musicplay" in low or "filename:" in low
                    or "code=" in low or "keylog" in low):
                evidence.append(line)
            # 灯显明细是后续几行 JSON: data: { / "rgb_led__home" : "white1_breathing"
            if "led_show_base" in low and "data:" in low:
                in_led_block = True
            # 两种固件日志都见过: `beep:066` / `beep: 0923` / `beep:`(空)
            m = re.search(r"beep:\s*(\d+)", line)
            if m and beep is None:
                beep = m.group(1)
            # 语音号也可能只出现在音乐播放行; 先记下, 最后没有 beep 才用它
            m = re.search(r"fileName:\s*(\d+)\.mp3", line)
            if m and file_beep is None:
                file_beep = m.group(1)
            m = re.search(r"code=(\d+)", line)
            if m and code is None:
                code = m.group(1)
        elif in_led_block:
            # 灯显 JSON 的续行没有事件名, 但它们就是该事件的灯显参数
            if re.search(r'"rgb_led__\w+"\s*:\s*"', line):
                evidence.append(line)
            else:
                in_led_block = False
        if any(k in line for k in ("led_show_base", "rgb_led",
                                   "lights_effect_set", "RGB LED")):
            for key in _LED_KEYS:
                m = re.search(r'"%s"\s*:\s*"([^"]*)"' % key, line)
                if m and m.group(1) and key not in led:
                    led[key] = m.group(1)
            if "RGB LED show_mode" in line and not led:
                led["show"] = line.strip()[-80:]
    return {"beep": beep or file_beep, "led": led, "code": code,
            "matched": matched[-8:], "evidence": evidence[-12:]}


def highlight_log_line(line):
    """日志行转 RichText：事件名/语音序号/灯显/故障码加颜色（其余保持原样）。

    用户 2026-11-11：日志里对应的参数内容要标记出来。
    """
    import html
    import re
    s = html.escape(str(line or ""))

    def mark(pattern, color):
        nonlocal s

        def repl(m):
            return (f'<span style="color:{color};font-weight:600">'
                    f'{m.group(0)}</span>')

        s = re.sub(pattern, repl, s)

    mark(r"event(?:_id)?:?\s*[A-Za-z0-9_]+", "#2563eb")   # 事件名
    mark(r"beep:\s*\d+", "#d97706")                        # 语音序号
    mark(r"fileName:\s*\d+\.mp3", "#d97706")               # 语音文件兜底
    mark(r"&quot;rgb_led__\w+&quot;\s*:\s*&quot;\w+&quot;",
         "#7c3aed")                                        # 灯显
    mark(r"code=\d+", "#dc2626")                           # 故障码
    return s


def load_event_config(path):
    """读取出包的事件配置 JSON → {identifier: {name,sound,led}}。

    配置里每个事件: sound.identifier 是期望语音序号; rgb_hardware_function_set
    给出各灯设备(rgb_led__start/home) 的期望灯效。
    """
    import json
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    out = {}
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        ident = str(item.get("identifier") or "").strip()
        if not ident:
            continue
        led = {}
        led_names = {}
        for dev in item.get("rgb_hardware_function_set") or []:
            if not isinstance(dev, dict):
                continue
            effect_obj = dev.get("rgb_light_effect") or {}
            effect = effect_obj.get("identifier")
            dev_id = dev.get("identifier")
            if dev_id and effect:
                led[dev_id] = effect
                led_names[dev_id] = {
                    "device": str(dev.get("name") or dev_id),
                    "effect": str(effect_obj.get("name") or effect),
                    "effect_id": effect,
                }
        out[ident] = {
            "name": str(item.get("name") or ident),
            "sound": str((item.get("sound") or {}).get("identifier") or ""),
            "led": led,
            "led_names": led_names,
        }
    return out


def _short_device_name(name, dev_id=""):
    """配置里是「RGB回充灯设备」这种长名; 显示时用「回充灯 / 电源灯」。"""
    if dev_id == "rgb_led__home":
        return "回充灯"
    if dev_id == "rgb_led__start":
        return "电源灯"
    s = str(name or "")
    if s.startswith("RGB"):
        s = s[3:]
    if s.endswith("设备"):
        s = s[:-2]
    return s or dev_id


def led_name_maps(expected):
    """汇总配置里的灯设备/灯效中文名: ({dev_id: 中文名}, {effect_id: 中文名})。"""
    dev_names = {}
    effect_names = {}
    for exp in (expected or {}).values():
        for dev, info in (exp.get("led_names") or {}).items():
            dev_names.setdefault(
                dev, _short_device_name(info.get("device"), dev))
            effect_names.setdefault(info.get("effect_id") or "",
                                    info.get("effect") or "")
    return dev_names, effect_names


def led_display(led, expected=None):
    """{rgb_led__home: red_slow_flick} → "RGB回充灯设备=红灯慢闪"（中文）。"""
    dev_names, effect_names = led_name_maps(expected)
    parts = []
    for dev, effect in (led or {}).items():
        parts.append(f"{dev_names.get(dev, dev)}="
                     f"{effect_names.get(effect, effect)}")
    return "、".join(parts)


def compare(actual, expected, event):
    """实际解析结果 vs 事件配置。返回 {ok, problems, expected}。

    ok: True=一致 / False=不一致 / None=无法判定（配置里没有该事件，
    或配置里没有配语音/灯显）。触发事件本身就是实际行为，不需要人工
    再"以实测为准"（用户 2026-11-11）。
    """
    exp = dict((expected or {}).get(event) or {})
    if not exp:
        return {"ok": None, "problems": ["事件配置里没有该事件"],
                "expected": None}
    want = exp.get("sound") or ""
    want_led = exp.get("led") or {}
    if not want and not want_led:
        # 配置里有这个事件，但没配语音/灯显（用户 2026-11-11：应显示"未配置"）
        return {"ok": None, "problems": ["事件配置里未配置语音/灯显"],
                "expected": exp}
    problems = []
    got = str(actual.get("beep") or "")
    if want and got != want:
        problems.append(f"语音序号 期望 {want}, 实际 {got or '未捕获'}")
    want_led = exp.get("led") or {}
    got_led = actual.get("led") or {}
    dev_names, effect_names = led_name_maps(expected)
    for dev, effect in want_led.items():
        got_effect = got_led.get(dev)
        if got_effect != effect:
            dev_text = dev_names.get(dev, dev)
            want_text = effect_names.get(effect, effect)
            got_text = effect_names.get(got_effect, got_effect) or "未捕获"
            problems.append(
                f"{dev_text} 期望 {want_text}, 实际 {got_text}")
    return {"ok": not problems, "problems": problems, "expected": exp}


def infer_topic(event):
    """按事件名猜主题：厂商模拟表里没有的事件才用这个兜底。"""
    e = str(event or "").strip()
    if e.startswith("error") or e.startswith("pickup"):
        return "/hardware/error"
    if any(k in e for k in ("dust_box", "water_box_out", "tank")):
        return "/event/robot_monitor"
    return "/event/onboard_ui"


def topic_for(event):
    """触发某事件应使用的主题：优先厂商模拟表，其次按名猜测。

    界面不再暴露主题选择（用户 2026-11-11）：调用方只管给事件名。
    """
    e = str(event or "").strip()
    return EVENT_TOPICS.get(e) or infer_topic(e)


def has_simulation(event):
    """该事件是否有厂商给出的模拟指令(没有的话设备可能不响应 PostEvent)。"""
    return str(event or "").strip() in EVENT_TOPICS


#: 事件配置里没有中文名的事件，用这张表兜底（用户 2026-11-11：没有中文名的翻译掉）
EVENT_NAMES = {
    "app_sweeper_standby_timeout_go_back": "扫拖机待机超时回充",
    "back_charger_fail": "回充失败事件",
    "back_charger_lost_pose": "回充定位失败事件",
    "back_wash_paused": "暂停回洗拖布事件",
    "base_station_sewage_tank_block": "基站污水箱堵住事件",
    "base_station_without_dust_cover": "基站未装尘袋盖",
    "charge_ok_resume_clean": "充电已完成继续清扫事件",
    "charge_ok_resume_explore_map": "充电已完成继续探索建图事件",
    "clean_cannot_arrive": "清扫区域无法到达事件",
    "clean_finish": "清扫完成事件",
    "clean_lost_pose": "清扫定位失败事件",
    "collect_dust_done": "集尘完成事件",
    "collect_dust_full": "工作站尘袋已满事件",
    "collect_dust_no_bag": "工作站未安装尘袋事件",
    "collect_dust_no_fan": "工作站风机异常事件",
    "distribution_network_timeout": "配网超时事件",
    "dry_mop_fan_error": "烘干风机故障事件",
    "dust_box_water_box_out": "尘盒水箱移除事件",
    "enter_silent_mode": "进入勿扰模式事件",
    "error_battery_discharge_high_temperature": "电池放电高温",
    "error_battery_discharge_low_temperature": "电池放电低温",
    "error_battery_disconnect": "电池未连接异常",
    "error_battery_high_temperture": "电池充电高温报警",
    "error_battery_low_temperture": "电池充电低温报警",
    "error_collision_stuck": "前撞异常",
    "error_double_rotate_rag_motor_open_short_circuit":
        "双旋转抹布电机开路或短路",
    "error_double_rotate_rag_motor_stuck": "双旋转抹布电机卡住",
    "error_drop": "地检异常",
    "error_fan_stuck": "风机卡住",
    "error_front_wall_dirty": "检测到正前方避障传感器脏事件",
    "error_garbage_box_full_out": "取出已满尘盒事件",
    "error_kit_arm_motor_err": "机械臂电机异常",
    "error_kit_fan_err": "风机开路或短路",
    "error_kit_l_rotate_rag_motor_err": "左拖布开路或短路",
    "error_kit_left_wheel_err": "左轮开路或短路",
    "error_kit_lidar_err": "雷达异常事件",
    "error_kit_lidar_obscured": "雷达被遮挡事件",
    "error_kit_lidar_speed": "雷达控速异常事件",
    "error_kit_lidar_stuck": "雷达堵转事件",
    "error_kit_lidar_tilt": "开始建图倾斜报错事件",
    "error_kit_line_laser_error": "线激光串口通信异常",
    "error_kit_middle_brush_err": "中刷开路或短路",
    "error_kit_mop_motor_err": "拖布电机异常",
    "error_kit_pwm_water_pump_err": "水泵电机开路或短路",
    "error_kit_r_rotate_rag_motor_err": "右拖布开路或短路",
    "error_kit_right_wheel_err": "右轮开路或短路",
    "error_kit_rotate_rag_motor_err": "旋转拖布电机异常",
    "error_kit_side_brush_err": "边刷开路或短路",
    "error_kit_universal_wheel_err": "万向轮异常",
    "error_kit_vibration_motor_err": "振动电机异常",
    "error_kit_water_pump_err": "水泵异常",
    "error_l_rotate_rag_motor_stuck": "左拖布卡住",
    "error_left_wheel_stuck": "左轮卡住",
    "error_lidar_protective_cover": "雷达上盖异常",
    "error_line_laser_wall_follow_error": "沿墙线激光异常",
    "error_middle_brush_not_in_place": "中扫未安装到位",
    "error_middle_brush_stuck": "中刷卡住",
    "error_mop_motor_stuck": "拖布电机堵转",
    "error_physical_trapped": "机器被困事件",
    "error_psd_dirty": "沿墙传感器异常",
    "error_pwm_water_pump_stuck": "PWM 水泵堵转",
    "error_r_rotate_rag_motor_stuck": "右拖布卡住",
    "error_right_wheel_stuck": "右轮卡住",
    "error_rocker_switch_off": "请打开船型开关",
    "error_rotate_rag_motor_stuck": "旋转拖布电机堵转",
    "error_sewage_tank_full": "污水箱已满",
    "error_side_brush_stuck": "边刷卡住",
    "error_side_brush_telescoping_in": "伸缩边刷无法收回",
    "error_side_brush_telescoping_out": "伸缩边刷无法外摆",
    "error_three_in_one_mop_motor_open_short_circuit":
        "三合一拖布电机开路/短路",
    "error_three_in_one_mop_motor_stuck": "三合一拖布电机堵转",
    "error_tilt_do_task": "请勿倾斜面上启动事件",
    "error_ultrasonic_carpet_err": "超声波故障",
    "error_vibration_motor_stuck": "振动电机堵转",
    "error_virtual_trapped": "机器禁区被困事件",
    "error_water_box_empty": "检测到水箱中水量不足事件",
    "exit_silent_mode": "退出勿扰模式事件",
    "explore_map_finish_back_charger": "探索建图完成后回充事件",
    "firmware_upgrade_fail": "固件升级失败事件",
    "forbid_mode_not_resume_clean": "禁区模式未恢复清扫",
    "garbage_box_full": "尘盒满请清理尘盒事件",
    "incorrect_wifi_passwd": "配网验证错误事件",
    "line_laser_avoid_obstacle_dirty": "线激光避障传感器被遮挡",
    "line_laser_wall_follow_dirty": "线激光沿墙传感器被遮挡",
    "low_bat_need_recharge": "低电量请充电",
    "low_bat_poweroff": "低电关机事件",
    "low_bat_time_task_disable": "低电量定时任务禁用",
    "low_battery_to_clean": "电量太低无法启动清扫事件",
    "low_battery_to_clean_on_charger": "电量太低无法在充电桩上启动清扫事件",
    "mop_in": "拖布安装事件",
    "mop_out": "拖布移除事件",
    "opt_during_upgrade": "升级中，请勿操作事件",
    "pickup": "离地检测异常",
    "restart_work_and_build_map": "定位失败,重新建图事件",
    "restart_work_and_explore_map": "定位失败,重新探索建图事件",
    "robot_battery_temperature_error": "电池包温度异常",
    "rug_start_error": "地毯启动报错事件",
    "start_form_forbid_area": "在禁区中无法启动工作事件",
    "station_clean_water_pump_fault": "基站清水泵故障",
    "station_clean_water_pump_open": "基站清水泵开路",
    "station_clean_water_pump_short": "基站清水泵短路",
    "station_detergent_not_enough": "基站清洁液不足",
    "station_sewage_pump_fault": "基站污水泵故障",
    "station_sewage_pump_open": "基站污水泵开路",
    "station_sewage_pump_short": "基站污水泵短路",
    "station_water_valve_open": "基站电磁阀开路",
    "station_water_valve_short": "基站电磁阀短路",
    "time_task_disable": "定时任务已禁用",
    "time_task_disable_in_silent_mode": "静音模式下定时任务禁用",
    "time_to_work_start": "预约清扫开始事件",
    "time_total_clean_start": "累计清扫开始",
    "try_poweroff_on_charger": "充电桩上无法关机事件",
    "water_box_mop_in": "水箱拖布安装",
}


def event_name(event, expected=None):
    """事件的中文名：配置里的名称 > 内置翻译 > identifier。"""
    e = str(event or "").strip()
    info = (expected or {}).get(e) or {}
    return str(info.get("name") or "").strip() or EVENT_NAMES.get(e) or e


def _preset_group(event):
    """给补充事件分个类，下拉里都带分组前缀。"""
    e = str(event or "").lower()
    if "network" in e or "wifi" in e or "distribution" in e:
        return "网络与配网"
    if "trapped" in e:            # 机器被困属于状态类, 不是硬件故障
        return "状态故障"
    if (e.startswith("error") or "dirty" in e or "trapped" in e
            or "stuck" in e or "lidar" in e):
        return GROUP_HARDWARE
    if any(k in e for k in ("dust", "mop", "water", "tank", "station",
                            "clean", "wash", "rag", "brush", "collect",
                            "dry", "detergent", "pump", "sewage", "valve")):
        return GROUP_CLEAN
    if (e.startswith(("low_bat", "charge", "back_charger", "try_poweroff"))
            or "battery" in e):
        return GROUP_SYSTEM
    return "状态与任务"


def presets(expected=None):
    """「常用故障」下拉的全部条目：先列常用项，再补厂商模拟指令里的其它事件。

    返回 [{label, event, group, name}]；配置里有中文名就用中文名，
    否则用内置翻译（用户 2026-11-11：剩下的也补齐）。
    """
    out = []
    seen = set()
    for f in FAULTS:
        out.append({"label": f'{f["group"]} · {f["name"]}',
                    "event": f["event"], "group": f["group"],
                    "name": f["name"]})
        seen.add(f["event"])
    extra = []
    for event in sorted(EVENT_TOPICS):
        if event in seen:
            continue
        info = (expected or {}).get(event) or {}
        name = str(info.get("name") or "").strip() or EVENT_NAMES.get(event) \
            or event
        group = _preset_group(event)
        extra.append({"label": f"{group} · {name}", "event": event,
                      "group": group, "name": name})
    order = [GROUP_HARDWARE, GROUP_SENSOR, GROUP_CLEAN, GROUP_SYSTEM,
             "网络与配网", "状态故障", "状态与任务"]
    extra.sort(key=lambda p: (order.index(p["group"])
                              if p["group"] in order else len(order),
                              p["name"]))
    return out + extra


def get(key):
    """按 key 取故障定义；不存在返回 None。"""
    return _BY_KEY.get(str(key or ""))


def groups():
    """[(小组名, [fault...])]，保持 FAULTS 里的顺序。"""
    out = []
    for f in FAULTS:
        for name, items in out:
            if name == f["group"]:
                items.append(f)
                break
        else:
            out.append((f["group"], [f]))
    return out
