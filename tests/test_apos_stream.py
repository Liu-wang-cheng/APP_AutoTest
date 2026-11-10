# -*- coding: utf-8 -*-
"""属性上传流 / 参数服务合并推送值 / 坏路径缓存（不加载真实 DLL）。"""
import json
import time
import types

import pytest

from core.apos import client as client_mod
from core.apos import sdk as sdk_mod
from core.apos.service import ParamService
from core.apos.stream import UploadStream

_READY = json.dumps({"/System/CustomSN": "SN"}).encode()


@pytest.fixture(autouse=True)
def _skip_tcp_preflight(monkeypatch):
    monkeypatch.setattr(client_mod.AposClient, "_reachable",
                        staticmethod(lambda *a, **k: True))


# ── 上传流：构造/解析 ──
def test_stream_start_without_dll_returns_false(tmp_path):
    stream = UploadStream(tmp_path, license_text="lic")
    assert stream.start("192.168.1.2", ["/A"]) is False
    assert stream.running is False
    assert stream.values() == {}


def test_stream_start_without_license_returns_false(tmp_path):
    (tmp_path / "libapos_client.dll").write_bytes(b"dll")
    (tmp_path / "BinpackCLR.dll").write_bytes(b"dll")
    assert UploadStream(tmp_path, license_text="").start("1.2.3.4", ["/A"]) is False


def test_stream_parses_property_upload_event():
    stream = UploadStream()
    calls = []

    class _BP:
        @staticmethod
        def ToString(_data):
            return json.dumps({"/MotorFan/Speed": 1200,
                               "AposTimeStamp": 99}).encode()

    stream._api = (types.SimpleNamespace(), _BP)
    stream._on_event(1, b"/AposSystem/PropertyUpload", 0, 1)
    values = stream.values()
    assert values["/MotorFan/Speed"] == 1200
    assert "AposTimeStamp" not in values
    assert stream.last_event() > 0


def test_stream_ignores_other_topics_and_bad_payload():
    stream = UploadStream()

    class _BP:
        @staticmethod
        def ToString(_data):
            return b"not json"

    stream._api = (types.SimpleNamespace(), _BP)
    stream._on_event(1, b"/Log/KeyLog", 0, 1)
    assert stream.values() == {}
    stream._on_event(1, b"/AposSystem/PropertyUpload", 0, 1)  # 坏 JSON 不崩
    assert stream.values() == {}


# ── 参数服务：推送值优先/补漏 ──
class _FakeClient:
    def __init__(self, connect_ok=True):
        self.calls = 0
        self.connect_ok = connect_ok

    def connect(self, ip, retries=1, **kwargs):
        return self.connect_ok

    def disconnect(self):
        pass

    def read(self, paths):
        self.calls += 1
        return {p: 7 for p in paths}


class _FakeStream:
    def __init__(self, values, ts=123.0, running=True):
        self._values = values
        self._ts = ts
        self._running = running
        self.stopped = 0
        self.started = []

    @property
    def running(self):
        return self._running

    def start(self, ip, paths):
        self.started.append((ip, list(paths)))
        self._running = True
        return True

    def read(self, paths, timeout_ms=3000):
        return {p: 9 for p in paths}

    def values(self):
        return dict(self._values)

    def last_event(self):
        return self._ts

    def stop(self):
        self.stopped += 1
        self._running = False


def test_service_latest_merges_stream_values():
    stream = _FakeStream({"/KeyColliLeft/WorkState": 1,
                          "/KeyColliRight/WorkState": 0})
    svc = ParamService(_FakeClient(), ["/KeyColliLeft/WorkState"],
                       stream=stream, stream_paths=["/KeyColliLeft/WorkState"])
    snap = svc.latest()
    assert snap["ok"]["/KeyColliLeft/WorkState"] == 1
    # 未配置的路径也来自推送(曲线页会用到)
    assert snap["ok"]["/KeyColliRight/WorkState"] == 0
    assert snap["ts"] == 123.0


def test_service_latest_clears_failed_when_stream_has_value():
    stream = _FakeStream({"/A": 5}, ts=10.0)
    svc = ParamService(_FakeClient(), ["/A"], stream=stream)
    svc._snapshot = {"ok": {}, "failed": {"/A": "未返回"}, "ts": 1.0}
    snap = svc.latest()
    assert snap["ok"]["/A"] == 5
    assert "/A" not in snap["failed"]


def test_binary_path_json_is_repaired():
    """沿墙红外二进制属性是裸十六进制串，修复后才能解析且不拖累同批其它值。"""
    import json
    from core.apos.client import repair_binary_json
    path = "/IrAlongWall/AdcDifValue[MaxDetectionAdcChannelNum]"
    raw = '{"%s":0100020003000000,"/A":1}' % path
    data = json.loads(repair_binary_json(raw))
    assert data["/A"] == 1
    assert data[path] == "0100020003000000"


def test_stream_parse_repairs_binary_batch():
    """上传流同批里有二进制属性时，不能把整批 JSON 解析拖垮。"""
    from core.apos.stream import UploadStream
    path = "/IrAlongWall/AdcDifValue[MaxDetectionAdcChannelNum]"
    raw = ('{"%s":%s,"/A":1}' % (path, "ab" * 500)).encode()
    obj = UploadStream._parse(raw)
    assert obj["/A"] == 1
    assert obj[path] == "ab" * 500


def test_binary_param_display_summary():
    from core.apos import catalog
    path = "/IrAlongWall/AdcDifValue[MaxDetectionAdcChannelNum]"
    text = catalog.display_value({"path": path},
                                 {"ok": {path: "010002000300"}})
    assert "3 通道" in text and "1 2 3" in text


def test_failed_paths_ignore_unpushed_when_stream_on():
    """推流开启时未推送的路径不算读取失败（否则表格会到处飘红）。"""
    paths = ["/A", "/System/CustomSN"]
    failed = ParamService._failed_paths(paths, {"/A": 1}, stream_on=True)
    assert failed == {"/System/CustomSN": "未返回"}
    # 没有推送流时，读不到就是要报失败
    failed = ParamService._failed_paths(paths, {"/A": 1}, stream_on=False)
    assert failed == {"/System/CustomSN": "未返回"}
    failed = ParamService._failed_paths(["/A", "/B"], {}, stream_on=False)
    assert failed == {"/A": "未返回", "/B": "未返回"}


def test_service_connect_starts_stream_immediately():
    """连接成功立刻开流，之后的属性读都走流的句柄（两套 SDK 客户端不能同时用）。"""
    stream = _FakeStream({}, ts=0.0, running=False)
    svc = ParamService(_FakeClient(), ["/A"], stream=stream)
    assert svc.connect("192.168.1.2") is True
    assert stream.started and stream.started[0][0] == "192.168.1.2"
    svc.disconnect()
    assert stream.running is False


def test_service_read_once_prefers_pushed_values():
    """数值属性用推送值(读接口对数值返回 0, 不能当数据源)。"""
    stream = _FakeStream({"/A": 5}, ts=0.0)
    svc = ParamService(_FakeClient(), ["/A"], stream=stream)
    assert svc.read_once(["/A"]) == {"/A": 5}


def test_service_read_once_reads_system_paths_via_stream_handle():
    stream = _FakeStream({}, ts=0.0)
    svc = ParamService(_FakeClient(), ["/System/FirmwareInfo"], stream=stream)
    assert svc.read_once(["/System/FirmwareInfo"]) == {"/System/FirmwareInfo": 9}


def test_service_read_once_falls_back_to_client_without_stream():
    client = _FakeClient()
    svc = ParamService(client, ["/A"])
    assert svc.read_once(["/A"]) == {"/A": 7}   # 来自 client.read


def test_stream_read_decodes_partial_result():
    stream = UploadStream()
    stream._handle = 1

    class _BP:
        @staticmethod
        def Init():
            return object()

        @staticmethod
        def SetInt32(*a):
            pass

        @staticmethod
        def SetObject(*a):
            pass

        @staticmethod
        def ToString(_data):
            return json.dumps({"/System/CustomSN": "SN",
                               "AposTimeStamp": 5}).encode()

    class _Client:
        @staticmethod
        def AposSdkRead(*a):
            return -1          # 有路径不存在时返回 -1，但有效值仍在输出里

    stream._api = (_Client, _BP)
    assert stream.read(["/System/CustomSN", "/NotExist"]) == {
        "/System/CustomSN": "SN"}


def test_read_keeps_stream_alive_on_curve_paths():
    """曲线页只订曲线路径时也要在同一个句柄上读一次(否则机器人推送 35 秒后停)。"""
    from core.apos.service import READ_ONLY_PATHS
    stream = _FakeStream({}, ts=0.0)
    svc = ParamService(_FakeClient(), ["/MotorWheelL/Speed"], stream=stream)
    data = svc._read(["/MotorWheelL/Speed"])
    assert set(data) == set(READ_ONLY_PATHS)


def test_watchdog_restarts_stream_when_pushes_stale():
    class _StaleStream(_FakeStream):
        def __init__(self):
            super().__init__({}, ts=0.0)
            self.restarted = 0

        def last_event(self):
            return time.time() - 30

        def restart(self):
            self.restarted += 1
            return True

    stream = _StaleStream()
    svc = ParamService(_FakeClient(), ["/A"], stream=stream)
    svc._watch_stream()
    assert stream.restarted == 1


def test_watchdog_does_nothing_when_pushes_fresh():
    stream = _FakeStream({}, ts=time.time())
    svc = ParamService(_FakeClient(), ["/A"], stream=stream)
    svc._watch_stream()
    assert stream.stopped == 0


def test_service_disconnect_stops_stream():
    stream = _FakeStream({}, ts=0.0)

    class _C:
        def disconnect(self):
            pass

    svc = ParamService(_C(), ["/A"], stream=stream)
    svc.disconnect()
    assert stream.stopped == 1


# ── 客户端：机型不存在的路径只试一次 ──
def _fake_sdk_dir(tmp_path):
    for name in ("librpc_base.dll", "libapos_client.dll", "libzmq-x.dll"):
        (tmp_path / name).write_bytes(b"dll")
    (tmp_path / "license.dat").write_text("lic", encoding="utf-8")
    return tmp_path


def test_client_learns_missing_paths_and_skips_them(tmp_path, monkeypatch):
    _fake_sdk_dir(tmp_path)
    seen = []

    def raw_read(_handle, arr, count):
        paths = [arr[i].decode() for i in range(count)]
        seen.append(list(paths))
        if any("/NotExist" in p for p in paths):
            return b""
        return json.dumps({p: 1 for p in paths}).encode()

    fake = types.SimpleNamespace(
        CppAposSdkInit=lambda *a: 7,
        CppAposSdkDeInit=lambda *a: 0,
        CppAposSdkRead=raw_read,
        CppAposSdkService=lambda *a: b"{}",
        CppAposSdkReadFile=lambda *a: b"",
        get_log=lambda: b"",
    )
    monkeypatch.setattr(client_mod.ctypes, "WinDLL", lambda *a: fake)
    monkeypatch.setattr(client_mod.os, "add_dll_directory", lambda *a: None,
                        raising=False)
    c = client_mod.AposClient(tmp_path)
    assert c.connect("192.168.1.2", retries=1) is True

    data = c.read(["/A", "/NotExist", "/B"])
    assert data == {"/A": 1, "/B": 1}
    assert c.bad_paths() == set()          # 一次失败不拉黑(防抖动误判)

    data = c.read(["/A", "/NotExist", "/B"])
    assert data == {"/A": 1, "/B": 1}
    assert "/NotExist" in c.bad_paths()   # 连续两轮读不到才认定机型没有

    seen.clear()
    data = c.read(["/A", "/NotExist", "/B"])
    assert data == {"/A": 1, "/B": 1}
    # 之后不再把坏路径发给机器人
    assert all("/NotExist" not in batch for batch in seen)
    c.disconnect()


def test_binpack_path_falls_back_to_manifest_source(tmp_path):
    (tmp_path / "sdk_manifest.json").write_text(
        json.dumps({"source": str(tmp_path / "vendor")}), encoding="utf-8")
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    (vendor / "BinpackCLR.dll").write_bytes(b"dll")
    found = sdk_mod.binpack_path(tmp_path)
    assert found == vendor / "BinpackCLR.dll"


def test_sn_falls_back_to_robot_files():
    """CLA_ZY2L 属性里读不到 SN，要从 /oem/bin/sys_data/*.sn 文件补。"""
    import json as _json

    class FakeStream:
        running = True

        def read_file(self, path):
            if path == "/oem/bin/sys_data/custom.sn":
                # 机器人把文件末尾换行原样塞进 JSON, 整段不是合法 JSON
                return '{"\\/System\\/FileStrOp":{"FileData":"ZY2L0011\n"}}'
            if path == "/oem/bin/sys_data/sn":
                return _json.dumps({"/System/FileStrOp":
                                    {"FileData": "26420AN3105A00FA"}})
            return "{}"

    class _C:
        def read(self, paths):
            return {}

    svc = ParamService(_C(), ["/A"], stream=FakeStream())
    data = {}
    svc._fill_system_from_files(data, ["/System/CustomSN", "/System/BoardSN"])
    assert data["/System/CustomSN"] == "ZY2L0011"
    assert data["/System/BoardSN"] == "26420AN3105A00FA"


# ── 连接顺序：libapos 优先，失败才退回 librpc 轮询 ──
def test_connect_prefers_libapos_and_skips_librpc():
    class FakeClient:
        connected = False

        def __init__(self):
            self.calls = 0

        def connect(self, ip, retries=1, **kwargs):
            self.calls += 1
            self.connected = True
            return True

        def read(self, paths):
            return {p: 7 for p in paths}

        def disconnect(self):
            self.connected = False

    class FakeStream:
        running = False

        def start(self, ip, paths, preserve=False):
            self.running = True
            return True

        def stop(self, clear=True):
            self.running = False

        def read(self, paths, timeout_ms=3000):
            return {"/System/FirmwareInfo": '{"Model": "CLA_HS4"}'}

        def values(self):
            return {"/BatteryBattery/BatteryLevel": 90}

        def last_event(self):
            return time.time()

    fc = FakeClient()
    svc = ParamService(fc, ["/A"], stream=FakeStream(), stream_paths=["/A"])
    try:
        assert svc.connect("10.0.0.6") is True
        assert svc._stream_only is True
        assert fc.calls == 0                 # 流连上了就不再建 librpc 会话
        assert svc._model == "CLA_HS4"
    finally:
        svc.stop(timeout=1.0)


def test_connect_falls_back_to_librpc_when_stream_unavailable():
    class FakeClient:
        connected = False

        def __init__(self):
            self.calls = 0

        def connect(self, ip, retries=1, **kwargs):
            self.calls += 1
            self.connected = True
            return True

        def read(self, paths):
            return {p: 7 for p in paths}

        def disconnect(self):
            self.connected = False

    class FakeStream:
        running = False

        def start(self, ip, paths, preserve=False):
            return False

        def stop(self, clear=True):
            pass

    fc = FakeClient()
    svc = ParamService(fc, ["/A"], stream=FakeStream(), stream_paths=["/A"])
    assert svc.connect("10.0.0.7") is True
    assert fc.calls >= 1
    assert svc._stream_only is False
    svc.stop(timeout=1.0)


def test_connect_falls_back_to_libapos_stream_only():
    class FakeClient:
        connected = False
        last_error = "sdk_init"

        def connect(self, ip, retries=1, **kwargs):
            return False

        def read(self, paths):
            return {}

        def disconnect(self):
            pass

    class FakeStream:
        running = False

        def __init__(self):
            self.started_with = ""

        def start(self, ip, paths, preserve=False):
            self.started_with = ip
            self.running = True
            return True

        def stop(self, clear=True):
            self.running = False

        def read(self, paths, timeout_ms=3000):
            return {"/System/FirmwareInfo": '{"Model": "CLA_ZY2L"}'}

        def values(self):
            return {"/BatteryBattery/BatteryLevel": 88}

        def last_event(self):
            return time.time()

        def read_file(self, path):
            return ""

        def send_event(self, event_id):
            return 0

    st = FakeStream()
    svc = ParamService(FakeClient(), ["/A"], stream=st, stream_paths=["/A"])
    try:
        assert svc.connect("10.0.0.9") is True
        assert svc._stream_only is True
        assert st.started_with == "10.0.0.9"
        snap = svc.latest()
        assert snap["ok"]["/BatteryBattery/BatteryLevel"] == 88
        # 系统信息在同一个流句柄上读到了
        assert snap["ok"]["/System/FirmwareInfo"]
    finally:
        svc.stop(timeout=1.0)


# ── 基站版本：推流开着时走同一个句柄读文件 ──
def test_base_station_read_over_stream_keeps_running():
    """推流开着时不能走 librpc 文件读（会一直返回空），也不能停流。"""
    import json

    class FakeStream:
        running = True

        def __init__(self):
            self.events = []
            self.stopped = False

        def read_file(self, path):
            if path == "/tmp/base_station_version":
                return json.dumps({"/System/FileStrOp":
                                   {"FilePath": path, "FileData": "8.2.1.1"}})
            return ""

        def send_event(self, event_id):
            self.events.append(event_id)
            return 0

        def stop(self):
            self.stopped = True

    class _C:
        def read_base_station_version(self, retries=2, delay=1.0):
            return "WRONG"

        def read(self, paths):
            return {}

    st = FakeStream()
    svc = ParamService(_C(), ["/A"], stream=st)
    assert svc.read_base_station(retries=1, delay=0.01) == "8.2.1.1"
    assert not st.stopped            # 不能为了读文件把推流停掉
    assert st.events == []           # 文件已在, 不用触发刷新


def test_base_station_triggers_refresh_when_file_missing():
    class FakeStream:
        running = True

        def __init__(self):
            self.events = []

        def read_file(self, path):
            return ""

        def send_event(self, event_id):
            self.events.append(event_id)
            return 0

    class _C:
        def read(self, paths):
            return {}

    st = FakeStream()
    svc = ParamService(_C(), ["/A"], stream=st)
    assert svc.read_base_station(retries=1, delay=0.01) == ""
    assert st.events == ["/TaskManager/GetBaseStationVersion"]


# ── 采集线程生命周期（审查 2026-11-09） ──
def test_stop_keeps_stuck_thread_reference():
    """join 超时不能丢引用，否则下一次 start 会再开一个采集线程。"""
    class _C:
        def read(self, paths):
            return {}

    class Stuck:
        def is_alive(self):
            return True

        def join(self, timeout):
            pass

    svc = ParamService(_C(), ["/A"])
    stuck = Stuck()
    svc._thread = stuck
    svc.stop(timeout=0.01)
    assert svc._thread is stuck
    svc.start()                       # 旧线程还活着: 不许新建
    assert svc._thread is stuck


def test_empty_paths_still_does_keepalive_read():
    """只含 MCU 参数的曲线页 set_paths([]) 时，仍要在推送句柄上做保活读。"""
    from core.apos.service import READ_ONLY_PATHS

    class FakeStream:
        running = True

        def __init__(self):
            self.calls = []

        def read(self, paths, timeout_ms=3000):
            self.calls.append(list(paths))
            return {}

    class _C:
        def read(self, paths):
            return {}

    st = FakeStream()
    svc = ParamService(_C(), [], interval=0.05, stream=st)
    svc.set_paths([])
    svc._ready.set()
    svc.start()
    svc._wake.set()
    try:
        for _ in range(50):
            if st.calls:
                break
            time.sleep(0.02)
    finally:
        svc.stop(timeout=2.0)
    assert st.calls and st.calls[0] == list(READ_ONLY_PATHS)
