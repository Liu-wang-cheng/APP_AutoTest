# -*- coding: utf-8 -*-
"""APOS 客户端 / 采集线程（不加载真实 DLL）。"""
import json
import socket
import types

import pytest

from core.apos import client as client_mod
from core.apos.service import ParamService

#: 连接就绪探测会读 /System/CustomSN —— 假 SDK 必须能返回它才算"连上"
_READY = json.dumps({"/System/CustomSN": "SN"}).encode()
_ORIG_REACHABLE = client_mod.AposClient._reachable   # fixture 会替换类属性


@pytest.fixture(autouse=True)
def _skip_tcp_preflight(monkeypatch):
    """单测里的 IP 是假的, 跳过真实 TCP 预检。"""
    monkeypatch.setattr(client_mod.AposClient, "_reachable",
                        staticmethod(lambda *a, **k: True))


def test_reachable_checks_tcp_port():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    assert _ORIG_REACHABLE("127.0.0.1", port=port, tries=1)
    srv.close()
    assert not _ORIG_REACHABLE(
        "127.0.0.1", port=port, timeout=0.2, tries=1)


def _fake_sdk(tmp_path):
    for name in ("librpc_base.dll", "libapos_client.dll", "libzmq-x.dll"):
        (tmp_path / name).write_bytes(b"dll")
    (tmp_path / "license.dat").write_text("lic", encoding="utf-8")
    return tmp_path


def test_client_rejects_incomplete_sdk(tmp_path):
    try:
        client_mod.AposClient(tmp_path)
    except client_mod.AposError as e:
        assert "缺少" in str(e)
    else:
        raise AssertionError("不完整目录应抛 AposError")


def test_client_reads_json(tmp_path, monkeypatch):
    _fake_sdk(tmp_path)
    fake = types.SimpleNamespace(
        CppAposSdkInit=lambda *a: 7,
        CppAposSdkDeInit=lambda *a: 0,
        CppAposSdkRead=lambda *a: json.dumps(
            {"/BatteryBattery/BatteryLevel": 80, "/System/CustomSN": "SN",
             "AposTimeStamp": 1}).encode(),
        CppAposSdkService=lambda *a: b'{"ok": true}',
        CppAposSdkReadFile=lambda *a: b"log",
        get_log=lambda: b"",
    )
    monkeypatch.setattr(client_mod.ctypes, "WinDLL", lambda *a: fake)
    monkeypatch.setattr(client_mod.os, "add_dll_directory", lambda *a: None, raising=False)
    c = client_mod.AposClient(tmp_path)
    assert c.connect("192.168.1.2", retries=1) is True
    assert c.read(["/BatteryBattery/BatteryLevel"])["/BatteryBattery/BatteryLevel"] == 80
    assert c.service("/x") == {"ok": True}
    assert c.read_file("/tmp/a") == "log"
    c.disconnect()
    assert c.connected is False


def test_read_base_station_version_from_robot_file(tmp_path, monkeypatch):
    """基站版本在机器人文件里（与 AutopackDebugger 一致）。"""
    _fake_sdk(tmp_path)
    payloads = {"/tmp/base_station_version": "8.2.1.1"}

    def read_file(_handle, path):
        path = path.decode()
        value = payloads.get(path)
        if value is None:
            return b"{}"
        return json.dumps({"/System/FileStrOp": {"FilePath": path,
                                                 "FileData": value},
                           "AposTimeStamp": 1}).encode()

    fake = types.SimpleNamespace(
        CppAposSdkInit=lambda *a: 7,
        CppAposSdkDeInit=lambda *a: 0,
        CppAposSdkRead=lambda *a: _READY,
        CppAposSdkService=lambda *a: b"{}",
        CppAposSdkReadFile=read_file,
        get_log=lambda: b"",
    )
    monkeypatch.setattr(client_mod.ctypes, "WinDLL", lambda *a: fake)
    monkeypatch.setattr(client_mod.os, "add_dll_directory", lambda *a: None,
                        raising=False)
    c = client_mod.AposClient(tmp_path)
    assert c.connect("192.168.1.2", retries=1) is True
    assert c.read_base_station_version() == "8.2.1.1"
    c.disconnect()


def test_read_base_station_version_falls_back_to_typo_path(tmp_path, monkeypatch):
    """第一个文件为空时回退 /userdata/run/basestaion_version（厂商拼写）。"""
    _fake_sdk(tmp_path)
    payloads = {"/userdata/run/basestaion_version": "1.2.3"}

    def read_file(_handle, path):
        path = path.decode()
        value = payloads.get(path)
        if value is None:
            return b"{}"
        return json.dumps({"/System/FileStrOp": {"FileData": value}}).encode()

    fake = types.SimpleNamespace(
        CppAposSdkInit=lambda *a: 7,
        CppAposSdkDeInit=lambda *a: 0,
        CppAposSdkRead=lambda *a: _READY,
        CppAposSdkService=lambda *a: b"{}",
        CppAposSdkReadFile=read_file,
        get_log=lambda: b"",
    )
    monkeypatch.setattr(client_mod.ctypes, "WinDLL", lambda *a: fake)
    monkeypatch.setattr(client_mod.os, "add_dll_directory", lambda *a: None,
                        raising=False)
    c = client_mod.AposClient(tmp_path)
    assert c.connect("192.168.1.2", retries=1) is True
    assert c.read_base_station_version() == "1.2.3"
    c.disconnect()


def test_read_base_station_version_empty_when_files_missing(tmp_path, monkeypatch):
    _fake_sdk(tmp_path)
    fake = types.SimpleNamespace(
        CppAposSdkInit=lambda *a: 7,
        CppAposSdkDeInit=lambda *a: 0,
        CppAposSdkRead=lambda *a: _READY,
        CppAposSdkService=lambda *a: b"{}",
        CppAposSdkReadFile=lambda *a: b"{}",
        get_log=lambda: b"",
    )
    monkeypatch.setattr(client_mod.ctypes, "WinDLL", lambda *a: fake)
    monkeypatch.setattr(client_mod.os, "add_dll_directory", lambda *a: None,
                        raising=False)
    c = client_mod.AposClient(tmp_path)
    assert c.connect("192.168.1.2", retries=1) is True
    assert c.read_base_station_version(trigger=False) == ""
    c.disconnect()


def test_file_data_accepts_plain_text():
    assert client_mod.AposClient._file_data("8.2.1.1") == "8.2.1.1"
    assert client_mod.AposClient._file_data("{}") == ""
    assert client_mod.AposClient._file_data("") == ""


def test_read_base_station_version_retries(tmp_path, monkeypatch):
    """刚连上时文件可能还没就绪：按 retries 重试（切换 IP 后会用到）。"""
    _fake_sdk(tmp_path)
    calls = {"n": 0}

    def read_file(_handle, path):
        calls["n"] += 1
        if calls["n"] <= 2:          # 两个路径都先返回空
            return b"{}"
        return json.dumps({"/System/FileStrOp": {"FileData": "9.9.9"}}).encode()

    fake = types.SimpleNamespace(
        CppAposSdkInit=lambda *a: 7,
        CppAposSdkDeInit=lambda *a: 0,
        CppAposSdkRead=lambda *a: _READY,
        CppAposSdkService=lambda *a: b"{}",
        CppAposSdkReadFile=read_file,
        get_log=lambda: b"",
    )
    monkeypatch.setattr(client_mod.ctypes, "WinDLL", lambda *a: fake)
    monkeypatch.setattr(client_mod.os, "add_dll_directory", lambda *a: None,
                        raising=False)
    monkeypatch.setattr(client_mod.time, "sleep", lambda *_: None)
    c = client_mod.AposClient(tmp_path)
    assert c.connect("192.168.1.2", retries=1) is True
    assert c.read_base_station_version(retries=3) == "9.9.9"
    c.disconnect()


def test_read_base_station_version_triggers_robot_refresh(tmp_path, monkeypatch):
    """文件一开始为空 → 发 /TaskManager/GetBaseStationVersion，机器人写回后再读。"""
    _fake_sdk(tmp_path)
    state = {"ready": False}
    events = []

    def read_file(_handle, path):
        if not state["ready"]:
            return b"{}"
        return json.dumps({"/System/FileStrOp": {"FileData": "8.2.1.1"}}).encode()

    def send_event(_handle, event_id, _binpack):
        events.append(event_id.decode())
        state["ready"] = True       # 机器人处理事件后写好了文件
        return -1

    fake = types.SimpleNamespace(
        CppAposSdkInit=lambda *a: 7,
        CppAposSdkDeInit=lambda *a: 0,
        CppAposSdkRead=lambda *a: _READY,
        CppAposSdkService=lambda *a: b"{}",
        CppAposSdkReadFile=read_file,
        CppAposSdkSendEvent=send_event,
        get_log=lambda: b"",
    )
    monkeypatch.setattr(client_mod.ctypes, "WinDLL", lambda *a: fake)
    monkeypatch.setattr(client_mod.os, "add_dll_directory", lambda *a: None,
                        raising=False)
    monkeypatch.setattr(client_mod.time, "sleep", lambda *_: None)
    c = client_mod.AposClient(tmp_path)
    assert c.connect("192.168.1.2", retries=1) is True
    assert c.read_base_station_version(retries=2, delay=0.01) == "8.2.1.1"
    assert events == ["/TaskManager/GetBaseStationVersion"]
    c.disconnect()


class FakeClient:
    def __init__(self):
        self.connected = False
        self.calls = 0
        self.fail = False

    def connect(self, ip):
        self.connected = True
        return True

    def disconnect(self):
        self.connected = False

    def read(self, paths):
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")
        return {p: (80 if "BatteryLevel" in p else 1) for p in paths}


def _wait(cond, timeout=2.0):
    import time
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_service_polls_and_reads_battery():
    fc = FakeClient()
    svc = ParamService(fc, interval=0.05)
    assert svc.connect("1.2.3.4")
    assert _wait(lambda: svc.latest()["ok"].get("/BatteryBattery/BatteryLevel") == 80)
    assert svc.read_battery() == 80
    svc.set_paths(["/MotorFan/Speed"], interval=0.05)
    assert _wait(lambda: "/MotorFan/Speed" in svc.latest()["ok"])
    svc.stop()
    assert not (svc._thread and svc._thread.is_alive())
    svc.stop()                      # 幂等


def test_service_records_failures():
    fc = FakeClient()
    fc.fail = True
    svc = ParamService(fc, interval=0.05)
    svc.connect("1.2.3.4")
    assert _wait(lambda: bool(svc.latest()["failed"]))
    assert svc.read_battery() == -1
    svc.stop()


def test_service_read_once_empty_on_failure():
    fc = FakeClient()
    fc.fail = True
    svc = ParamService(fc, interval=1)
    assert svc.read_once(["/a"]) == {}
