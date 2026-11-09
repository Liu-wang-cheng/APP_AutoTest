# -*- coding: utf-8 -*-
"""属性上传流：AutopackDebugger 同款的实时数据通道。

厂商工具不是轮询读属性，而是：
  1. `AposSdkSetEventSubscribe` 订阅 `/AposSystem/PropertyUpload`；
  2. 发 `/AposSystem/PropertyUploadAdd`，参数是 `{属性路径: {offset:0, len:N}}`；
  3. 机器人按变化/周期把值推到 `PropertyUpload` 事件里(实测 ~50 次/秒)。

和 `CppAposSdkRead` 批量读相比：
  · 不存在的路径会被静默忽略 —— 不会像批量读那样"一条坏路径拖垮整批"，
    也就没有机器人侧 `AposSdkRead failed` 的刷屏和客户端二分重试；
  · 曲线数据是推送的，5Hz 刷新不需要反复读全表。

依赖：`libapos_client.dll` + `BinpackCLR.dll`（后者负责构造请求/解析事件）。
这两个文件都在 `apos\\` 目录；缺 BinpackCLR 时本模块 start() 返回 False，
调用方退回普通轮询读。
"""
import ctypes
import json
import threading
import time
from pathlib import Path

from core.apos import sdk as sdk_mod
from core.apos import client as client_mod
from core.logger import get_logger

log = get_logger()

#: 事件回调签名：(handle, topic, len, binpack_ptr)
EVENT_CB = ctypes.CFUNCTYPE(None, ctypes.c_int32, ctypes.c_char_p,
                            ctypes.c_int32, ctypes.c_void_p)


class UploadStream:
    """订阅属性上传流；线程安全。值由 SDK 内部线程回调写入。"""

    def __init__(self, sdk_dir=None, license_text=None):
        self.sdk_dir = Path(sdk_dir) if sdk_dir else sdk_mod.default_sdk_dir()
        self._license = (license_text if license_text is not None
                         else sdk_mod.load_license(self.sdk_dir))
        self._lock = threading.Lock()
        self._api_lock = threading.RLock()   # BinpackCLR 调用(回调线程/读取线程)
        #: 读与句柄生命周期的互斥锁: 读进行中不能被 stop() DeInit 掉句柄
        #  (后台 read_once / 采集线程与断开/切换设备并发时, 拿已销毁的句柄
        #  读最坏是进程访问违例)。回调线程只碰 _api_lock, 不会因这把锁死锁。
        self._life = threading.RLock()
        self._values = {}            # path -> (value, ts)
        #: 返回值不是合法 JSON 的属性（实测 /IrAlongWall/AdcDifValue[...] 会吐
        #: 一大串裸数字）—— 会让整批解析失败, 二分定位后记住不再读。
        self._bad_paths = set()
        self._last_event = 0.0
        self._handle = -1
        self._cb = None              # 必须保持引用，否则回调被 GC
        self._root = None
        self._api = None             # (libapos_client, BinpackCLR)
        self._paths = []
        self._ip = ""

    # ── 生命周期 ──
    @property
    def running(self):
        return self._handle > 0

    def start(self, ip, paths, preserve=False):
        """连接机器人并开始接收上传流。失败返回 False（调用方退回轮询）。

        preserve=True: 重启订阅时保留已收到的值（界面不闪空）。
        """
        with self._life:
            return self._start(ip, paths, preserve)

    def _start(self, ip, paths, preserve=False):
        self.stop(clear=not preserve)
        paths = [str(p) for p in (paths or []) if p]
        if not ip or not paths or not self._license:
            return False
        self._ip = str(ip)
        try:
            client, bp = self._load_api()
        except Exception as e:
            log.info(f"[上传流] 不可用(BinpackCLR 缺失或加载失败): {e}")
            return False
        try:
            handle = client.AposSdkInit(self._license.encode("utf-8"),
                                        str(ip).encode("utf-8"))
        except Exception as e:
            log.info(f"[上传流] AposSdkInit 异常: {e}")
            return False
        if handle <= 0:
            log.info(f"[上传流] AposSdkInit 失败: {ip}")
            return False
        self._api = (client, bp)
        self._handle = handle
        try:
            cb = EVENT_CB(self._on_event)
            self._cb = cb
            topics = [b"/AposSystem/PropertyUpload"]
            arr = (ctypes.c_char_p * len(topics))(*topics)
            if client.AposSdkSetEventSubscribe(handle, cb, arr, len(topics)) != 0:
                log.info("[上传流] 订阅 PropertyUpload 失败")
                self.stop()
                return False
            root = bp.Init()
            for path in paths:
                sub = bp.Init()
                bp.SetInt32(sub, b"offset", 0)
                # 二进制数组按厂商的 6 字节/段结构取，留 256 字节足够
                bp.SetInt32(sub, b"len",
                            256 if path in client_mod.BINARY_PATHS else 800)
                bp.SetObject(root, path.encode("utf-8"), sub)
            self._root = root
            self._paths = list(paths)
            client.AposSdkSendEvent(handle, b"/AposSystem/PropertyUploadAdd", root)
        except Exception as e:
            log.info(f"[上传流] 启动失败: {e}")
            self.stop()
            return False
        log.info(f"[上传流] 已订阅 {len(paths)} 条属性: {ip}")
        return True

    def stop(self, clear=True):
        # 等正在进行的读结束再 DeInit（读调用有超时上限, 不会无限等）
        with self._life:
            self._stop_locked(clear)

    def _stop_locked(self, clear=True):
        handle, self._handle = self._handle, -1
        if handle > 0 and self._api is not None:
            try:
                self._api[0].AposSdkDeInit(handle)
            except Exception:
                pass
        # ★ 先 DeInit 再丢回调引用: SDK 内部线程可能还在调用回调，
        #   CFUNCTYPE 对象被回收后 C++ 再调就是野指针（实测会直接崩进程）。
        self._cb = None
        self._root = None
        if clear:
            with self._lock:
                self._values.clear()
                self._last_event = 0.0
        self._paths = []

    def restart(self):
        """换一个新句柄重新订阅（机器人推送约 35 秒后会自己停掉，实测重发
        PropertyUploadAdd / 重新 Subscribe 都无效，只有新的 AposSdkInit 能恢复）。

        保留已收到的值，界面不会闪空。
        """
        paths = list(self._paths)
        if not paths:
            return False
        return self.start(self._ip, paths, preserve=True)

    # ── 数据 ──
    def values(self):
        with self._lock:
            return {p: v for p, (v, _ts) in self._values.items()}

    def last_event(self):
        with self._lock:
            return self._last_event

    def read(self, paths, timeout_ms=3000):
        """用**同一个句柄**做一次 binpack 读（和推送流共用 libapos_client）。

        为什么不走 `CppAposSdkRead`：librpc_base 和 libapos_client 两套客户端
        同时用会互相卡死 —— 实测推送流一开，CppAposSdkRead 一次读要 11~22 秒
        且返回空。厂商本来也是用 libapos_client 的 binpack 读。

        不存在的路径会让返回码是 -1，但**有效值仍然在输出里**，所以不看返回码，
        直接解出拿到的键值（无二分、无报错刷屏）。
        """
        with self._life:      # 读期间不许 stop()/start() 换句柄
            return self._read_batch(paths, timeout_ms)

    def _read_batch(self, paths, timeout_ms=3000):
        if not self.running or not paths:
            return {}
        paths = [str(p) for p in paths if p]
        plain = [p for p in paths if p not in client_mod.COMPOSITE_PATHS]
        composites = [p for p in paths if p in client_mod.COMPOSITE_PATHS]
        data = {}
        # 复合属性(固件信息)先读: 单条很快(几十毫秒), 让型号/固件立刻显示,
        # 不用等普通批次(首次可能要做一次二分定位坏路径)。
        for path in composites:
            raw = self._read_raw([path], timeout_ms)
            value = client_mod.AposClient._extract_composite(raw, path)
            if value is not None:
                data[path] = value
        if plain:
            data.update(self._read_plain(plain, timeout_ms))
        return data

    def _read_plain(self, paths, timeout_ms=3000):
        """批量读普通属性；某条路径让整批不是合法 JSON 时二分定位并记住。"""
        paths = [p for p in paths if p not in self._bad_paths]
        if not paths:
            return {}
        obj = self._parse(self._read_raw(paths, timeout_ms))
        if obj is not None:
            return obj
        if len(paths) == 1:
            self._bad_paths.add(paths[0])
            return {}
        mid = len(paths) // 2
        out = self._read_plain(paths[:mid], timeout_ms)
        out.update(self._read_plain(paths[mid:], timeout_ms))
        return out

    @staticmethod
    def _parse(raw):
        if not raw:
            return None
        try:
            text = client_mod.repair_binary_json(
                raw.decode("utf-8", "replace"))
            obj = json.loads(text)
        except Exception:
            return None
        if not isinstance(obj, dict):
            return None
        return {k: v for k, v in obj.items() if k != "AposTimeStamp"}

    def _read_raw(self, paths, timeout_ms=3000):
        """一次 binpack 读，返回原始字节（失败 None）。"""
        client, bp = self._api
        try:
            # ★ 构造/解析 Binpack 时加锁（CLR 对象不并发），但**不能**在
            #   AposSdkRead 期间持锁：回调和读响应由 SDK 同一个收线程处理，
            #   锁住它会让回调卡住 → 读一直等到超时(实测 3 秒返回空)。
            with self._api_lock:
                req = bp.Init()
                for path in paths:
                    sub = bp.Init()
                    bp.SetInt32(sub, b"offset", 0)
                    bp.SetInt32(sub, b"len", 2000)
                    bp.SetObject(req, str(path).encode("utf-8"), sub)
                out = bp.Init()
            client.AposSdkRead(self._handle, req, out, int(timeout_ms))
            with self._api_lock:
                return bp.ToString(out)
        except Exception as e:
            log.debug(f"[上传流] 读取失败: {e}")
            return None

    # ── 内部 ──
    def _load_api(self):
        if self._api is not None:
            return self._api
        dll = self.sdk_dir / "libapos_client.dll"
        if not dll.is_file():
            raise FileNotFoundError(dll.name)
        bp_path = sdk_mod.binpack_path(self.sdk_dir)
        if bp_path is None:
            raise FileNotFoundError("BinpackCLR.dll")
        import os
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(str(self.sdk_dir))
            os.add_dll_directory(str(Path(bp_path).parent))
        client = ctypes.WinDLL(str(dll))
        bp = ctypes.WinDLL(str(bp_path))
        client.AposSdkInit.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        client.AposSdkInit.restype = ctypes.c_int32
        client.AposSdkDeInit.argtypes = [ctypes.c_int32]
        client.AposSdkDeInit.restype = ctypes.c_int32
        client.AposSdkSendEvent.argtypes = [ctypes.c_int32, ctypes.c_char_p,
                                            ctypes.c_void_p]
        client.AposSdkSendEvent.restype = ctypes.c_int32
        client.AposSdkRead.argtypes = [ctypes.c_int32, ctypes.c_void_p,
                                       ctypes.c_void_p, ctypes.c_int32]
        client.AposSdkRead.restype = ctypes.c_int32
        client.AposSdkSetEventSubscribe.argtypes = [
            ctypes.c_int32, EVENT_CB, ctypes.POINTER(ctypes.c_char_p),
            ctypes.c_int32]
        client.AposSdkSetEventSubscribe.restype = ctypes.c_int32
        bp.Init.restype = ctypes.c_void_p
        bp.SetInt32.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int32]
        bp.SetInt32.restype = ctypes.c_void_p
        bp.SetObject.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p]
        bp.SetObject.restype = ctypes.c_void_p
        bp.ToString.argtypes = [ctypes.c_void_p]
        bp.ToString.restype = ctypes.c_char_p
        self._api = (client, bp)
        return self._api

    def _on_event(self, _handle, topic, _length, data):
        """SDK 线程回调：只做解析和合并，不做任何 UI/阻塞操作。"""
        try:
            if not data:
                return
            name = topic.decode("utf-8", "replace") if topic else ""
            if "PropertyUpload" not in name:
                return
            with self._api_lock:
                raw = self._api[1].ToString(data)
                if not raw:
                    return
                text = raw.decode("utf-8", "replace")
                obj = json.loads(client_mod.repair_binary_json(text))
            if not isinstance(obj, dict):
                return
            now = time.time()
            with self._lock:
                for path, value in obj.items():
                    if path == "AposTimeStamp":
                        continue
                    self._values[path] = (value, now)
                self._last_event = now
        except Exception:
            # 回调里绝不能抛异常（会穿透到 SDK 的 C++ 线程）
            pass
