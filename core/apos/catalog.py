# -*- coding: utf-8 -*-
"""参数/曲线预设（来自 AutopackDebugger 的 BinpackParam.xml / CurveConfig.xaml）。

- 顶部常用参数（系统/电量/状态）手工整理，带单位与中文名；
- 其余 77 项来自 `BinpackParam.xml`（`vendor_params.py`），按厂商分组展示；
- `field` 表示值是一个 JSON 字符串（如 /System/FirmwareInfo），需要从里面取字段；
- 扫描 / 采集只认识 `path`，UI 用 `display_value` 做字段解析和状态翻译。
"""
import json

from core.apos.client import BINARY_PATHS
from core.apos.vendor_params import VENDOR_PARAMS

GROUP_CN = {
    "RegGroup_HostControlStatus": "底盘控制",
    "RegGroup_Motor": "电机",
    "RegGroup_Battery": "电池寄存器",
    "RegGroup_DustDet": "灰尘检测",
    "RegGroup_MCUWorkStatus": "MCU 状态",
    "RegGroup_MCUFeedBack": "MCU 反馈",
    "RegGroup_OtherSensor": "其他传感器",
    "RegGroup_IMU": "IMU",
    "RegGroup_IMUCalValue": "IMU 校准",
    "RegGroup_KeyValue": "按键",
    "RegGroup_SystemStatus": "系统状态",
    "RegGroup_FramRate": "帧率",
    "VibrationMotor": "振动电机",
    "DoubleRotateRag": "双旋转拖布",
    "DrainTank": "排水箱",
    "UltrasonicSensorDet": "超声波",
    "KeyRotateRagLeft": "左旋转拖布",
    "KeyRotateRagRight": "右旋转拖布",
    "MotorThreeInOneMop": "三合一拖布电机",
    "KeyArmMotorLocationIn": "边刷外扩",
    "MotorMopArm": "拖布外扩",
    "MotorMopArmStep": "拖布臂步进",
    "WaterBox": "水箱",
    "StretchingInPlace": "伸缩到位",
    "Fan": "风机",
    "MidLift": "中刷抬升",
    "SpinnerWheel": "万向轮",
    "IR": "红外",
    "Cliff": "地检",
    "Voice": "语音",
    "PointLaser": "点激光",
    "IrAlongWall": "沿墙红外",
    "CollectDecoder": "集尘解码",
    "KeyThreeInOneMopMotorInPlace": "三合一拖布到位",
    "KeySewageTank": "污水箱",
    "KeyCleaningDrumHall": "清洗鼓霍尔",
}

NAME_CN = {
    "CarrierWorkMode": "底盘模式",
    "Level": "档位",
    "Pwm": "PWM",
    "Current": "电流",
    "Speed": "速度",
    "AdapterVoltage": "适配器电压",
    "SystemCur": "系统电流",
    "BatteryTemp": "电池温度",
    "Prop_DustLevel": "灰尘等级",
    "Prop_DustAmount": "灰尘量",
    "FanSpeed": "风机转速",
    "FanCurrent": "风机电流",
    "AdcDifValue": "ADC 差值",
    "WaterTankStatus": "水箱状态",
    "IsStatusEmpty": "是否为空",
    "IsStatusFull": "是否已满",
    "DrainBoxStatus": "污水箱状态",
    "STATUS_SEWAGE_TANK_FULL": "污水箱已满",
    "IrLeft": "左红外",
    "IrFrontLeft": "左前红外",
    "IrFrontRight": "右前红外",
    "IrRight": "右红外",
    "IrBackRight": "右后红外",
    "IrBackLeft": "左后红外",
    "IrLeftTwo": "左红外2",
    "IrRightTwo": "右红外2",
    "VibrationStatus": "振动电机状态",
    "Param_MidLiftMotorAdc": "中刷抬升电机 ADC",
    "MotorMidLiftUpCheck": "中刷抬升到位",
    "PoleConnectStatus": "极片连接状态",
    "Volume": "音量",
    "AdcValue": "ADC 值",
    "DetectionResult": "超声波检测结果",
    "UltWorkStateFb": "超声波工作状态反馈",
    "UltWorkStateCtl": "超声波工作状态控制",
    "ProbeDetAdc": "探头检测 ADC",
    "DoubleRotateRagLevel": "双旋转拖布档位",
    "DoubleRotateRagPwm": "双旋转拖布 PWM",
    "DoubleRotateRagCurrent": "双旋转拖布电流",
    "DoubleRotateRagCurrent2": "双旋转拖布电流2",
    "DoubleRotateRagSpeed": "双旋转拖布速度",
    "KeyRotateRagLeftStatus": "左旋转拖布状态",
    "RotateRagLeftCurrent": "左旋转拖布电流",
    "RotateRagLeftPwm": "左旋转拖布 PWM",
    "RotateRagLeftUpCheck": "左旋转拖布抬升到位",
    "KeyRotateRagRightStatus": "右旋转拖布状态",
    "RotateRagRightCurrent": "右旋转拖布电流",
    "RotateRagRightPwm": "右旋转拖布 PWM",
    "RotateRagRightUpCheck": "右旋转拖布抬升到位",
    "MotorThreeInOneMopLevel": "三合一拖布档位",
    "MotorThreeInOneMopPwm": "三合一拖布 PWM",
    "MotorThreeInOneMopCurrent": "三合一拖布电流",
    "MotorThreeInOneMopSpeed": "三合一拖布速度",
    "KeyThreeInOneMopMotorInPlaceStatus": "三合一拖布到位状态",
    "KeyWheelLiftUpRightState": "右轮抬升状态",
    "KeyWheelLiftUpLeftState": "左轮抬升状态",
    "KeyStretchingInPlaceUp": "伸缩到位(上)",
    "KeyStretchingInPlaceDown": "伸缩到位(下)",
    "KeyStretchingInPlaceEx": "伸缩到位(外)",
    "KeyArmMotorLocationInState": "内位置在位",
    "KeyArmMotorLocationOutState": "外位置在位",
    "KeyArmMotorLocationCurrent": "电机电流",
    "KeyArmMotorLocationPwm": "电机PWM",
    "KeySewageTankState": "污水箱状态",
    "KeyCleaningDrumHallState": "清洗鼓霍尔状态",
    "KeyMopArmMotorLocationIn": "内摆到位",
    "KeyMopArmMotorLocationOut": "外摆到位",
    "KeyMopArmMotorCurrent": "电机电流",
    "Param_MopArmMotorPWM": "电机PWM",
    "MotorMopArmStepPWM": "拖布臂步进 PWM",
    "KeySpinnerWheelLiftUp": "万向轮抬升到位",
    "SpinnerWheelCurrent": "万向轮电流",
    "ActiveWheelCurrent": "主动轮电流",
    "ActiveWheelSpeed": "主动轮速度",
    "CliffData": "地检数据",
    "PointLaserData": "点激光数据",
}

VALUE_LABELS = {
    "work_status_idle": "空闲",
    "work_status_total_clean": "清扫中",
    "work_status_pause": "已暂停",
    "work_status_explore_map": "探索建图中",
    "work_status_build_map": "建图中",
    "work_status_dormant": "休眠",
    "work_status_charging": "充电中",
    "work_status_full_charging": "充满电",
    "work_status_go_charge": "回充中",
    "work_status_remote_control": "遥控中",
    "work_status_relocation": "重定位中",
    "work_status_clean": "清扫中",
    "work_status_dry_mop": "拖布烘干中",
    "work_status_wash_mop": "洗拖布",
    "work_status_assemble_mop": "装配拖布",
    "work_status_disassemble_mop": "拆卸拖布",
    "work_status_collect_dust": "集尘中",
    "work_status_cut_hair": "割毛发",
    "CARRIER_WORK_IDLE": "空闲",
    "CARRIER_WORK_NORMAL": "正常工作",
    "CARRIER_WORK_NULL": "无效",
    "CARRIER_WORK_REBOOT": "底盘重启",
    "CARRIER_WORK_SHUTDOWN": "底盘关机",
    "CARRIER_WORK_WAKE_UP_SOC": "SOC 唤醒",
    "CARRIER_WORK_CEC": "CEC 超低功耗",
}


def decode_ir_wall_distance(hex_text):
    """IrAlongWall ADC 差值数组 → 沿墙距离（mm）。

    厂商 SDK 的 UpdateIrAlongWallAdcDifData 把数组按 6 字节一组分成 3 段：
    沿墙距离 / 近接收灯ADC / 远接收灯ADC。这里取第一段的首个 uint16。
    """
    s = str(hex_text or "").strip()
    if len(s) % 2:
        s = s[:-1]
    try:
        data = bytes.fromhex(s)
    except ValueError:
        return None
    if len(data) < 2:
        return None
    return float(int.from_bytes(data[:2], "little", signed=True))


def decode_value(item, raw):
    """按 item/curve 里的 decode/scale 标记把原始值转成显示值。"""
    item = item or {}
    dec = item.get("decode")
    if dec == "ir_wall_distance":
        raw = decode_ir_wall_distance(raw)
    scale = item.get("scale")
    if scale is not None and raw is not None:
        try:
            raw = float(raw) * float(scale)
        except (TypeError, ValueError):
            pass
    return raw


def _format_binary(hex_text):
    """把二进制属性的十六进制串转成「N 通道 · 前几个值」的摘要。"""
    s = str(hex_text).strip()
    if len(s) % 2:
        s = s[:-1]
    try:
        data = bytes.fromhex(s)
    except ValueError:
        return s[:60]
    vals = [int.from_bytes(data[i:i + 2], "little", signed=True)
            for i in range(0, len(data) - 1, 2)]
    if not vals:
        return f"{len(data)} 字节"
    head = " ".join(str(v) for v in vals[:6])
    return f"{len(vals)} 通道 · {head}" + (" …" if len(vals) > 6 else "")


def translate_value(path, value):
    """把机器人返回的英文状态/布尔值翻成中文（只翻译能确定的值）。"""
    s = str(value).strip()
    if s in VALUE_LABELS:
        return VALUE_LABELS[s]
    if s.lower() in ("true", "false"):
        return "是" if s.lower() == "true" else "否"
    if ("Bool" in path or path.endswith("Existed")) and s in ("0", "1"):
        return "否" if s == "0" else "是"
    return s


CURATED_GROUPS = [
    {"name": "系统", "items": [
        {"name": "型号", "vendor_name": "Model", "path": "/System/FirmwareInfo", "field": "Model"},
        {"name": "固件版本", "vendor_name": "Version", "path": "/System/FirmwareInfo", "field": "Version"},
        {"name": "镜像版本", "vendor_name": "SocImageVersion", "path": "/System/FirmwareInfo", "field": "SocImageVersion"},
        {"name": "MCU版本", "vendor_name": "McuVersion", "path": "/System/FirmwareInfo", "field": "McuVersion"},
        {"name": "构建时间", "vendor_name": "BuildTime", "path": "/System/FirmwareInfo", "field": "BuildTime"},
        {"name": "自定义SN", "path": "/System/CustomSN"},
        {"name": "主板SN", "path": "/System/BoardSN"},
    ]},
    {"name": "电量", "items": [
        {"name": "电量", "vendor_name": "BatteryLevel", "path": "/BatteryBattery/BatteryLevel", "unit": "%"},
        {"name": "电池电压", "vendor_name": "BatteryVol", "path": "/BatteryBattery/BatteryVol", "unit": "mV"},
        {"name": "电池温度", "vendor_name": "BatteryTemp", "path": "/BatteryBattery/BatteryTemp", "unit": "°C"},
        {"name": "适配器电压", "vendor_name": "AdapterVol", "path": "/BatteryBattery/AdapterVol", "unit": "mV"},
        # ChargerSort 为充电器类别原始值(实测恒 0)，不是「充电状态」；
        # 真正的充电状态见下方 /Mcu/ChargeStatus。
        {"name": "充电器类别", "vendor_name": "ChargerSort", "path": "/BatteryBattery/ChargerSort"},
        # 厂商面板的「充电状态」：来自 MCU 7809 帧（在桩 1 / 离桩 0）
        {"name": "充电状态", "path": "/Mcu/ChargeStatus"},
    ]},
    {"name": "状态", "items": [
        {"name": "工作状态", "vendor_name": "CleanAppStatus", "path": "/CleanTask/CleanAppStatus"},
        {"name": "底盘模式", "vendor_name": "CarrierWorkMode", "path": "/Carrier/CarrierWorkMode"},
    ]},
    {"name": "沿墙红外", "items": [
        # 厂商面板「红外沿墙 → 沿墙距离」由 IrAlongWall 的 ADC 差值数组换算
        # （AutopackDebugger APOSSDKThread.UpdateIrAlongWallAdcDifData：
        #  把数组均分成 3 段 → 沿墙距离 / 近接收灯ADC / 远接收灯ADC）。
        # 这里取第一段的首值作为沿墙距离；机器人未沿墙时整组为 0。
        {"name": "沿墙距离",
         "path": "/IrAlongWall/AdcDifValue[MaxDetectionAdcChannelNum]",
         "unit": "mm", "decode": "ir_wall_distance"},
        # 厂商「其他传感器 → 墙距离」：1009 帧 offset 66（碰撞传感器后一位）
        {"name": "墙距离", "path": "/Mcu/WallDistance", "unit": "mm"},
    ]},
    {"name": "地检", "items": [
        # 地检触发 = 厂商「其他传感器 → 跌落传感器」的同一字节：
        # 1009 帧 offset 57。按用户逐个方向实测的位序：
        #   bit0=前左  bit1=左边  bit2=右边  bit3=前右
        {"name": "左边跌落", "path": "/Mcu/CliffTrigger", "bit": 1},
        {"name": "右边跌落", "path": "/Mcu/CliffTrigger", "bit": 2},
        {"name": "前左跌落", "path": "/Mcu/CliffTrigger", "bit": 0},
        {"name": "前右跌落", "path": "/Mcu/CliffTrigger", "bit": 3},
        {"name": "地检数据", "path": "/Cliff/CliffData"},
    ]},
    {"name": "主动轮", "items": [
        # 与 AutopackDebugger「主动轮」组对齐
        {"name": "左主动轮速度", "path": "/MotorWheelL/Speed", "unit": "mm/s"},
        {"name": "右主动轮速度", "path": "/MotorWheelR/Speed", "unit": "mm/s"},
        {"name": "左主动轮PWM", "path": "/MotorWheelL/PwmFb"},
        {"name": "右主动轮PWM", "path": "/MotorWheelR/PwmFb"},
        {"name": "左主动轮电流", "path": "/MotorWheelL/Curr", "unit": "mA"},
        {"name": "右主动轮电流", "path": "/MotorWheelR/Curr", "unit": "mA"},
        {"name": "线速度", "path": "/CarrierPosCarrierPos/LineVelocity",
         "unit": "mm/s"},
        {"name": "角速度", "path": "/CarrierPosCarrierPos/AngularVelocity",
         "unit": "mrad/s"},
        {"name": "偏航角度", "path": "/Mcu/YawAngle"},
        {"name": "左主动轮里程计", "path": "/Mcu/LeftOdometer", "unit": "m",
         "scale": 0.001},
        {"name": "右主动轮里程计", "path": "/Mcu/RightOdometer", "unit": "m",
         "scale": 0.001},
        {"name": "打滑系数", "path": "/CarrierPosCarrierPos/SkidError"},
    ]},
    {"name": "边刷", "items": [
        {"name": "左边刷状态", "path": "/MotorSideBrushL/WorkState"},
        {"name": "左边刷电流", "path": "/MotorSideBrushL/Curr"},
        {"name": "左边刷PWM", "path": "/MotorSideBrushL/PwmFb"},
        {"name": "左边刷转速", "path": "/MotorSideBrushL/Speed", "unit": "rpm"},
        {"name": "右边刷状态", "path": "/MotorSideBrushR/WorkState"},
        {"name": "右边刷电流", "path": "/MotorSideBrushR/Curr"},
        {"name": "右边刷PWM", "path": "/MotorSideBrushR/PwmFb"},
        {"name": "右边刷转速", "path": "/MotorSideBrushR/Speed", "unit": "rpm"},
    ]},
    {"name": "中刷", "items": [
        {"name": "中刷状态", "path": "/MotorMidBrush/WorkState"},
        {"name": "中刷电流", "path": "/MotorMidBrush/Curr"},
        {"name": "中刷PWM", "path": "/MotorMidBrush/PwmFb"},
        {"name": "中刷转速", "path": "/MotorMidBrush/Speed", "unit": "rpm"},
    ]},
]


#: MCU 反馈（组件挡位/控制量）：来自机器人 7809 调试流，不是 APOS 属性。
#: 与 AutopackDebugger「MCU 反馈」面板逐值对照过（见 core/apos/mcu.py）。
MCU_GROUP = {"name": "挡位（MCU反馈）", "items": [
    {"name": "风机控制量", "path": "/Mcu/FanControl"},
    {"name": "边刷控制量", "path": "/Mcu/SideBrushControl"},
    {"name": "中扫控制量", "path": "/Mcu/MidBrushControl"},
    {"name": "风机值", "path": "/Mcu/FanValue"},
    {"name": "侧刷值", "path": "/Mcu/SideBrushValue"},
    {"name": "中刷值", "path": "/Mcu/MidBrushValue"},
]}


def _vendor_groups():
    seen = {it["path"] for g in CURATED_GROUPS for it in g["items"]}
    groups = []
    for p in VENDOR_PARAMS:
        path = p.get("path") or ""
        # 二进制 ADC 数组不是距离/状态，界面上不展示（用户 2026-11-09）
        if not path or path in seen or path in BINARY_PATHS:
            continue
        seen.add(path)
        gname = GROUP_CN.get(p.get("group"), p.get("group") or "其他")
        # 名称保持 AutopackDebugger 的 DisplayName 原样，避免和厂商界面对不上
        display = p.get("name") or path.rsplit("/", 1)[-1]
        item = {"name": NAME_CN.get(display, display),
                "path": path, "optional": True}
        if path.endswith("BatteryVol") or path.endswith("AdapterVol"):
            item["unit"] = "mV"
        elif path.endswith("BatteryTemp"):
            item["unit"] = "°C"
        elif path.endswith("SystemCur"):
            item["unit"] = "mA"
        groups.append((gname, item))
    out = []
    for gname, item in groups:
        for g in out:
            if g["name"] == gname:
                g["items"].append(item)
                break
        else:
            out.append({"name": gname, "items": [item]})
    return out


PARAM_GROUPS = CURATED_GROUPS + _vendor_groups() + [MCU_GROUP]


CURVE_PAGES = [
    # ★ 曲线图清单(用户 2026-11-06 给定): 风机/水泵/边刷/主刷/电池/地检/碰撞/
    #   超声波/IMU。水泵、地检、碰撞、IMU 的具体曲线等组件参数清单再补。
    {"name": "风机", "groups": [
        {"name": "风机", "curves": [
            {"title": "风机-转速", "path": "/MotorFan/Speed", "unit": "rpm"},
            {"title": "风机-PWM", "path": "/MotorFan/PwmFb"},
            {"title": "风机-电流", "path": "/MotorFan/Curr", "unit": "mA"},
        ]},
        {"name": "挡位", "curves": [
            {"title": "风机控制量", "path": "/Mcu/FanControl"},
        ]},
        {"name": "风机值", "curves": [
            {"title": "风机值", "path": "/Mcu/FanValue"},
        ]},
    ]},
    {"name": "水泵", "groups": [
        {"name": "水泵", "curves": [
            {"title": "水泵-ADC", "path": "/SwitchsPump/CheckAdc"},
        ]},
    ]},
    {"name": "边刷", "groups": [
        # ★ 边刷没有转速采集(实测 /MotorSideBrush*/Speed 恒为 0, 厂商曲线里
        #   也只有电流和控制量), 这里画"控制量"和"PWM 反馈"两个真实量。
        {"name": "电流", "curves": [
            {"title": "左边刷", "path": "/MotorSideBrushL/Curr", "unit": "mA"},
            {"title": "右边刷", "path": "/MotorSideBrushR/Curr", "unit": "mA"},
        ]},
        {"name": "PWM反馈", "curves": [
            {"title": "左边刷", "path": "/MotorSideBrushL/PwmFb"},
            {"title": "右边刷", "path": "/MotorSideBrushR/PwmFb"},
        ]},
        # 机器人暴露的"控制量(PWM/档位)"原始值(PwmOrLevelCtl)
        # 真·组件挡位（MCU 反馈，来自 7809 调试流）
        {"name": "挡位", "curves": [
            {"title": "边刷控制量", "path": "/Mcu/SideBrushControl"},
        ]},
        {"name": "侧刷值", "curves": [
            {"title": "侧刷值", "path": "/Mcu/SideBrushValue"},
        ]},
    ]},
    {"name": "主刷", "groups": [
        # ★ 同边刷: 中刷也没有转速采集(/MotorMidBrush/Speed 恒 0)。
        {"name": "主刷", "curves": [
            {"title": "主刷-电流", "path": "/MotorMidBrush/Curr", "unit": "mA"},
            {"title": "主刷-PWM反馈", "path": "/MotorMidBrush/PwmFb"},
            {"title": "主刷-挡位", "path": "/Mcu/MidBrushControl"},
        ]},
        {"name": "中刷值", "curves": [
            {"title": "中刷值", "path": "/Mcu/MidBrushValue"},
        ]},
    ]},
    {"name": "主动轮", "groups": [
        # 与 AutopackDebugger「主动轮」组对齐：
        # 左/右主动轮速度、线速度/角速度、里程计、PWM、电流、打滑系数
        {"name": "速度", "curves": [
            {"title": "左主动轮速度", "path": "/MotorWheelL/Speed"},
            {"title": "右主动轮速度", "path": "/MotorWheelR/Speed"},
        ]},
        {"name": "线速度/角速度", "curves": [
            {"title": "线速度(mm/s)",
             "path": "/CarrierPosCarrierPos/LineVelocity"},
            {"title": "角速度(mrad/s)",
             "path": "/CarrierPosCarrierPos/AngularVelocity"},
        ]},
        {"name": "里程计/偏航", "curves": [
            # 1008 帧原值就是 mm（与厂商面板逐值核对过），无需再换算
            {"title": "左主动轮里程计(m)", "path": "/Mcu/LeftOdometer",
             "unit": "m", "scale": 0.001},
            {"title": "右主动轮里程计(m)", "path": "/Mcu/RightOdometer",
             "unit": "m", "scale": 0.001},
            {"title": "偏航角度", "path": "/Mcu/YawAngle"},
        ]},
        {"name": "PWM反馈", "curves": [
            {"title": "左主动轮PWM", "path": "/MotorWheelL/PwmFb"},
            {"title": "右主动轮PWM", "path": "/MotorWheelR/PwmFb"},
        ]},
        {"name": "电流", "curves": [
            {"title": "左主动轮电流", "path": "/MotorWheelL/Curr"},
            {"title": "右主动轮电流", "path": "/MotorWheelR/Curr"},
        ]},
        {"name": "打滑系数", "curves": [
            # 厂商「其他传感器」首位的打滑系数（CarrierPos.SkidError）
            {"title": "打滑系数", "path": "/CarrierPosCarrierPos/SkidError"},
        ]},
    ]},
    {"name": "电池", "groups": [
        {"name": "电量", "curves": [
            {"title": "电池-电量", "path": "/BatteryBattery/BatteryLevel",
             "unit": "%"},
        ]},
        {"name": "电压", "curves": [
            {"title": "电池-电压(mV)", "path": "/BatteryBattery/BatteryVol",
             "unit": "mV"},
        ]},
        {"name": "温度", "curves": [
            {"title": "电池-温度(℃)", "path": "/BatteryBattery/BatteryTemp",
             "unit": "°C"},
        ]},
        {"name": "充电状态", "curves": [
            # ★ APOS 没有 ChargeStatus 属性(ChargerSort 实测恒 0)；厂商面板
            #   的「充电状态」来自 MCU 反馈(CurveConfig.xaml: RegGroup_Battery
            #   / ChargeStatus)。2026-11-09 与厂商面板同步对照确认：7809 的
            #   1009 帧 offset 82 在桩=1、离桩清扫=0，与面板完全一致。
            {"title": "电池-充电状态", "path": "/Mcu/ChargeStatus"},
        ]},
    ]},
    {"name": "地检", "groups": [
        {"name": "地检", "curves": [
            {"title": "地检-探头0", "path": "/Cliff/CliffData", "key": "Cliff0"},
            {"title": "地检-探头1", "path": "/Cliff/CliffData", "key": "Cliff1"},
            {"title": "地检-探头2", "path": "/Cliff/CliffData", "key": "Cliff2"},
            {"title": "地检-探头3", "path": "/Cliff/CliffData", "key": "Cliff3"},
            {"title": "地检-探头4", "path": "/Cliff/CliffData", "key": "Cliff4"},
            {"title": "地检-探头5", "path": "/Cliff/CliffData", "key": "Cliff5"},
        ]},
        {"name": "地检触发", "curves": [
            {"title": "左边跌落", "path": "/Mcu/CliffTrigger", "bit": 1,
             "series_id": 0},
            {"title": "右边跌落", "path": "/Mcu/CliffTrigger", "bit": 2,
             "series_id": 1},
            {"title": "前左跌落", "path": "/Mcu/CliffTrigger", "bit": 0,
             "series_id": 2},
            {"title": "前右跌落", "path": "/Mcu/CliffTrigger", "bit": 3,
             "series_id": 3},
        ]},
    ]},
    {"name": "碰撞", "groups": [
        # ★ /IrRecv*/Data 是回充红外信号，不是碰撞（用户 2026-11-06 指出）。
        #   碰撞用按键模块(4 路)，通过属性上传流上报；清扫状态才有值，
        #   待机恒为 0（用户 2026-11-06 说明）。
        #   CLA_HS4 实测只注册了左/右/前左三路，前右要别的机型才上报；
        #   不存在的路径上传流会直接忽略，曲线保持空白，不会报错。
        {"name": "碰撞", "curves": [
            {"title": "左碰撞", "path": "/KeyColliLeft/WorkState"},
            {"title": "右碰撞", "path": "/KeyColliRight/WorkState"},
            {"title": "前左碰撞", "path": "/KeyColliFrontLeft/WorkState"},
            {"title": "前右碰撞", "path": "/KeyColliFrontRight/WorkState"},
        ]},
    ]},
    {"name": "超声波", "groups": [
        {"name": "超声波", "curves": [
            {"title": "超声波-检测结果", "path": "/UltrasonicSensor/DetResult"},
            {"title": "超声波-探头ADC", "path": "/UltrasonicSensor/ProbeDetAdc"},
            {"title": "超声波-工作状态", "path": "/UltrasonicSensor/WorkStateFb"},
        ]},
    ]},
    {"name": "沿墙", "groups": [
        {"name": "沿墙距离", "curves": [
            {"title": "沿墙距离(mm)",
             "path": "/IrAlongWall/AdcDifValue[MaxDetectionAdcChannelNum]",
             "unit": "mm", "decode": "ir_wall_distance"},
        ]},
        {"name": "墙距离", "curves": [
            # 厂商「其他传感器 → 墙距离」，来自 1009 帧 offset 64（实测 255）
            {"title": "墙距离(mm)", "path": "/Mcu/WallDistance", "unit": "mm"},
        ]},
    ]},
    {"name": "IMU", "groups": [
        {"name": "加速度", "curves": [
            {"title": "IMU-加速度X", "path": "/ImuImu/AccelX"},
            {"title": "IMU-加速度Y", "path": "/ImuImu/AccelY"},
            {"title": "IMU-加速度Z", "path": "/ImuImu/AccelZ"},
        ]},
        {"name": "角速度", "curves": [
            {"title": "IMU-角速度X", "path": "/ImuImu/GyroX"},
            {"title": "IMU-角速度Y", "path": "/ImuImu/GyroY"},
            {"title": "IMU-角速度Z", "path": "/ImuImu/GyroZ"},
        ]},
    ]},
]


def item_label(item):
    """显示名（中文；厂商 DisplayName 只用于内部映射）。"""
    return item.get("name", "")


def is_mcu_path(path):
    """MCU 反馈走 7809 调试流，不是 APOS 属性，不能参与属性读/上传订阅。"""
    return str(path).startswith("/Mcu/")


def all_param_paths():
    out = []
    for group in PARAM_GROUPS:
        for item in group["items"]:
            if is_mcu_path(item["path"]):
                continue
            if item["path"] not in out:
                out.append(item["path"])
    return out


def page_curve_paths(page):
    out = []
    for group in page.get("groups", []):
        for curve in group.get("curves", []):
            if is_mcu_path(curve["path"]):
                continue
            if curve["path"] not in out:
                out.append(curve["path"])
    return out


def all_curve_paths():
    out = []
    for page in CURVE_PAGES:
        for p in page_curve_paths(page):
            if p not in out:
                out.append(p)
    return out


def all_monitor_paths():
    """表格页也顺带采集曲线路径 —— 连上就开始积累曲线历史。

    曲线页只按当前页高频采集(P0 是 5Hz)；表格页用较慢间隔读全量，
    这样用户中途切到曲线页时，之前的历史不会是一段空白。
    """
    paths = all_param_paths()
    for p in all_curve_paths():
        if p not in paths:
            paths.append(p)
    return paths


def display_value(item, snapshot):
    """把一条参数值转成表格显示文本（支持 field / JSON / 状态翻译）。"""
    ok = snapshot.get("ok", {}) if snapshot else {}
    raw = ok.get(item["path"])
    if raw is None:
        return ""
    raw = decode_value(item, raw)
    if raw is None:
        return ""
    if item.get("bit") is not None:
        try:
            return str((int(raw) >> int(item["bit"])) & 1)
        except (TypeError, ValueError):
            return ""
    if (item.get("decode") or item.get("scale") is not None) \
            and isinstance(raw, (int, float)):
        return f"{raw:g}"
    if isinstance(raw, dict):
        vals = {k: v for k, v in raw.items() if k != "AposTimeStamp"}
        if len(vals) == 1:
            raw = next(iter(vals.values()))
    if isinstance(raw, str) and item["path"] in BINARY_PATHS:
        return _format_binary(raw)
    field = item.get("field")
    if field == "summary":
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(data, dict):
                data = {k: v for k, v in data.items() if k != "AposTimeStamp"}
                parts = [f"{k}={v}" for k, v in list(data.items())[:2]]
                if len(data) > 2:
                    parts.append(f"…（共 {len(data)} 项）")
                return "，".join(parts)
        except Exception:
            pass
        return translate_value(item["path"], raw)
    if field:
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(data, dict) and field in data:
                return str(data[field])
        except Exception:
            pass
        return ""
    return translate_value(item["path"], raw)
