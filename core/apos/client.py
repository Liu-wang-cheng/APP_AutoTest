# -*- coding: utf-8 -*-
"""APOS SDK 的 ctypes 封装（只读优先）。

行为与 `D:\\claude_test\\apos_min_client.py` 一致（该脚本已在真机验证），差别：
  · DLL/license 从工具自己的 `BASE_DIR\\apos\\` 读取；
  · 缺文件/加载失败给出可直接展示给用户的中文原因；
  · 不打印 license，不把 license 写日志。
"""
import ctypes
import json
import os
import re
import socket
import threading
import time
from pathlib import Path

from core.apos import sdk as sdk_mod
from core.logger import get_logger

log = get_logger()


#: 基站版本文件（与 AutopackDebugger 一致；第二项是厂商源码里的原拼写）
BASE_STATION_FILES = (
    "/tmp/base_station_version",
    "/userdata/run/basestaion_version",
)

#: 让机器人去查询基站版本并写回文件的事件（AutopackDebugger 读文件前会先发它）
BASE_STATION_EVENT = "/TaskManager/GetBaseStationVersion"

#: 返回值是「JSON 字符串」且内部未转义的属性：不能和普通属性同批读，
#: 否则整条响应都不是合法 JSON（值里的引号没转义）。
COMPOSITE_PATHS = frozenset({"/System/FirmwareInfo", "/Areas/GoodsInfo"})

#: 返回值是二进制（BinpackType_BINARY），ToString 会输出一长串十六进制数字，
#: 不是合法 JSON。解析前要把这个值加引号，再按十六进制还原。
BINARY_PATHS = frozenset({"/IrAlongWall/AdcDifValue[MaxDetectionAdcChannelNum]"})


def repair_binary_json(text):
    """二进制属性的值是一串裸十六进制数字（非法 JSON），加引号后才能解析。"""
    for path in BINARY_PATHS:
        pattern = re.compile(r'("' + re.escape(path) + r'"\s*:\s*)'
                             r'([0-9A-Fa-f]{16,})')
        text = pattern.sub(r'\1"\2"', text)
    return text


class AposError(RuntimeError):
    """SDK 不可用（目录缺失/位数不对/依赖缺失/初始化失败）。"""


class AposClient:
    def __init__(self, sdk_dir=None, license_text=None):
        self.sdk_dir = Path(sdk_dir) if sdk_dir else sdk_mod.default_sdk_dir()
        info = sdk_mod.check_sdk(self.sdk_dir)
        if not info["ok"]:
            raise AposError("SDK 目录不完整，缺少: " + ", ".join(info["missing"]))
        self.license = license_text if license_text is not None else sdk_mod.load_license(self.sdk_dir)
        if not self.license:
            raise AposError("license.dat 为空或不存在")
        dll = self.sdk_dir / "librpc_base.dll"
        try:
            if hasattr(os, "add_dll_directory"):
                os.add_dll_directory(str(self.sdk_dir))
            self.lib = ctypes.WinDLL(str(dll))
        except OSError as e:
            raise AposError(f"加载 {dll.name} 失败（可能缺少 VC++ 运行库或位数不对）: {e}")
        self._bind()
        self.handle = -1
        self._lock = threading.RLock()
        self.last_error = ""
        #: 本机不存在的属性路径（机型差异），二分定位后记住, 后续不再重试。
        #: 实测每台机器有 20 条左右不存在的路径, 不记住的话每次轮询都会
        #: 触发机器人侧 `AposSdkRead failed` 并把请求量放大好几倍。
        self._bad_paths = set()
        self._miss_counts = {}       # path -> 连续读不到的次数
        self._probing = False
        self._channel_ok = None      # 本次调用内一次通道健康探测的缓存

    def _bind(self):
        L = self.lib
        L.CppAposSdkInit.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        L.CppAposSdkInit.restype = ctypes.c_int32
        L.CppAposSdkDeInit.argtypes = [ctypes.c_int32]
        L.CppAposSdkDeInit.restype = ctypes.c_int32
        L.CppAposSdkService.argtypes = [ctypes.c_int32, ctypes.c_char_p, ctypes.c_int32]
        L.CppAposSdkService.restype = ctypes.c_char_p
        L.CppAposSdkRead.argtypes = [ctypes.c_int32, ctypes.POINTER(ctypes.c_char_p), ctypes.c_int32]
        L.CppAposSdkRead.restype = ctypes.c_char_p
        L.CppAposSdkReadFile.argtypes = [ctypes.c_int32, ctypes.c_char_p]
        L.CppAposSdkReadFile.restype = ctypes.c_char_p
        # 发事件（无参数事件传 0 即可）；返回值 -1 只表示"没有同步响应"，
        # 机器仍会处理事件 —— 实测发完/GetBaseStationVersion 后版本文件才出现。
        if hasattr(L, "CppAposSdkSendEvent"):
            L.CppAposSdkSendEvent.argtypes = [ctypes.c_int32, ctypes.c_char_p,
                                              ctypes.c_int32]
            L.CppAposSdkSendEvent.restype = ctypes.c_int32
        if hasattr(L, "get_log"):
            L.get_log.argtypes = []
            L.get_log.restype = ctypes.c_char_p

    @property
    def connected(self):
        return self.handle > 0

    def connect(self, ip, retries=3, delay=1.0):
        with self._lock:
            self.last_error = ""
            # ★ 先做 TCP 预检(端口 8801): 设备离线/不在线时 SDK init 每次都要
            #   等 3~4 秒, 3 次重试就是十几秒的"连接中"(用户反馈)。
            if not self._reachable(ip):
                self.last_error = "unreachable"
                log.debug(f"[APOS] TCP 预检失败, IP 不可达: {ip}")
                return False
            ok = self._connect_unlocked(ip, retries, delay)
            if not ok:
                self.last_error = "sdk_init"
            return ok

    @staticmethod
    def _reachable(ip, port=8801, timeout=1.5, tries=2):
        """端口能连上才值得调 SDK；离线设备快速失败, 不让界面干等。"""
        for attempt in range(max(1, tries)):
            try:
                with socket.create_connection((str(ip), port), timeout=timeout):
                    return True
            except OSError:
                if attempt + 1 < tries:
                    time.sleep(0.3)
        return False

    def _connect_unlocked(self, ip, retries, delay):
        if self.connected:
            self.disconnect()
            # 给机器人一点时间释放旧会话(连续切换 IP 时偶发拒绝新连接)
            time.sleep(0.1)
        attempts = max(1, int(retries))
        for attempt in range(attempts):
            self.handle = self.lib.CppAposSdkInit(
                self.license.encode("utf-8"), str(ip).encode("utf-8"))
            if self.handle > 0:
                # ★ 拿到句柄 ≠ 通道可用: 推送通道约 0.4s 才就绪, 而且要确认
                #   真能读到属性, 否则会出现"连上了但参数/基站版本全空"。
                if self._wait_ready(timeout=1.5):
                    self._bad_paths.clear()   # 换了机器人, 机型差异重新学习
                    return True
                log.debug("[APOS] 句柄已建立但通道未就绪, 重试连接")
                try:
                    self.lib.CppAposSdkDeInit(self.handle)
                except Exception:
                    pass
                self.handle = -1
            else:
                log.debug(f"[APOS] CppAposSdkInit 失败(第 {attempt + 1} 次): {ip}")
            if attempt + 1 < attempts:
                time.sleep(delay)
        return False

    def _wait_ready(self, timeout=1.5):
        """轮询一条轻量属性，确认推送通道真的通了才认为连接成功。"""
        deadline = time.time() + max(0.2, float(timeout))
        self._probing = True          # 探测阶段失败不代表路径不存在, 不能记黑名单
        try:
            while time.time() < deadline:
                try:
                    if self._read_many(["/System/CustomSN"]):
                        return True
                except Exception:
                    pass
                time.sleep(0.15)
            return False
        finally:
            self._probing = False

    def disconnect(self):
        with self._lock:
            if not self.connected:
                return
            self._disconnect_unlocked()

    def _disconnect_unlocked(self):
        if self.connected:
            try:
                self.lib.CppAposSdkDeInit(self.handle)
            finally:
                self.handle = -1

    def read(self, paths):
        """批量读属性 → dict；失败/空返回 {}。"""
        if not self.connected or not paths:
            return {}
        with self._lock:
            return self._read_unlocked(paths)

    #: 返回值是「JSON 字符串」且内部未转义的属性：不能和普通属性同批读
    COMPOSITE_PATHS = COMPOSITE_PATHS

    def _read_unlocked(self, paths):
        self._channel_ok = None      # 每次调用重新探测一次
        data = {}
        safe = [p for p in paths if p not in self.COMPOSITE_PATHS]
        if safe:
            data.update(self._read_many(safe))
        for p in paths:
            if p not in self.COMPOSITE_PATHS:
                continue
            val = self._extract_composite(self._raw_read([p]), p)
            if val is not None:
                data[p] = val
        return data

    def _read_many(self, paths):
        """批量读；某条路径让整批 JSON 解析失败时二分拆开，只逐条读坏的那一小段。

        单条路径确认读不到（不是链路抖动）时记进 `_bad_paths`，后续轮询直接
        跳过 —— 这些是机型差异导致不存在的参数，机器人每次都会报
        `AposSdkRead failed`，反复重试只会放大请求量。
        """
        paths = [p for p in paths if p not in self._bad_paths]
        if not paths:
            return {}
        parsed = self._parse_read(self._raw_read(paths))
        if parsed:
            for p in parsed:
                self._miss_counts.pop(p, None)
            return {p: parsed[p] for p in paths if p in parsed}
        if len(paths) == 1:
            if self._probing:
                return {}
            # 通道本身读不到东西(链路抖动/机器人忙)时, 不能把"读不到"当成
            # "机型没有该参数" —— 否则一次整体故障会把整页路径全拉黑。
            if not self._channel_healthy():
                return {}
            # 同一路径连续两轮都读不到才认定为"机型没有该参数"。
            # 只凭一次失败就拉黑会把偶发抖动误判掉, 之后就永远读不到了。
            path = paths[0]
            misses = self._miss_counts.get(path, 0) + 1
            self._miss_counts[path] = misses
            if misses >= 2:
                self._bad_paths.add(path)
            return {}
        mid = len(paths) // 2
        out = self._read_many(paths[:mid])
        out.update(self._read_many(paths[mid:]))
        return out

    def bad_paths(self):
        return set(self._bad_paths)

    def _channel_healthy(self):
        """读一条固定存在的属性判断通道是否可用（每次 read() 只探测一次）。"""
        if self._channel_ok is None:
            self._channel_ok = bool(self._parse_read(
                self._raw_read(["/System/CustomSN"])))
        return self._channel_ok

    def _raw_read(self, paths):
        arr = (ctypes.c_char_p * len(paths))(*[p.encode("utf-8") for p in paths])
        return self.lib.CppAposSdkRead(self.handle, arr, len(paths))

    @staticmethod
    def _parse_read(raw):
        if not raw:
            return {}
        try:
            text = repair_binary_json(raw.decode("utf-8", "replace"))
            data = json.loads(text)
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _extract_composite(raw, path):
        """取复合属性的值：正常 JSON 直接取；未转义的字符串用正则截取。"""
        if not raw:
            return None
        text = raw.decode("utf-8", "replace")
        try:
            data = json.loads(text)
            if isinstance(data, dict) and path in data:
                return data[path]
        except Exception:
            pass
        m = re.search(r'"%s"\s*:\s*"(.*)"\s*,\s*"AposTimeStamp"'
                      % re.escape(path), text, re.S)
        return m.group(1) if m else None

    def service(self, event_id, timeout_ms=6000):
        if not self.connected:
            return None
        with self._lock:
            return self._service_unlocked(event_id, timeout_ms)

    def _service_unlocked(self, event_id, timeout_ms):
        raw = self.lib.CppAposSdkService(
            self.handle, str(event_id).encode("utf-8"), int(timeout_ms))
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            return None

    def read_file(self, path):
        if not self.connected:
            return ""
        with self._lock:
            return self._read_file_unlocked(path)

    def _read_file_unlocked(self, path):
        raw = self.lib.CppAposSdkReadFile(self.handle, str(path).encode("utf-8"))
        return raw.decode("utf-8", "replace") if raw else ""

    def send_event(self, event_id, binpack_handle=0):
        """发 APOS 事件（无参数事件 binpack_handle=0）。返回 SDK 原始返回值。"""
        if not self.connected:
            return -1
        with self._lock:
            return int(self.lib.CppAposSdkSendEvent(
                self.handle, str(event_id).encode("utf-8"),
                int(binpack_handle or 0)))

    def _scan_base_station_files(self):
        for path in BASE_STATION_FILES:
            try:
                version = self._file_data(self.read_file(path))
            except Exception:
                version = ""
            if version:
                return version
        return ""

    def read_base_station_version(self, retries=2, delay=1.0, trigger=True):
        """读基站版本。

        基站版本不是 APOS 属性，而是机器人上的文件（AutopackDebugger 的
        `TryGetBaseStationVersion` 就是这么做的）。文件不在时先发
        `/TaskManager/GetBaseStationVersion` 让机器人去问基站并把版本写回文件，
        再重试读取。返回空串表示确实读不到（例如机器没装基站）。
        """
        version = self._scan_base_station_files()
        if version or not trigger:
            return version
        try:
            self.send_event(BASE_STATION_EVENT)
        except Exception:
            pass
        tries = max(1, int(retries or 1))
        for _ in range(tries):
            time.sleep(delay)
            version = self._scan_base_station_files()
            if version:
                return version
        return ""

    @staticmethod
    def _file_data(text):
        """从 CppAposSdkReadFile 的返回里取 FileData。

        正常返回: {"/System/FileStrOp":{"FilePath":..., "FileData":"8.2.1.1"}, ...}
        文件不存在时返回 "{}"；也兼容返回值本身就是纯文本的机型。
        """
        text = (text or "").strip()
        if not text:
            return ""
        try:
            data = json.loads(text)
        except Exception:
            # ★ 机器人有时把文件里的换行原样塞进 JSON 字符串(SN 文件末尾就有
            #   一个 \n), 整段不是合法 JSON —— 用正则把 FileData 抠出来。
            m = re.search(r'"FileData"\s*:\s*"([^"]*)"', text, re.S)
            if m:
                return m.group(1).replace("\\n", "\n").strip()
            return "" if text.startswith("{") else text
        if not isinstance(data, dict):
            return ""
        op = data.get("/System/FileStrOp")
        if isinstance(op, str):
            try:
                op = json.loads(op)
            except Exception:
                op = {}
        if isinstance(op, dict):
            value = op.get("FileData")
        else:
            value = data.get("FileData")
        return str(value or "").strip()

    def log(self):
        if not hasattr(self.lib, "get_log"):
            return ""
        raw = self.lib.get_log()
        return raw.decode("utf-8", "replace") if raw else ""
