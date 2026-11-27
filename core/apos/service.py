# -*- coding: utf-8 -*-
"""后台参数采集：一个线程、一个 SDK 句柄、固定间隔批量 read。

纯 Python（不 import PySide6）—— GUI 用 QTimer 读 `latest()` 快照，core 可直接单测。
"""
import json
import threading
import time

from core.apos.catalog import STATIC_PATHS
from core.apos.client import (BASE_STATION_EVENT, BASE_STATION_FILES,
                              BINARY_PATHS, COMPOSITE_PATHS, AposClient)
from core.logger import get_logger

log = get_logger()

#: 属性上传流不会推送、只能读的属性（系统信息是复合 JSON）。
#: ★ 数值类属性必须走推送：binpack 读对数值属性返回的是 0（缺类型信息），
#:   拿它当数据源会让整页参数显示成 0。
READ_ONLY_PATHS = ("/System/CustomSN", "/System/BoardSN",
                   "/System/FirmwareInfo")

#: 这些机型的 CustomSN/BoardSN 在 binpack 上读不到(还会触发二分重试拖慢连接),
#: 直接从机器人文件读。实测 CLA_ZY2L; 新机型确认后往这里加。
NO_SN_MODELS = frozenset({"CLA_ZY2L"})

#: 属性路径 → 机器人上的 SN 文件（属性读不到时兜底；厂商源码同样读这些文件）
SN_FILES = {
    "/System/CustomSN": ("/oem/bin/sys_data/custom.sn",),
    "/System/BoardSN": ("/oem/bin/sys_data/sn",),
}

#: 默认参数（电量监测也依赖它）
DEFAULT_PATHS = (
    "/BatteryBattery/BatteryLevel",
    "/BatteryBattery/BatteryVol",
    "/BatteryBattery/BatteryTemp",
    "/BatteryBattery/ChargerSort",
    "/MotorFan/Speed",
    "/CleanTask/CleanAppStatus",
)


class ParamService:
    #: 本机(10.21)已确认存在的电机属性组；驱动轮不在其中——轮子走
    #: `/Carrier` 运动接口。模块名取自机器人 mod_reg.json 的 motor__*。
    MOTOR_GROUPS = {
        "mid_brush": "/MotorMidBrush",
        "fan": "/MotorFan",
        "side_brush_r": "/MotorSideBrushR",
        "side_brush_l": "/MotorSideBrushL",
        "cleaning_drum": "/MotorCleaningDrum",
        "drum_telescopic": "/MotorDrumTelescopic",
        "arm": "/MotorArm",
        "wheel_lift_l": "/MotorWheelLiftL",
        "wheel_lift_r": "/MotorWheelLiftR",
    }

    def __init__(self, client, paths=None, interval=2.0, stream=None,
                 stream_paths=None, mcu=None):
        self.client = client
        self._paths = list(paths or DEFAULT_PATHS)
        self.interval = max(0.05, float(interval or 2.0))
        #: 属性上传流（AutopackDebugger 同款推送通道）。可用时数据以推送为准，
        #: 轮询降频做兜底 —— 机器人侧不会再有批量读失败的噪音。
        self.stream = stream
        self._stream_paths = list(stream_paths or [])
        #: MCU 反馈（组件挡位）：机器人 7809 调试流，APOS 属性里没有这些字段。
        self.mcu = mcu
        self._lock = threading.Lock()
        self._snapshot = {"ok": {}, "failed": {}, "ts": 0.0}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._reading = threading.Event()     # 本轮读是否在跑（开流前要等它结束）
        #: 连接流程(读型号/SN/固件/基站版本/开流)做完之前不轮询 —— 否则第一轮
        #: librpc 读会和开流/读文件撞在一起，读空后把界面刷成空。
        self._ready = threading.Event()
        self._thread = None
        self._ip = ""
        self._connected = False
        #: 上传流优先: 连接成功时没有 librpc 会话, 数据/系统信息都走 libapos
        #  句柄; 上传流不可用时才会退回 librpc 轮询。
        self._stream_only = False
        #: 当前机型(从 FirmwareInfo 解析), 用于判断 SN 是否能在流句柄上读
        self._model = ""
        #: 推送反复中断时的兜底状态: 连续重启仍无数据 → 改用 librpc 轮询
        self._stream_stalls = 0
        self._stream_fallback = False
        self._stream_retry_at = 0.0
        #: 外部（续航测试）独占推流时置位: 不再建流/读属性，让出机器人
        #  推送会话, 避免同一台机器人上两条推送会话互相挤掉（用户 2026-11-26）
        self._stream_paused = False
        #: 固定参数(SN/固件/型号等): 读到一次后缓存, 之后不再参与轮询/推送覆盖,
        #: 重新连接时清空（用户 2026-11-27）。
        self._static_values = {}
        self._static_done = set()
        #: 最后一次成功从设备读到数据的时间 —— 长时间没有数据判定为断连
        self._last_live = time.time()

    # ── 生命周期 ──
    def connect(self, ip):
        # ★ 换设备/重连前先停掉旧采集线程和旧推流：librpc_base 连接(prime 读)
        #   与上一台的 libapos 上传流同时存在会互卡，表现为连接慢、SN/固件读空。
        self._ready.clear()
        # 重连/换设备后旧的兜底状态必须清零, 否则新开好的推流会被当成
        # “已退轮询”而被 _read 绕过(用户 2026-11-17 代码审查)
        self._stream_stalls = 0
        self._stream_fallback = False
        self._stream_retry_at = 0.0
        self.stop()
        if self.stream is not None:
            try:
                self.stream.stop()
            except Exception:
                pass
        if self.mcu is not None:
            try:
                self.mcu.stop()
            except Exception:
                pass
        # ★ libapos 优先: 它既是数据通道(AposSdkInit ~0.3s), 也能读 SN/固件/
        #   基站文件; 旧 librpc 的 CppAposSdkInit 对新机型(CLA_ZY2L)还会失败,
        #   先去连一遍只会白等。所以先开上传流, 成功就不再建 librpc 会话。
        self._ip = str(ip)
        stream_ok = False
        if self.stream is not None:
            for _ in range(2):         # 机器人忙时偶发一次失败, 再试一次
                try:
                    stream_ok = bool(self.stream.start(
                        self._ip, list(self._stream_paths or self._paths)))
                except Exception:
                    stream_ok = False
                if stream_ok:
                    break
                time.sleep(0.3)
        # ★ 上传流不可用(BinpackCLR 缺失/固件拒绝订阅)时退回 librpc 轮询,
        #   否则这些机器会"连上了但一直没有数据"。
        ok = bool(self.client.connect(self._ip, retries=1)) if not stream_ok else True
        self._stream_only = bool(ok and stream_ok)
        if stream_ok:
            log.info(f"[参数] {self._ip} 使用 libapos 上传流连接")
        elif ok:
            log.info(f"[参数] {self._ip} 上传流不可用, 退回 librpc 轮询")
        self._connected = ok
        if ok:
            # ★ 换设备后旧快照/固定参数缓存不能带过去
            self._static_values = {}
            self._static_done = set()
            self._last_live = time.time()
            with self._lock:
                self._snapshot = {"ok": {}, "failed": {}, "ts": 0}
            self._start_mcu()
            # 先用一次快速读把型号/固件/SN 放进快照 —— 界面不用等轮询周期
            # (这三项是复合属性, 单读只要几十毫秒)。
            self._prime_system()
            # ★ 立刻开推送流: 开流后所有属性读都走同一个 libapos_client 句柄
            #   (`stream.read`)，不再碰会互卡的 librpc_base。
            if not self._stream_only:
                self.start_stream()
            self.start()
            self._ready.set()
        return ok

    def read_base_station(self, retries=2, delay=0.6):
        """读基站版本（机器人上的文件）。

        ★ 推流开着的时候不能走 librpc_base 的文件读 —— 实测只要开过上传流，
          同一个连接的 CppAposSdkReadFile 就一直返回空（停流也恢复不了）。
          所以在推流句柄上用 binpack 读 `/System/FileStrOp`（厂商工具的做法），
          流也不用停，读基站版本不会再打断曲线/参数。
        """
        stream = self.stream
        if stream is not None and getattr(stream, "running", False):
            version = self._stream_base_version()
            if version:
                return version
            try:
                stream.send_event(BASE_STATION_EVENT)
            except Exception:
                pass
            for _ in range(max(1, int(retries or 1))):
                time.sleep(delay)
                version = self._stream_base_version()
                if version:
                    return version
            return ""
        try:
            return self.client.read_base_station_version(retries=retries,
                                                         delay=delay)
        except Exception:
            return ""

    def post_event(self, topic, event):
        """发任意机器人事件(配置事件手动验证用)。返回是否发送成功。"""
        stream = self.stream
        if (stream is None or not str(topic or "").strip()
                or not str(event or "").strip()):
            return False
        if not getattr(stream, "running", False):
            log.info(f"[故障模拟] 上传流未运行, 暂不能发送 {event}"
                     "(看门狗会自动重连)")
            return False
        try:
            rc = stream.post_event(topic, event)
            # ★ 厂商 SDK 的返回值不是可靠的成败标志: 实测有的设备返回负数,
            #   但事件已经送达并播报(用户 2026-11-13)。只要调用没抛异常就
            #   继续按设备日志判定, 不要在这里误报"发送失败"。
            if isinstance(rc, int) and rc < 0:
                log.info(f"[故障模拟] {event} SDK 返回 {rc}，继续等待设备日志")
            return True
        except Exception as e:
            log.info(f"[故障模拟] 发送 {event} 异常: {e}")
            return False

    def simulate_fault(self, key):
        """注入一条故障模拟事件(`/Common/SetCmd` PostEvent)。返回是否发送成功。"""
        from core.apos import faults
        item = faults.get(key)
        stream = self.stream
        if (item is None or stream is None
                or not getattr(stream, "running", False)):
            return False
        try:
            rc = stream.post_event(item["topic"], item["event"])
            if isinstance(rc, int) and rc < 0:
                log.info(f"[故障模拟] {key} SDK 返回 {rc}，继续等待设备日志")
            return True
        except Exception as e:
            log.info(f"[故障模拟] 发送 {key} 异常: {e}")
            return False

    def fault_log_since(self, ts):
        """取 ts 之后的设备日志行(故障模拟验证语音/灯显)。"""
        mcu = self.mcu
        if mcu is None or not hasattr(mcu, "log_since"):
            return []
        try:
            return mcu.log_since(ts)
        except Exception:
            return []

    def clear_faults(self):
        """清除故障/状态提示（厂商 Cmd_ClearStatus 控制码）。"""
        mcu = self.mcu
        if mcu is None or not hasattr(mcu, "send_control_code"):
            return False
        try:
            from core.apos.mcu import CMD_CLEAR_STATUS
            return bool(mcu.send_control_code(CMD_CLEAR_STATUS))
        except Exception:
            return False

    def stop_tasks(self):
        """批量结束回到空闲: 停止清扫 + 停止找桩(已真机验证事件生效)。

        ★ 优先用推送流句柄: ZY2L 等机型走 libapos 连接时没有 librpc 会话,
          用 AposClient 发任务命令会一直 -1(用户 2026-11-17)。
        """
        paths = ("/TaskManager/StopClean",
                 "/GoBackStationTask/StopFindCharger")
        stream = self.stream
        out = []
        if stream is not None and getattr(stream, "running", False):
            for path in paths:
                try:
                    out.append((path, int(stream.send_event(path))))
                except Exception as e:
                    out.append((path, f"ERR {e}"))
            return out
        client = self.client
        if client is None or not getattr(client, "connected", False):
            return []
        for path in paths:
            try:
                out.append((path, int(client.send_event(path))))
            except Exception as e:
                out.append((path, f"ERR {e}"))
        return out

    # ── 清扫控制（自动化/续航测试用，10.21 真机验证） ──
    def _stream_ready(self):
        stream = self.stream
        return stream is not None and getattr(stream, "running", False)

    def set_clean_mode(self, mode):
        """设置清扫模式：sweepOnly / sweepMop / mopOnly。

        机器人日志确认：`/Task/SetCleanMode` → SetCleanType:
        CLEAN_TYPE_SWEEP / CLEAN_TYPE_SWEEP_AND_MOP / CLEAN_TYPE_MOP。
        """
        if not self._stream_ready():
            return False
        try:
            return self.stream.send_binpack_event(
                "/Task/SetCleanMode", {"CleanMode": str(mode)}) >= 0
        except Exception as e:
            log.info(f"[控制] 设置清扫模式失败: {e}")
            return False

    def set_fan_level(self, level):
        """设置吸力挡位 1~5（APP 用户挡位，转场后保持）。

        ★ 事件载荷字段是 `data`（厂商网络层 property.cpp 就是这样下发的）；
          写成 `FanLevelUserSet` 会被处理器判为缺字段（日志 level:-1）。
          处理器成功后写入 ParamManager 的 FanLevelUserSet，转场按它恢复。
        ★ 再补一条 `/event/config_file_change` 写用户设置（与水量同款，
          2026-11-26）：只发 change_fan_level 时机器人侧能改当前工作挡位，
          但 APP/云端显示的用户挡位由 network 的 FanLevelServer 维护，
          不一定同步；补写属性是最稳妥的尝试（不支持的固件会忽略）。
        """
        if not self._stream_ready():
            return False
        level = int(level)
        try:
            rc1 = self.stream.post_event(
                "/event/component_ctrl", "change_fan_level",
                {"data": level})
            rc2 = self.stream.post_event(
                "/event/config_file_change", "config_file_attribute_change",
                {"FanLevelUserSet": level})
            return rc1 >= 0 and rc2 >= 0
        except Exception as e:
            log.info(f"[控制] 设置吸力挡位失败: {e}")
            return False

    def set_water_level(self, level):
        """设置水量挡位 0~3（0 = 关，1/2/3 = 低/中/高）。

        APP 改水量实际是两步（2026-11-25 抓包确认）:
          1. `/event/component_ctrl` `change_water_pump` `{data:N}`
             —— 设置当前工作挡位（设备日志 `water_pump level set
             successfully! level:N water_level_count:4`）；
          2. `/event/config_file_change` `config_file_attribute_change`
             `{WaterLevelUserSet:N}` —— 写用户设置，APP/云端显示的是它。
        只发第 1 步会改运行挡位，但 APP 里仍是旧值（用户反馈）。
        """
        if not self._stream_ready():
            return False
        level = int(level)
        try:
            rc1 = self.stream.post_event(
                "/event/component_ctrl", "change_water_pump",
                {"data": level})
            rc2 = self.stream.post_event(
                "/event/config_file_change", "config_file_attribute_change",
                {"WaterLevelUserSet": level})
            return rc1 >= 0 and rc2 >= 0
        except Exception as e:
            log.info(f"[控制] 设置水量挡位失败: {e}")
            return False

    def continue_clean(self):
        """继续当前清扫任务（报错恢复用，不要重开新任务）。

        机器人上报的是 `/event/task_manager` 的 `app_sweeper_resume_clean_task`
        （base_station 节点恢复清扫用的同一条事件）。
        """
        if not self._stream_ready():
            return False
        try:
            return self.stream.post_event(
                "/event/task_manager", "app_sweeper_resume_clean_task") >= 0
        except Exception as e:
            log.info(f"[控制] 继续清扫失败: {e}")
            return False

    def start_total_clean(self):
        """从任意状态启动总清扫（含充电桩/基站任务）。

        机器人启动清扫节点前必须先退出当前的充电/基站/回充节点，否则事件
        会被忽略（实测：回充途中直接 start 会被 collect_dust/charging 抢走，
        隔几分钟才生效）。这里 kill 后等状态离开这些节点再发 start。
        """
        if not self._stream_ready():
            return False
        try:
            self.stream.post_event("/event/task_manager", "kill_all_nodes")
            end = time.time() + 8
            blocked = ("work_status_back_charge", "work_status_go_charge",
                       "work_status_charging", "work_status_full_charging",
                       "work_status_base_station", "work_status_collect_dust",
                       "work_status_dry_mop", "work_status_wash_mop")
            while time.time() < end:
                v = self.stream.values().get("/CleanTask/CleanAppStatus")
                status = v.get("CleanAppStatus") if isinstance(v, dict) \
                    else str(v or "")
                if status not in blocked:
                    break
                time.sleep(0.5)
            self.stream.post_event("/event/task_manager",
                                   "app_sweeper_start_total_clean")
            return True
        except Exception as e:
            log.info(f"[控制] 启动清扫失败: {e}")
            return False

    def start_find_charger(self):
        """开始找桩/回充。"""
        if not self._stream_ready():
            return False
        try:
            return self.stream.post_event("/event/task_manager",
                                          "start_find_charger") >= 0
        except Exception as e:
            log.info(f"[控制] 回充失败: {e}")
            return False

    def stop_clean(self):
        """停止清扫（不回充）。"""
        if not self._stream_ready():
            return False
        try:
            return self.stream.post_event("/event/task_manager",
                                          "stop_clean") >= 0
        except Exception as e:
            log.info(f"[控制] 停止清扫失败: {e}")
            return False

    # ── 调试控制（底盘/任务模式、滚筒伸缩电机） ──
    def set_task_manager_mode(self, mode):
        """任务模式：`auto_mode` / `manual_mode`。

        厂商 Event.AposTask.SetTaskManagerMode → `/Task/SetTaskManagerMode`
        `{"Mode": mode}`。
        """
        if not self._stream_ready():
            return False
        try:
            return self.stream.send_binpack_event(
                "/Task/SetTaskManagerMode", {"Mode": str(mode)}) >= 0
        except Exception as e:
            log.info(f"[控制] 设置任务模式失败: {e}")
            return False

    def set_carrier_work_mode(self, mode):
        """底盘工作模式：`CARRIER_WORK_NORMAL` / `CARRIER_WORK_IDLE`。

        厂商 Event.Carrier.SetCarrierWorkMode → `/Carrier/SetCarrierWorkMode`
        `{"Mode": mode}`。调试电机前必须切成 NORMAL，否则写入成功但
        驱动不响应（真机实测，2026-11-25）。
        """
        if not self._stream_ready():
            return False
        try:
            return self.stream.send_binpack_event(
                "/Carrier/SetCarrierWorkMode", {"Mode": str(mode)}) >= 0
        except Exception as e:
            log.info(f"[控制] 设置底盘模式失败: {e}")
            return False

    def set_motor_pwm(self, module, pwm, work_mode=1):
        """电机直控：写 `WorkModeCtl`(uint8) + `PwmOrLevelCtl`(int16)。

        module 可用短名（见 `MOTOR_GROUPS`）或完整属性组路径，如
        `"drum_telescopic"` / `"/MotorDrumTelescopic"`。正负方向由电机
        决定；驱动轮不走这里（按 `/Carrier` 运动接口控制）。
        前置条件（真机实测）：底盘 `CARRIER_WORK_NORMAL`、任务模式
        `manual_mode`，否则写入成功但电机不响应。
        """
        group = self.MOTOR_GROUPS.get(str(module), str(module))
        if not group.startswith("/Motor"):
            return False
        if not self._stream_ready():
            return False
        try:
            return self.stream.write_props([
                (group + "/WorkModeCtl", int(work_mode), "uint8"),
                (group + "/PwmOrLevelCtl", int(pwm), "int16"),
            ]) >= 0
        except Exception as e:
            log.info(f"[控制] 电机 {module} 直控失败: {e}")
            return False

    def motor_state(self, module):
        """读电机当前值（PwmFb/Curr 非 0 表示真的在动）。"""
        group = self.MOTOR_GROUPS.get(str(module), str(module))
        stream = self.stream
        if stream is None or not group.startswith("/Motor"):
            return {}
        try:
            vals = stream.values()
        except Exception:
            return {}
        out = {}
        for key in ("WorkModeCtl", "PwmOrLevelCtl", "PwmFb", "Curr",
                    "WorkState", "Speed"):
            item = vals.get(group + "/" + key)
            out[key] = item.get(key) if isinstance(item, dict) else item
        return out

    def set_drum_telescopic(self, pwm):
        """控制清洁滚筒伸缩电机：正 PWM 伸出，负 PWM 收回，0 停止。

        与 AutopackDebugger 的 `MotorDrumTelescopic_SetPWM` 同款。
        """
        return self.set_motor_pwm("drum_telescopic", pwm)

    def drum_telescopic_state(self):
        """读滚筒伸缩电机当前值（PwmFb/Curr 非 0 表示真的在动）。"""
        return self.motor_state("drum_telescopic")

    def _stream_base_version(self):
        """在推流句柄上读两个候选文件，返回解析出的版本号。"""
        stream = self.stream
        if stream is None or not getattr(stream, "running", False):
            return ""
        for path in BASE_STATION_FILES:
            try:
                raw = stream.read_file(path)
            except Exception:
                raw = ""
            version = AposClient._file_data(raw)
            if version:
                return version
        return ""

    def _prime_system(self):
        paths = ["/System/CustomSN", "/System/BoardSN", "/System/FirmwareInfo"]
        stream = self.stream
        self._model = ""
        try:
            if stream is not None and getattr(stream, "running", False):
                # 先读复合属性(几十毫秒), 从里面拿到机型
                data = stream.read(["/System/FirmwareInfo"]) or {}
                fw = data.get("/System/FirmwareInfo")
                if isinstance(fw, str):
                    try:
                        fw = json.loads(fw)
                    except Exception:
                        fw = {}
                self._model = str((fw or {}).get("Model") or "") \
                    if isinstance(fw, dict) else ""
                if self._model not in NO_SN_MODELS:
                    # 普通机型 SN/BoardSN 在同一句柄上读得到, 补进快照;
                    # 超时给短一点, 读不到时代价也小。
                    data.update(stream.read(
                        ["/System/CustomSN", "/System/BoardSN"],
                        timeout_ms=1500) or {})
                # 属性读不到的机型(如 CLA_ZY2L)直接读机器人上的 SN 文件
                self._fill_system_from_files(
                    data, ["/System/CustomSN", "/System/BoardSN"])
            else:
                data = self.client.read(paths) or {}
        except Exception:
            data = {}
        if data:
            for path, value in data.items():
                if path in STATIC_PATHS and str(value or "").strip():
                    self._static_values[path] = value
                    self._static_done.add(path)
            with self._lock:
                self._snapshot = {"ok": dict(data), "failed": {},
                                  "ts": time.time()}

    def _fill_system_from_files(self, data, paths):
        """属性里没有的 SN 从机器人文件补（`/oem/bin/sys_data/custom.sn`）。

        CLA_ZY2L 的 `/System/CustomSN` 在 binpack 上返回空，但文件里就是 SN。
        """
        stream = self.stream
        if stream is None or not getattr(stream, "running", False):
            return
        for path in paths:
            # ★ 已经读到有效值才跳过：binpack 读 CustomSN 会带回空字符串，
            #   之前按 "path in data" 判断会把文件兜底也跳掉 → SN 一直空
            #   （用户 2026-11-26：CLA_ZY2L 老是读取失败）。
            if str(data.get(path) or "").strip() or path not in SN_FILES:
                continue
            for fn in SN_FILES[path]:
                try:
                    value = AposClient._file_data(stream.read_file(fn))
                except Exception:
                    value = ""
                if value:
                    data[path] = value
                    break

    def start_stream(self):
        """确保属性上传流开启（libapos 优先连接时通常已经在跑了）。

        ★ 连接流程现在先开流；这里主要给「老流程/退回轮询后再重开」兜底。
          开流之后的读全部走同一个 libapos_client 句柄(`UploadStream.read`)，
          不再碰会互卡的 librpc_base。
        """
        if self.stream is None or not self._connected:
            return False
        if self._stream_paused:
            return False                  # 已被续航测试等场景借用会话
        if getattr(self.stream, "running", False):
            return True                   # 已经在推流了
        # 等当前这一轮 librpc 读结束再开流 —— 两个客户端同时操作会互相卡死
        self._wait_read_idle()
        paths = list(self._stream_paths or self._paths)
        started = False
        # 停流后立刻重开时机器人可能还没释放上一个会话(实测偶发 Init 失败),
        # 重试两次即可恢复。
        for attempt in range(3):
            try:
                started = bool(self.stream.start(self._ip, paths))
            except Exception as e:
                log.debug(f"[参数] 上传流启动失败, 退回轮询: {e}")
                started = False
            if started:
                break
            time.sleep(0.5)
        # ★ 启动失败时不要 stream.stop(): 它会把订阅路径清空, 看门狗
        #   restart() 就再也没有路径可重试, 推送通道永远恢复不了。
        self._ready.set()             # 无论成败都开始轮询(失败就走 librpc 读)
        self._wake.set()
        return started

    def _start_mcu(self):
        if self.mcu is None:
            return
        try:
            self.mcu.start(self._ip)
        except Exception as e:
            log.debug(f"[参数] MCU 调试流启动失败: {e}")

    # ── 推流借用（续航测试独占） ──
    def pause_stream(self):
        """测试独占期间挂起本服务（用户 2026-11-27: 全局共用同一条推送会话）。

        ★ 不再停流 —— 会话是全局唯一的, 测试直接复用这条流; 这里只挂起
          本服务的采集/看门狗, 避免和测试的 keepalive 抢着重启会话。
          推送还在进, 设备页数值继续刷新(_stream_snapshot 不受暂停影响)。
        """
        self._stream_paused = True
        self._wake.set()

    def resume_stream(self):
        """恢复本服务（测试结束后调用）；测试若改窄了订阅, 重订回全量路径。"""
        was_paused = self._stream_paused
        self._stream_paused = False
        self._stream_only = bool(getattr(self.stream, "running", False))
        target = list(self._stream_paths or self._paths)
        stream = self.stream
        if stream is not None:
            subscribed = getattr(stream, "_paths", None)
            running = getattr(stream, "running", False)
            if (not running or subscribed is None
                    or set(subscribed) != set(target)):
                # 测试期间 keepalive 可能把订阅改窄成它的 4 条 —— 恢复宽订阅
                try:
                    stream.start(self._ip, target, preserve=True)
                except Exception:
                    pass
        #: 固定参数(SN/固件/型号等): 读到一次后缓存, 之后不再参与轮询/推送覆盖,
        #: 重新连接时清空（用户 2026-11-27）。
        self._static_values = {}
        self._static_done = set()
        #: 最后一次成功从设备读到数据的时间 —— 长时间没有数据判定为断连
        self._last_live = time.time()
        if not was_paused:
            return bool(getattr(self.stream, "running", False))
        self._watch_at = 0.0
        self._stream_retry_at = 0.0
        return self.start_stream()

    def disconnect(self):
        self._ready.clear()
        self.stop()
        if self.mcu is not None:
            try:
                self.mcu.stop()
            except Exception:
                pass
        if self.stream is not None:
            try:
                self.stream.stop()
            except Exception:
                pass
        try:
            self.client.disconnect()
        except Exception:
            pass
        self._connected = False

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="apos-param-service")
        self._thread.start()

    def stop(self, timeout=5.0):
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout)
        # ★ 卡在一次慢读里的线程 join 超时后不能丢引用: 否则下一次 start()
        #   会再开一个采集线程, 两个线程同时读同一个 SDK 句柄(快照抖动/句柄
        #   错乱), 且 _stop.clear() 会让旧线程永远退不出去。
        if t is not None and t.is_alive():
            log.warning("[参数] 采集线程未在 %.1f 秒内退出, 保持引用不再新建"
                        % timeout)
            return
        self._thread = None

    # ── 配置 ──
    def set_paths(self, paths, interval=None):
        with self._lock:
            self._paths = list(paths or [])
            if interval is not None:
                self.interval = max(0.05, float(interval))
        self._wake.set()

    def paths(self):
        with self._lock:
            return list(self._paths)

    def is_connected(self):
        """链路是否还有数据: GUI 用它实时把"已连接"改成"未连接"。"""
        return bool(self._connected)

    def _cache_static(self, ok):
        """固定参数只认第一次读到的值, 之后不再更新(重连时清空)。

        ok 会被原地补上缓存值 —— 后续轮询不再读这些路径, 界面仍能拿到。
        """
        for path in STATIC_PATHS:
            value = ok.get(path)
            if (path not in self._static_values
                    and str(value or "").strip()):
                self._static_values[path] = value
                self._static_done.add(path)
            if path in self._static_values:
                ok[path] = self._static_values[path]

    # ── 数据 ──
    def latest(self):
        with self._lock:
            ok = dict(self._snapshot["ok"])
            failed = dict(self._snapshot["failed"])
            ts = self._snapshot["ts"]
        pushed, pushed_ts = self._stream_snapshot()
        if pushed:
            # 固定参数一旦缓存, 推送里的新值也不覆盖(直到重连/换 IP)
            pushed = {p: v for p, v in pushed.items()
                      if p not in self._static_values}
            ok.update(pushed)
            for path in pushed:
                failed.pop(path, None)
            ts = max(ts, pushed_ts)
        mcu_vals, mcu_ts = self._mcu_snapshot()
        if mcu_vals:
            ok.update(mcu_vals)
            for path in mcu_vals:
                failed.pop(path, None)
            ts = max(ts, mcu_ts)
        return {"ok": ok, "failed": failed, "ts": ts}

    def _mcu_snapshot(self, max_age=10.0):
        """MCU 反馈（挡位）最新值。

        ★ 调试流线程还活着但不来帧时(机器人停发/只停发某类帧), 旧值会一直
          留在内存里被当成实时值显示。这里按每条的采集时间丢掉超过 max_age
          的值, 表格显示「—」, 用户能看出数据已冻结。
        """
        mcu = self.mcu
        if mcu is None or not getattr(mcu, "running", False):
            return {}, 0.0
        try:
            fn = getattr(mcu, "values_with_ts", None)
            if callable(fn):
                now = time.time()
                values = fn() or {}
                fresh = {p: v for p, (v, ts) in values.items()
                         if now - ts <= max_age}
                last = mcu.last_event() or 0.0
                return fresh, last if fresh else 0.0
            return mcu.values(), mcu.last_event() or 0.0
        except Exception:
            return {}, 0.0

    def _wait_read_idle(self, timeout=8.0):
        """等正在进行的那一轮读结束（_reading 是"读进行中"标志）。"""
        end = time.time() + timeout
        while self._reading.is_set() and time.time() < end:
            time.sleep(0.02)

    @staticmethod
    def _failed_paths(paths, ok, stream_on):
        """哪些路径算「读取失败」。

        ★ 推流开启时, 没出现过的路径只可能是"机器人侧暂未推送"（变化才发 /
          切换页面刚加入订阅），不能算失败；否则界面会冒出大量红色读取失败。
          只有真正主动去读的系统信息才可能失败。
        """
        if stream_on:
            return {p: "未返回" for p in paths
                    if p in READ_ONLY_PATHS and p not in ok}
        return {p: "未返回" for p in paths if p not in ok}

    def _stream_polling_fallback(self):
        """停掉推送流, 切到 librpc 轮询(推送服务异常时参数至少还能刷新)."""
        stream = self.stream
        log.info("[上传流] 连续重新订阅仍无推送, 改用 librpc 轮询")
        try:
            stream.stop(clear=False)
        except Exception:
            pass
        self._stream_fallback = True
        self._stream_stalls = 0
        self._stream_retry_at = time.time() + 30.0
        # libapos 优先连接时本来没有 librpc 会话, 这时补建一个
        if not getattr(self.client, "connected", False):
            try:
                self.client.connect(self._ip, retries=1)
            except Exception as e:
                log.debug(f"[上传流] 轮询兜底连接失败: {e}")
        self._wake.set()          # 让采集线程立刻用轮询读一次

    def _watch_stream(self, stall=6.0):
        """看门狗：推送流约 30~40 秒后会自己停（机器人侧），重发 Add/重订阅都
        无效，只有换一个新句柄重新 AposSdkInit 才能恢复（实测）。

        没有这条，界面会在启动半分钟后永远停在最后一个数值。
        """
        stream = self.stream
        if stream is None:
            return
        if self._stream_paused:
            return                        # 推流已被续航测试借用
        now = time.time()
        if now < getattr(self, "_watch_at", 0.0):
            return
        if self._stream_fallback:
            # 轮询兜底中: 每分钟试一次恢复推送, 成功则切回推送优先
            if now < self._stream_retry_at:
                return
            self._stream_retry_at = now + 30.0
            self._wait_read_idle()
            try:
                ok = bool(stream.start(
                    self._ip, list(self._stream_paths or self._paths),
                    preserve=True))
            except Exception as e:
                ok = False
                log.debug(f"[上传流] 恢复推送异常: {e}")
            if ok:
                self._stream_fallback = False
                self._stream_only = True
                self._watch_at = now + 15.0
                log.info("[上传流] 已重新订阅推送, 观察数据…")
            return
        if not getattr(stream, "running", False):
            # ★ 推送通道已经断了(重启失败退回轮询, 或模拟 wifi_reset 事件把
            #   机器人网络重置): 之后 post_event 会一直报「推流未就绪」。
            #   这里定期重试恢复, 网络回来后发送/曲线自动恢复正常, 不用重连。
            self._watch_at = now + 15.0
            self._wait_read_idle()
            try:
                if stream.restart():
                    log.info("[上传流] 已恢复上传流")
                else:
                    log.debug("[上传流] 恢复失败, 继续轮询")
            except Exception as e:
                log.debug(f"[上传流] 恢复异常: {e}")
            return
        try:
            last = stream.last_event()
        except Exception:
            return
        if not last or now - last <= stall:
            self._stream_stalls = 0      # 推送正常, 清零连续中断计数
            return
        self._watch_at = now + 15.0     # 最多 15 秒重启一次，避免反复空转
        self._stream_stalls += 1
        log.info("[上传流] 推送中断 %.0f 秒 (第 %d 次), 重新建立订阅"
                 % (now - last, self._stream_stalls))
        if self._stream_stalls >= 2:
            # 刚重启过又中断 → 重启解决不了, 直接切轮询(否则界面一直很慢)
            self._stream_polling_fallback()
            return
        self._wait_read_idle()
        for _ in range(3):
            try:
                if stream.restart():
                    return
            except Exception as e:
                log.debug(f"[上传流] 重启失败: {e}")
            time.sleep(0.5)
        log.info("[上传流] 重启失败, 退回轮询")
        self._stream_polling_fallback()

    def _read(self, paths):
        """批量读：推送流在跑时用同一个 libapos_client 句柄，否则走 librpc_base。"""
        self._reading.set()
        try:
            if (self.stream is not None
                    and getattr(self.stream, "running", False)
                    and not self._stream_fallback):
                # ★ 只读系统信息(复合 JSON)和二进制数组(上传流不一定推送)；
                #   普通数值属性一律用推送值（binpack 读数值会得到 0）。
                only = [p for p in paths
                        if p in READ_ONLY_PATHS or p in BINARY_PATHS]
                if not only:
                    # 曲线页只订阅曲线路径, 这里没有可读项 —— 但仍要在这个句柄上
                    # 周期性做一次读，否则机器人推送约 35 秒后会自己停掉(实测)。
                    # 固定参数已缓存后不要再读它们，用一条廉价的属性做保活即可。
                    only = [p for p in READ_ONLY_PATHS
                            if p not in self._static_done]
                    if not only:
                        only = ["/BatteryBattery/BatteryLevel"]
                return self.stream.read(only) or {}
            return self.client.read(paths) or {}
        finally:
            self._reading.clear()

    def _stream_snapshot(self):
        """推送流里的最新值（没有则空）。"""
        stream = self.stream
        if (stream is None or not getattr(stream, "running", False)
                or self._stream_fallback):
            return {}, 0.0
        try:
            return stream.values(), stream.last_event() or 0.0
        except Exception:
            return {}, 0.0

    def read_battery(self):
        """"电量监测读取入口：取最近一次快照；读不到返回 -1。"""
        try:
            v = self.latest()["ok"].get("/BatteryBattery/BatteryLevel", -1)
            return int(v)
        except (TypeError, ValueError):
            return -1

    def read_once(self, paths=None):
        """同步读一次（连接/诊断用）。失败返回 {}。

        推送流在跑时走同一个 libapos_client 句柄读 —— 不能再碰 librpc_base。
        """
        paths = list(paths or self.paths())
        if not paths:
            return {}
        try:
            stream = self.stream
            if stream is not None and getattr(stream, "running", False):
                pushed = stream.values()
                data = {p: pushed[p] for p in paths if p in pushed}
                rest = [p for p in paths if p not in pushed
                        and (p in READ_ONLY_PATHS or p in BINARY_PATHS)]
                if self._stream_only and self._model in NO_SN_MODELS:
                    # 这些机型的普通属性(如 CustomSN)在 binpack 上读不到, 还会
                    # 触发 SDK 二分重试(每次好几秒); 只读复合属性, SN 走文件。
                    rest = [p for p in rest if p in COMPOSITE_PATHS]
                if rest:
                    data.update(stream.read(rest) or {})
                self._fill_system_from_files(data, paths)
            else:
                data = self.client.read(paths) or {}
        except Exception as e:
            log.debug(f"[参数] 单次读取失败: {e}")
            return {}
        for p in paths:
            if p in self._static_values:
                data.setdefault(p, self._static_values[p])
        return {p: data[p] for p in paths if p in data}

    # ── 线程 ──
    def _loop(self):
        failures = 0
        while not self._stop.is_set():
            # 先等一个周期再读：连接阶段让 ConnectThread 先读型号/SN/固件/基站
            # （同一把 librpc 锁），开流之后这里就会自动切到推送句柄。
            wait = self.interval
            if (self.stream is not None
                    and getattr(self.stream, "running", False)
                    and not self._stream_fallback):
                # 推送流已经给了 5~50Hz 的数据，轮询只用来兜底补漏
                wait = max(wait, 5.0)
            self._wake.wait(wait)
            self._wake.clear()
            if self._stop.is_set():
                break
            if not self._ready.is_set():
                continue
            self._watch_stream()
            if self._stream_paused:
                # 续航测试独占推流期间: 不读属性、不刷快照，恢复后自动继续
                continue
            paths = [p for p in self.paths() if p not in self._static_done]
            stream_on = (self.stream is not None
                         and getattr(self.stream, "running", False)
                         and not self._stream_fallback)
            # ★ 曲线页只含 /Mcu/* 时 _apos_paths 过滤后是空列表 —— 仍然要在
            #   推送流句柄上做一次保活读, 否则机器人约 35 秒后自己停推, 只剩
            #   看门狗反复重建会话(_read() 里会兜底读 READ_ONLY_PATHS)。
            if not paths and not stream_on:
                continue
            try:
                data = self._read(paths)
                ok = {p: data[p] for p in paths if p in data}
                if not ok and not stream_on:
                    # 整批读空(无线链路抖动/机器人忙): 稍等一下立刻重试一次,
                    # 避免界面把整页参数刷成"读取失败"。
                    self._stop.wait(0.2)
                    data = self._read(paths)
                    ok = {p: data[p] for p in paths if p in data}
                if data:
                    self._last_live = time.time()
                self._cache_static(ok)
                if stream_on:
                    # 推送是"变化才发", 这一轮没推到的路径沿用上一次的值,
                    # 避免界面在两次推送之间闪成空白/0。
                    with self._lock:
                        prev = dict(self._snapshot["ok"])
                    for path, value in prev.items():
                        ok.setdefault(path, value)
                    failed = self._failed_paths(paths, ok, stream_on=True)
                else:
                    failed = self._failed_paths(paths, ok, stream_on=False)
                with self._lock:
                    self._snapshot = {"ok": ok, "failed": failed,
                                      "ts": time.time()}
                failures = 0
                if (self._connected and not self._stream_paused
                        and time.time() - self._last_live > 15.0):
                    self._connected = False
                    log.warning("[参数] 长时间没有读到设备数据, 判定连接已断开")
            except Exception as e:
                with self._lock:
                    self._snapshot = {
                        "ok": {},
                        "failed": {p: f"{type(e).__name__}: {e}" for p in paths},
                        "ts": time.time()}
                failures += 1
                log.debug(f"[参数] 读取失败 {failures} 次: {e}")
                if failures >= 5:
                    self._connected = False
                    time.sleep(min(10.0, 2.0 * failures))
        # 退出前不再改快照：保留最后一次有效值
