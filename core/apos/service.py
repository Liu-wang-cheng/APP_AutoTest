# -*- coding: utf-8 -*-
"""后台参数采集：一个线程、一个 SDK 句柄、固定间隔批量 read。

纯 Python（不 import PySide6）—— GUI 用 QTimer 读 `latest()` 快照，core 可直接单测。
"""
import threading
import time

from core.apos.client import BINARY_PATHS
from core.logger import get_logger

log = get_logger()

#: 属性上传流不会推送、只能读的属性（系统信息是复合 JSON）。
#: ★ 数值类属性必须走推送：binpack 读对数值属性返回的是 0（缺类型信息），
#:   拿它当数据源会让整页参数显示成 0。
READ_ONLY_PATHS = ("/System/CustomSN", "/System/BoardSN",
                   "/System/FirmwareInfo")

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

    # ── 生命周期 ──
    def connect(self, ip):
        # ★ 换设备/重连前先停掉旧采集线程和旧推流：librpc_base 连接(prime 读)
        #   与上一台的 libapos 上传流同时存在会互卡，表现为连接慢、SN/固件读空。
        self._ready.clear()
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
        ok = bool(self.client.connect(ip))
        self._ip = str(ip)
        self._connected = ok
        if ok:
            # ★ 换设备后旧快照不能带过去（否则界面会先显示上一台机器的值）
            with self._lock:
                self._snapshot = {"ok": {}, "failed": {}, "ts": 0}
            self._start_mcu()
            # 先用一次快速读把型号/固件/SN 放进快照 —— 界面不用等轮询周期
            # (这三项是复合属性, 单读只要几十毫秒)。
            self._prime_system()
            # ★ 立刻开推送流: 开流后所有属性读都走同一个 libapos_client 句柄
            #   (`stream.read`)，不再碰会互卡的 librpc_base。
            self.start_stream()
            self.start()
            self._ready.set()
        return ok

    def read_base_station(self, retries=2, delay=0.6):
        """读基站版本（机器人文件，只能走 librpc_base）。

        ★ 推送流和 librpc 文件读不能同时进行（实测会把流句柄卡死/读空），
        所以这里临时停流 → 读文件 → 重新开流。文件存在时只要几十毫秒，
        停流窗口几乎无感；没有基站的机型最多等约 6 秒，期间参数改走轮询。
        """
        was_streaming = bool(self.stream is not None
                             and getattr(self.stream, "running", False))
        if was_streaming:
            self._wait_read_idle()       # 别在 stream.read 进行中把流停掉
            try:
                self.stream.stop()
            except Exception:
                pass
        try:
            return self.client.read_base_station_version(retries=retries,
                                                         delay=delay)
        except Exception:
            return ""
        finally:
            if was_streaming and self._connected:
                self._wait_read_idle()      # 等轮询那一轮 librpc 读结束
                self.start_stream()         # 内部带重试

    def _prime_system(self):
        paths = ["/System/CustomSN", "/System/BoardSN", "/System/FirmwareInfo"]
        try:
            data = self.client.read(paths) or {}
        except Exception:
            data = {}
        if data:
            with self._lock:
                self._snapshot = {"ok": dict(data), "failed": {},
                                  "ts": time.time()}

    def start_stream(self):
        """连接后的第二步：开属性上传流。

        ★ 必须等**所有 librpc_base 读操作做完再开**（型号/SN/固件/基站版本）:
          实测两套客户端同时用会互相卡死 —— 推送流一开, CppAposSdkRead 一次读
          要 11~22 秒且返回空，基站版本文件读也会失败。开流之后的读全部走
          同一个 libapos_client 句柄(`UploadStream.read`)，不再碰 librpc_base。
        """
        if self.stream is None or not self._connected:
            return False
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
        if not started:
            try:
                self.stream.stop()
            except Exception:
                pass
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

    # ── 数据 ──
    def latest(self):
        with self._lock:
            ok = dict(self._snapshot["ok"])
            failed = dict(self._snapshot["failed"])
            ts = self._snapshot["ts"]
        pushed, pushed_ts = self._stream_snapshot()
        if pushed:
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

    def _watch_stream(self, stall=6.0):
        """看门狗：推送流约 30~40 秒后会自己停（机器人侧），重发 Add/重订阅都
        无效，只有换一个新句柄重新 AposSdkInit 才能恢复（实测）。

        没有这条，界面会在启动半分钟后永远停在最后一个数值。
        """
        stream = self.stream
        if stream is None or not getattr(stream, "running", False):
            return
        now = time.time()
        if now < getattr(self, "_watch_at", 0.0):
            return
        try:
            last = stream.last_event()
        except Exception:
            return
        if not last or now - last <= stall:
            return
        self._watch_at = now + 15.0     # 最多 15 秒重启一次，避免反复空转
        log.info("[上传流] 推送中断 %.0f 秒, 重新建立订阅" % (now - last))
        self._wait_read_idle()
        for _ in range(3):
            try:
                if stream.restart():
                    return
            except Exception as e:
                log.debug(f"[上传流] 重启失败: {e}")
            time.sleep(0.5)
        log.info("[上传流] 重启失败, 退回轮询")

    def _read(self, paths):
        """批量读：推送流在跑时用同一个 libapos_client 句柄，否则走 librpc_base。"""
        self._reading.set()
        try:
            if self.stream is not None and getattr(self.stream, "running", False):
                # ★ 只读系统信息(复合 JSON)和二进制数组(上传流不一定推送)；
                #   普通数值属性一律用推送值（binpack 读数值会得到 0）。
                only = [p for p in paths
                        if p in READ_ONLY_PATHS or p in BINARY_PATHS]
                if not only:
                    # 曲线页只订阅曲线路径, 这里没有可读项 —— 但仍要在这个句柄上
                    # 周期性做一次读，否则机器人推送约 35 秒后会自己停掉(实测)。
                    only = list(READ_ONLY_PATHS)
                return self.stream.read(only) or {}
            return self.client.read(paths) or {}
        finally:
            self._reading.clear()

    def _stream_snapshot(self):
        """推送流里的最新值（没有则空）。"""
        stream = self.stream
        if stream is None or not getattr(stream, "running", False):
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
                if rest:
                    data.update(stream.read(rest) or {})
            else:
                data = self.client.read(paths) or {}
        except Exception as e:
            log.debug(f"[参数] 单次读取失败: {e}")
            return {}
        return {p: data[p] for p in paths if p in data}

    # ── 线程 ──
    def _loop(self):
        failures = 0
        while not self._stop.is_set():
            # 先等一个周期再读：连接阶段让 ConnectThread 先读型号/SN/固件/基站
            # （同一把 librpc 锁），开流之后这里就会自动切到推送句柄。
            wait = self.interval
            if self.stream is not None and getattr(self.stream, "running", False):
                # 推送流已经给了 5~50Hz 的数据，轮询只用来兜底补漏
                wait = max(wait, 5.0)
            self._wake.wait(wait)
            self._wake.clear()
            if self._stop.is_set():
                break
            if not self._ready.is_set():
                continue
            self._watch_stream()
            paths = self.paths()
            stream_on = (self.stream is not None
                         and getattr(self.stream, "running", False))
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
