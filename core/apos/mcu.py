# -*- coding: utf-8 -*-
"""MCU 反馈（组件挡位/控制量）：机器人 7809 调试流解码。

* 通道：ZeroMQ SUB，`tcp://<ip>:7809`（AutopackDebugger 同款调试流）；
* 帧格式：`[u16 类型][u16 子类型][8B 保留][u64 时间戳][u64 序号][负载]`；
* 已确认的字段（与 AutopackDebugger「MCU 反馈」面板逐值对照过）：

    类型 1111 / 长度 82 —— 控制量（挡位）
        off 55  风机控制量   (u8, 0~255)
        off 64  边刷控制量   (u8, 实测 160~196)
        off 65  中扫控制量   (u8, 实测 2)

    类型 1009 / 长度 134 —— 反馈值
        off 36/38/40/42/44/46/48  超声波通道0~6 (u16)
        off 57  地检触发     (u8 位图，ch0+ch3=9；厂商「跌落传感器」)
        off 65  碰撞传感器   (u8, 实测 0)
        off 66  墙距离       (u8, 实测 255；在碰撞传感器后一位)
        off 82  充电状态     (u8, 在桩 1 / 离桩 0；与厂商面板一致)
        off 91  风机值
        off 92  中刷值
        off 93  侧刷值

    类型 1008 / 长度 94 —— 底盘位姿/其他传感器
        off 40  偏航角度     (u32, 实测 553571)
        off 44  左主动轮里程计 (u32, 单位 mm, 实测 1147149)
        off 48  右主动轮里程计 (u32, 单位 mm, 实测 1265990)
        off 78  光流x轴偏移量 (i16, 实测 -8649)

这些值不在 APOS 属性接口里，只能从这条调试流拿。LED/传感器开关/蜂鸣等
32 位位图字段暂未解码，先不采集。
"""
import struct
import threading
import time

from core.logger import get_logger

log = get_logger()

#: 属性路径 → 显示名（与厂商面板一致）
FIELDS = {
    "/Mcu/FanControl": "风机控制量",
    "/Mcu/SideBrushControl": "边刷控制量",
    "/Mcu/MidBrushControl": "中扫控制量",
    "/Mcu/FanValue": "风机值",
    "/Mcu/SideBrushValue": "侧刷值",
    "/Mcu/MidBrushValue": "中刷值",
    "/Mcu/ChargeStatus": "充电状态",
    "/Mcu/CliffTrigger": "地检触发",
    "/Mcu/WallDistance": "墙距离",
    "/Mcu/OpticalFlowX": "光流x轴偏移量",
    "/Mcu/YawAngle": "偏航角度",
    "/Mcu/LeftOdometer": "左主动轮里程计",
    "/Mcu/RightOdometer": "右主动轮里程计",
    "/Mcu/Ultrasonic0": "超声波通道0",
    "/Mcu/Ultrasonic1": "超声波通道1",
    "/Mcu/Ultrasonic2": "超声波通道2",
    "/Mcu/Ultrasonic3": "超声波通道3",
    "/Mcu/Ultrasonic4": "超声波通道4",
    "/Mcu/Ultrasonic5": "超声波通道5",
    "/Mcu/Ultrasonic6": "超声波通道6",
}

#: 调试流端口（AutopackDebugger 同款）
PORT = 7809


class McuStream:
    """订阅 7809 调试流并解析 MCU 反馈。线程安全。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._values = {}          # path -> (value, ts)
        self._last_event = 0.0
        self._stop = threading.Event()
        self._sub = None
        self._thread = None
        self._ip = ""

    # ── 生命周期 ──
    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, ip):
        self.stop()
        try:
            import zmq
        except Exception as e:      # 没装 pyzmq 时静默降级
            log.info(f"[MCU] 不可用(缺少 pyzmq): {e}")
            return False
        self._ip = str(ip)
        try:
            ctx = zmq.Context.instance()
            sub = ctx.socket(zmq.SUB)
            sub.setsockopt(zmq.SUBSCRIBE, b"")
            sub.setsockopt(zmq.RCVTIMEO, 500)
            sub.connect(f"tcp://{self._ip}:{PORT}")
        except Exception as e:
            log.info(f"[MCU] 连接调试流失败: {e}")
            return False
        self._sub = sub
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="mcu-stream")
        self._thread.start()
        log.info(f"[MCU] 已订阅调试流: {self._ip}:{PORT}")
        return True

    def stop(self):
        self._stop.set()
        t = self._thread
        self._thread = None
        if t and t.is_alive():
            t.join(1.0)
        sub, self._sub = self._sub, None
        if sub is not None:
            try:
                sub.close(0)
            except Exception:
                pass
        with self._lock:
            self._values.clear()
            self._last_event = 0.0

    # ── 数据 ──
    def values(self):
        with self._lock:
            return {p: v for p, (v, _ts) in self._values.items()}

    def values_with_ts(self):
        """path -> (value, ts)。调试流停了以后值不会变，上层要靠 ts 判断过期。"""
        with self._lock:
            return dict(self._values)

    def last_event(self):
        with self._lock:
            return self._last_event

    # ── 内部 ──
    def _loop(self):
        while not self._stop.is_set():
            sub = self._sub
            if sub is None:
                return
            try:
                frame = sub.recv()
            except Exception:
                continue
            try:
                self._decode(frame)
            except Exception:
                pass

    def _decode(self, frame):
        """解析一帧；只处理已确认的两种帧，其余忽略。"""
        if not frame or len(frame) < 4:
            return
        typ, subtype = struct.unpack_from("<HH", frame, 0)
        if subtype != 2001:          # 只有 0x07D1 子类型的帧是这两类数据帧
            return
        values = None
        if typ == 1111 and len(frame) == 82:
            values = {
                "/Mcu/FanControl": frame[55],
                "/Mcu/SideBrushControl": frame[64],
                "/Mcu/MidBrushControl": frame[65],
            }
        elif typ == 1009 and len(frame) == 134:
            u16 = lambda off: struct.unpack_from("<H", frame, off)[0]
            values = {
                "/Mcu/Ultrasonic0": u16(36),
                "/Mcu/Ultrasonic1": u16(38),
                "/Mcu/Ultrasonic2": u16(40),
                "/Mcu/Ultrasonic3": u16(42),
                "/Mcu/Ultrasonic4": u16(44),
                "/Mcu/Ultrasonic5": u16(46),
                "/Mcu/Ultrasonic6": u16(48),
                "/Mcu/CliffTrigger": frame[57],
                "/Mcu/WallDistance": frame[66],
                "/Mcu/ChargeStatus": frame[82],
                "/Mcu/FanValue": frame[91],
                "/Mcu/SideBrushValue": frame[93],
                "/Mcu/MidBrushValue": frame[92],
            }
        elif typ == 1008 and len(frame) == 94:
            u32 = lambda off: struct.unpack_from("<I", frame, off)[0]
            values = {
                "/Mcu/YawAngle": u32(40),
                "/Mcu/LeftOdometer": u32(44),
                "/Mcu/RightOdometer": u32(48),
                "/Mcu/OpticalFlowX": struct.unpack_from("<h", frame, 78)[0],
            }
        if not values:
            return
        now = time.time()
        with self._lock:
            for path, value in values.items():
                self._values[path] = (value, now)
            self._last_event = now
