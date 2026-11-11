# -*- coding: utf-8 -*-
"""MCU 反馈（组件挡位/控制量）：机器人 7809 调试流解码。

* 通道：ZeroMQ SUB，`tcp://<ip>:7809`（AutopackDebugger 同款调试流）；
  机器人不是一直推流 —— 连接后必须向 `tcp://<ip>:7813` 发控制帧（心跳，
  `[u16 0xF1][u16 0][u32 0]`）唤醒，并每 3 秒续一帧保活（2026-11-10 实测）；
* 帧格式：`[u16 类型][u16 子类型][8B 头部][u64 时间戳][负载]`，
  文本日志负载从第 20 字节开始（2026-11-11 原始帧确认）；数据帧按下面
  的字节偏移解析；
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
import collections
import re
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

#: 控制端口：7809 的发布器不是常开的，要先往这里发控制帧（厂商工具连上后
#: 持续发心跳）机器人才开始推流。实测只订阅 7809 一台都收不到。
CTRL_PORT = 7813

#: 心跳间隔（秒）：厂商工具持续发，停太久机器人会再次停推
HEARTBEAT_SECONDS = 3.0

#: 设备日志行的开头："11-11 7:14:37.944/SW D/..."，用来识别一帧开头是不是残行
_LOG_LINE_START = re.compile(r"^\d{1,2}-\d{1,2}\s+\d{1,2}:\d{1,2}:\d{1,2}")

#: 厂商控制码 Cmd_ClearStatus（清除当前状态/故障提示）
CMD_CLEAR_STATUS = 0x0BC9


def control_frame(func=0xF1, start=0, payload=b""):
    """7813 控制帧：`[u16 功能码][u16 起始地址][u32 长度][负载]`。

    与厂商 SDK 的 SdkHeader 内存布局一致（short/ushort/uint，共 8 字节）。
    空负载就是心跳；发一帧心跳即可唤醒 7809 推流（实测）。
    """
    return struct.pack("<HHI", int(func), int(start), len(payload)) + payload


class McuStream:
    """订阅 7809 调试流并解析 MCU 反馈。线程安全。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._values = {}          # path -> (value, ts)
        self._last_event = 0.0
        self._stop = threading.Event()
        self._sub = None
        self._ctrl = None          # PUSH -> 7813：唤醒/保活调试流
        self._hb_at = 0.0
        #: 设备日志行(类型 1001 文本帧): [(ts, line)]，故障模拟验证语音/灯显用
        self._log_lines = collections.deque(maxlen=4000)
        # 文本帧是按字节流切块的, 一帧往往从一行中间开始; 这里拼回完整行,
        # 否则界面上会出现 ":5:11.103/SW D/..." 这种缺了开头时间的日志
        self._log_pending = ""
        self._log_synced = False
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
            # ★ 关键：只订阅 7809 收不到任何帧 —— 机器人要收到 7813 的控制帧
            #   才开始推流（厂商工具连上后一直在发心跳）。先发一帧，之后在
            #   接收循环里每 HEARTBEAT_SECONDS 秒补一帧保活。
            ctrl = ctx.socket(zmq.PUSH)
            ctrl.setsockopt(zmq.LINGER, 0)
            ctrl.connect(f"tcp://{self._ip}:{CTRL_PORT}")
        except Exception as e:
            log.info(f"[MCU] 连接调试流失败: {e}")
            return False
        self._sub = sub
        self._ctrl = ctrl
        self._stop.clear()
        self._send_heartbeat()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="mcu-stream")
        self._thread.start()
        log.info(f"[MCU] 已订阅调试流: {self._ip}:{PORT}"
                 f"（已发送唤醒帧 {self._ip}:{CTRL_PORT}）")
        return True

    def send_control_code(self, code):
        """发一条厂商控制码(7813): 如 CMD_CLEAR_STATUS 清除状态。"""
        ctrl = self._ctrl
        if ctrl is None:
            return False
        try:
            ctrl.send(control_frame(payload=struct.pack("<ii", 2, int(code))))
            return True
        except Exception as e:
            log.debug(f"[MCU] 控制码发送失败 {code}: {e}")
            return False

    def _send_heartbeat(self):
        ctrl = self._ctrl
        if ctrl is None:
            return
        try:
            ctrl.send(control_frame())
            self._hb_at = time.time()
        except Exception as e:
            log.debug(f"[MCU] 调试流心跳失败: {e}")

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
        ctrl, self._ctrl = self._ctrl, None
        if ctrl is not None:
            try:
                ctrl.close(0)
            except Exception:
                pass
        with self._lock:
            self._values.clear()
            self._last_event = 0.0
            self._log_lines.clear()
            self._log_pending = ""
            self._log_synced = False

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

    def log_since(self, ts):
        """返回 ts 之后的设备日志行 [(时间, 文本)]（故障模拟验证用）。"""
        with self._lock:
            return [(t, line) for t, line in self._log_lines if t >= ts]

    # ── 内部 ──
    def _loop(self):
        while not self._stop.is_set():
            if time.time() - self._hb_at >= HEARTBEAT_SECONDS:
                self._send_heartbeat()          # 保活：否则机器人会停推
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
        # 类型 1001 / 子类型 0 是机器人的文本日志(语音/灯显/故障处理都在里面)。
        # ★ 文本从第 20 字节开始（实测原始帧：4B 类型/子类型 + 8B 头部字段 +
        #   8B u64 时间戳）；以前按 28 取会把每行开头 "11-11 16" 这 8 个字符切掉。
        if typ == 1001 and subtype == 0 and len(frame) > 20:
            try:
                text = frame[20:].decode("utf-8", "replace")
            except Exception:
                text = ""
            now = time.time()
            with self._lock:
                data = self._log_pending + text
                parts = data.split("\n")
                # 最后一段没有换行, 可能还有后续字节, 留到下一帧
                self._log_pending = parts.pop()
                if len(self._log_pending) > 4096:
                    self._log_pending = self._log_pending[-4096:]
                if not self._log_synced and parts:
                    # 第一帧通常从某一行中间开始; 只有缺了行首时间戳的残行才丢
                    if not _LOG_LINE_START.match(parts[0]):
                        parts.pop(0)
                    self._log_synced = True
                for line in parts:
                    line = line.strip()
                    if line:
                        self._log_lines.append((now, line))
            return
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
