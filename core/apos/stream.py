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
        #: 服务调用应答（/AposSystem/ServiceResponse → event_id → data）
        self._service_cond = threading.Condition()
        self._service_resp = {}
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
        # ★ 订阅路径先记下来: 初始化失败时也要保留, 否则看门狗后续的
        #   restart() 会因为"没有路径"直接放弃, 推送通道再也恢复不了
        #   (实测: 模拟 wifi_reset 后断流, 之后所有发事件都报"推流未就绪")。
        self._paths = list(paths)
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
            topics = [b"/AposSystem/PropertyUpload",
                      b"/AposSystem/ServiceResponse"]
            arr = (ctypes.c_char_p * len(topics))(*topics)
            if client.AposSdkSetEventSubscribe(handle, cb, arr, len(topics)) != 0:
                log.info("[上传流] 订阅 PropertyUpload 失败")
                # 失败也保留旧值: 看门狗马上会重试, 界面/采样不要闪成空值
                self.stop(clear=not preserve)
                self._paths = list(paths)   # 失败也保留, 供看门狗重试
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
            client.AposSdkSendEvent(handle, b"/AposSystem/PropertyUploadAdd", root)
        except Exception as e:
            log.info(f"[上传流] 启动失败: {e}")
            self.stop(clear=not preserve)
            self._paths = list(paths)       # 失败也保留, 供看门狗重试
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

    def read_file(self, path, timeout_ms=3000):
        """在推流句柄上读机器人文件（`/System/FileStrOp`），返回原始 JSON 文本。

        ★ 不能再走 librpc_base 的 CppAposSdkReadFile：推流一开，librpc 的文件读
          就返回空（实测），而且停流也恢复不了 —— 读基站版本时只能在这个
          句柄上读（厂商工具同样是用 binpack 读文件）。
        """
        if not self.running or not path:
            return ""
        with self._life:
            if not self.running:
                return ""
            client, bp = self._api
            try:
                with self._api_lock:      # CLR 对象不并发; 读调用本身不持锁
                    req = bp.Init()
                    sub = bp.Init()
                    bp.SetString(sub, b"FilePath", str(path).encode("utf-8"))
                    bp.SetObject(req, b"/System/FileStrOp", sub)
                    out = bp.Init()
                client.AposSdkRead(self._handle, req, out, int(timeout_ms))
                with self._api_lock:
                    raw = bp.ToString(out)
                return raw.decode("utf-8", "replace") if raw else ""
            except Exception as e:
                log.debug(f"[上传流] 读文件失败 {path}: {e}")
                return ""

    def send_event(self, event_id):
        """在推流句柄上发 APOS 事件（基站版本刷新等）。返回 SDK 返回值。"""
        if not self.running:
            return -1
        with self._api_lock:
            try:
                return int(self._api[0].AposSdkSendEvent(
                    self._handle, str(event_id).encode("utf-8"), 0))
            except Exception:
                return -1

    def send_binpack_event(self, event_id, params=None):
        """发带参数的事件（APOS 事件载荷就是 BinPack）。

        无参事件用 `send_event`；需要挡位/清扫类型这类参数时用这里：
        bool/int → INT32，float → FLOAT，其它 → STRING，全部挂在根节点。
        """
        if not self.running or not event_id:
            return -1
        with self._life:
            if not self.running:
                return -1
            client, bp = self._api
            try:
                # 构造 BinPack 时加 API 锁；发送放在锁外 —— SDK 收线程会回调
                # `_on_event`，持锁发送会和回调抢同一把锁(实测会卡住)。
                with self._api_lock:
                    root = bp.Init()
                    for key, value in (params or {}).items():
                        name = str(key).encode("utf-8")
                        if isinstance(value, bool):
                            bp.SetInt32(root, name, int(value))
                        elif isinstance(value, int):
                            bp.SetInt32(root, name, value)
                        elif isinstance(value, float):
                            bp.SetFloat(root, name, value)
                        else:
                            bp.SetString(root, name,
                                         str(value).encode("utf-8"))
                return int(client.AposSdkSendEvent(
                    self._handle, str(event_id).encode("utf-8"), root))
            except Exception as e:
                log.debug(f"[上传流] 发参数事件失败 {event_id}: {e}")
                return -1

    def call_service(self, func_id, params=None, timeout_ms=5000):
        """调用机器人 APOS 服务（ServiceRequest 协议），返回 {"rc", "data"}。

        协议来自机器人端 libapos_sdk_v2 的 AposSdkService 反编译（2026-11-26）：
          · 请求事件 `/AposSystem/ServiceRequest`
            载荷 `{"event_id": "/TaskControl/...", "data": {参数...}}`
          · 应答事件 `/AposSystem/ServiceResponse`（start() 里一起订阅）
        旧版 Windows SDK 的 `AposSdkService` 把服务名当普通事件发，机器人会
        回 iRet -1/-10；这里直接按新版协议走，不依赖新的 DLL。
        """
        if not self.running or not func_id:
            return {}
        key = str(func_id)
        with self._life:
            if not self.running:
                return {}
            client, bp = self._api
            try:
                with self._api_lock:
                    root = bp.Init()
                    bp.SetString(root, b"event_id", key.encode("utf-8"))
                    inner = bp.Init()
                    for k, v in (params or {}).items():
                        name = str(k).encode("utf-8")
                        if isinstance(v, bool):
                            bp.SetInt32(inner, name, int(v))
                        elif isinstance(v, int):
                            bp.SetInt32(inner, name, v)
                        elif isinstance(v, float):
                            bp.SetFloat(inner, name, v)
                        else:
                            bp.SetString(inner, name,
                                         str(v).encode("utf-8"))
                    bp.SetObject(root, b"data", inner)
                with self._service_cond:
                    self._service_resp.pop(key, None)
                rc = int(client.AposSdkSendEvent(
                    self._handle, b"/AposSystem/ServiceRequest", root))
                if rc < 0:
                    return {"rc": rc, "data": {}}
                with self._service_cond:
                    self._service_cond.wait_for(
                        lambda: key in self._service_resp,
                        timeout=max(0.1, int(timeout_ms) / 1000.0))
                    data = self._service_resp.pop(key, {})
                return {"rc": 0 if data else -1, "data": data}
            except Exception as e:
                log.debug(f"[上传流] 服务调用失败 {func_id}: {e}")
                return {}

    def call_service_native(self, func_id, params=None, timeout_ms=3000):
        """旧版 SDK 的 AposSdkService（只用于对照排查，机器人会拒绝）。"""
        if not self.running or not func_id:
            return {}
        with self._life:
            if not self.running:
                return {}
            client, bp = self._api
            try:
                with self._api_lock:
                    req = bp.Init()
                    for key, value in (params or {}).items():
                        name = str(key).encode("utf-8")
                        if isinstance(value, bool):
                            bp.SetInt32(req, name, int(value))
                        elif isinstance(value, int):
                            bp.SetInt32(req, name, value)
                        elif isinstance(value, float):
                            bp.SetFloat(req, name, value)
                        else:
                            bp.SetString(req, name,
                                         str(value).encode("utf-8"))
                    out = bp.Init()
                rc = int(client.AposSdkService(
                    self._handle, str(func_id).encode("utf-8"), req, out,
                    int(timeout_ms)))
                with self._api_lock:
                    raw = bp.ToString(out)
                data = {}
                if raw:
                    text = raw.decode("utf-8", "replace")
                    try:
                        data = json.loads(client_mod.repair_binary_json(text))
                    except Exception:
                        data = {"raw": text}
                return {"rc": rc, "data": data}
            except Exception as e:
                log.debug(f"[上传流] native 服务调用失败 {func_id}: {e}")
                return {}

    def write_props(self, items, timeout_ms=3000):
        """批量写属性（`AposSdkWrite` + BinPack 树）。

        items: `[(path, value, kind)]`，kind ∈ uint8/int8/uint16/int16/
        int32/float/string。设备按字段类型解析，类型不对会被拒：
        例如滚筒伸缩电机 `WorkModeCtl` 必须 uint8、`PwmOrLevelCtl`
        必须 int16（反编译 AutopackDebugger 的
        `Event.MotorDrumTelescopic.SetPWM` 得到，真机验证）。
        返回 SDK 原始返回值（>=0 视为已下发）。
        """
        if not self.running or not items:
            return -1
        with self._life:
            if not self.running:
                return -1
            client, bp = self._api
            setters = {
                "uint8": bp.SetUint8, "int8": bp.SetInt8,
                "uint16": bp.SetUint16, "int16": bp.SetInt16,
                "int32": bp.SetInt32, "float": bp.SetFloat,
                "string": bp.SetString,
            }
            try:
                with self._api_lock:
                    root = bp.Init()
                    for path, value, kind in items:
                        sub = bp.Init()
                        setters[kind](sub, b"data", value)
                        bp.SetObject(root, str(path).encode("utf-8"), sub)
                return int(client.AposSdkWrite(self._handle, root,
                                               int(timeout_ms)))
            except Exception as e:
                log.debug(f"[上传流] 写属性失败: {e}")
                return -1

    def send_json_event(self, topic, json_text):
        """以 JSON 载荷发事件（厂商 `CppAposSdkSentEventByJson` 同款）。

        机器人把 JSON 放进 BinPack 的 `JsonParam` 字段：服务类接口
        （如 `/Fan/SetLevel`）就是这么调用的。
        """
        if not self.running or not topic:
            return -1
        with self._life:
            if not self.running:
                return -1
            client, bp = self._api
            try:
                with self._api_lock:
                    root = bp.Init()
                    bp.SetString(root, b"JsonParam",
                                 str(json_text).encode("utf-8"))
                return int(client.AposSdkSendEvent(
                    self._handle, str(topic).encode("utf-8"), root))
            except Exception as e:
                log.debug(f"[上传流] 发 JSON 事件失败 {topic}: {e}")
                return -1

    def send_set_cmd(self, cmd, params=None):
        """发 `/Common/SetCmd` 通用命令（机器人 AposService 的 common_* 接口）。

        与 PostEvent 同一入口，`Cmd` 换成具体方法名，例如 `fan_set_level`、
        `water_pump_set_level`、`task_set_cleanmode`。
        """
        if not self.running or not cmd:
            return -1
        with self._life:
            if not self.running:
                return -1
            client, bp = self._api
            try:
                with self._api_lock:
                    req = bp.Init()
                    bp.SetString(req, b"Cmd", str(cmd).encode("utf-8"))
                    inner = bp.Init()
                    for k, v in (params or {}).items():
                        name = str(k).encode("utf-8")
                        if isinstance(v, bool):
                            bp.SetInt32(inner, name, int(v))
                        elif isinstance(v, int):
                            bp.SetInt32(inner, name, v)
                        elif isinstance(v, float):
                            bp.SetFloat(inner, name, v)
                        else:
                            bp.SetString(inner, name, str(v).encode("utf-8"))
                    bp.SetObject(req, b"Params", inner)
                return int(client.AposSdkSendEvent(
                    self._handle, b"/Common/SetCmd", req))
            except Exception as e:
                log.debug(f"[上传流] 发 SetCmd 失败 {cmd}: {e}")
                return -1

    def post_event(self, topic, event, params=None):
        """发一个机器人内部事件（厂商 `/Common/SetCmd` + `PostEvent`）。

        故障模拟用的就是这条通道（AutopackDebugger 的模拟按钮同款）:
          `{Cmd:"PostEvent", Params:{Topic, Event, Params:{}}}`
        返回 SDK 返回值（<0 视为失败）。
        """
        if not self.running:
            return -1
        with self._life:
            if not self.running:
                return -1
            client, bp = self._api
            try:
                with self._api_lock:
                    req = bp.Init()
                    bp.SetString(req, b"Cmd", b"PostEvent")
                    p = bp.Init()
                    bp.SetString(p, b"Topic", str(topic).encode("utf-8"))
                    bp.SetString(p, b"Event", str(event).encode("utf-8"))
                    inner = bp.Init()
                    for k, v in (params or {}).items():
                        name = str(k).encode("utf-8")
                        # 参数保持类型: 挡位这类字段机器人按 INT32 读, 传字符串
                        # 会被解析成 0(实测)。
                        if isinstance(v, bool):
                            bp.SetInt32(inner, name, int(v))
                        elif isinstance(v, int):
                            bp.SetInt32(inner, name, v)
                        elif isinstance(v, float):
                            bp.SetFloat(inner, name, v)
                        else:
                            bp.SetString(inner, name, str(v).encode("utf-8"))
                    bp.SetObject(p, b"Params", inner)
                    bp.SetObject(req, b"Params", p)
                return int(client.AposSdkSendEvent(
                    self._handle, b"/Common/SetCmd", req))
            except Exception as e:
                log.debug(f"[上传流] 发事件失败 {topic}/{event}: {e}")
                return -1

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
        # 属性写: AposSdkWrite(handle, binpack, timeout_ms)（厂商
        # Event.MotorDrumTelescopic.SetPWM 等就是这条路）
        client.AposSdkWrite.argtypes = [ctypes.c_int32, ctypes.c_void_p,
                                        ctypes.c_int32]
        client.AposSdkWrite.restype = ctypes.c_int32
        # 通用服务调用 AposSdkService(handle, func_id, req, resp, timeout)
        # 反汇编确认的签名（2026-11-26）：rcx=handle, rdx=func_id(c_char*),
        #   r8=req(binpack), r9=resp(binpack), [rsp+0x70]=timeout_ms
        client.AposSdkService.argtypes = [ctypes.c_int32, ctypes.c_char_p,
                                          ctypes.c_void_p, ctypes.c_void_p,
                                          ctypes.c_int32]
        client.AposSdkService.restype = ctypes.c_int32
        bp.Init.restype = ctypes.c_void_p
        bp.SetInt8.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int8]
        bp.SetInt8.restype = ctypes.c_void_p
        bp.SetUint8.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                ctypes.c_uint8]
        bp.SetUint8.restype = ctypes.c_void_p
        bp.SetInt16.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                ctypes.c_int16]
        bp.SetInt16.restype = ctypes.c_void_p
        bp.SetUint16.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                 ctypes.c_uint16]
        bp.SetUint16.restype = ctypes.c_void_p
        bp.SetInt32.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int32]
        bp.SetInt32.restype = ctypes.c_void_p
        bp.SetString.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                 ctypes.c_char_p]
        bp.SetString.restype = ctypes.c_void_p
        bp.SetFloat.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_float]
        bp.SetFloat.restype = ctypes.c_void_p
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
            if "ServiceResponse" in name:
                with self._api_lock:
                    raw = self._api[1].ToString(data)
                    if not raw:
                        return
                    text = raw.decode("utf-8", "replace")
                    obj = json.loads(client_mod.repair_binary_json(text))
                if not isinstance(obj, dict):
                    return
                eid = str(obj.get("event_id") or obj.get("EventId") or "")
                payload = obj.get("data", obj)
                with self._service_cond:
                    self._service_resp[eid] = payload
                    self._service_cond.notify_all()
                return
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
