# -*- coding: utf-8 -*-
"""设备解析单元测试：配置驱动的 adb connect / 同设备去重 / 备注名挂载

不依赖真实设备：monkeypatch 掉 subprocess.check_output,用假 adb 应答驱动。
运行: python -m pytest tests/test_device_resolve.py -v
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import app_detect


def _fake_adb(devices_out, id_map=None, connect_ok=True):
    """构造假的 subprocess.check_output

    devices_out: adb devices 的原始输出
    id_map:      {设备id: android_id},空串/缺失表示探测不到
    connect_ok:  adb connect 成功与否
    """
    calls = []

    def fake(cmd, *a, **kw):
        calls.append(list(cmd))
        if cmd[1] == "devices":
            return devices_out.encode()
        if cmd[1] == "connect":
            if not connect_ok:
                raise OSError("cannot connect to %s" % cmd[2])
            return b"connected to %s\n" % cmd[2].encode()
        if "android_id" in cmd:          # adb -s <id> shell settings get secure android_id
            return (id_map or {}).get(cmd[2], "").encode()
        return b""

    fake.calls = calls
    return fake


# ── host:port 识别 ──
class TestIsTcpAddr:

    def test_识别host_port形式(self):
        assert app_detect.is_tcp_addr("127.0.0.1:5555")
        assert app_detect.is_tcp_addr("192.168.1.7:62001")

    def test_非tcp形式(self):
        assert not app_detect.is_tcp_addr("emulator-5554")
        assert not app_detect.is_tcp_addr("")
        assert not app_detect.is_tcp_addr("127.0.0.1")


# ── 从配置取要连接的地址 ──
class TestConfiguredAddrs:

    def test_只取tcp形式并去重保序(self):
        cfg = {"device": {"default": "127.0.0.1:5555",
                          "list": [{"id": "127.0.0.1:5555", "name": "模拟器"},
                                   {"id": "emulator-5554", "name": "本地"},
                                   {"id": "127.0.0.1:62001"}]}}
        assert app_detect.configured_addrs(cfg) == ["127.0.0.1:5555", "127.0.0.1:62001"]

    def test_无device段返回空(self):
        assert app_detect.configured_addrs({}) == []


# ── 只连缺失的 ──
class TestEnsureConnected:

    def test_已在线的不重复connect(self, monkeypatch):
        fake = _fake_adb("List of devices attached\n127.0.0.1:5555\tdevice\n\n")
        monkeypatch.setattr("common.app_detect.subprocess.check_output", fake)
        cfg = {"device": {"default": "127.0.0.1:5555", "list": [{"id": "127.0.0.1:5555"}]}}
        assert app_detect.ensure_connected(cfg) == []
        assert not any(c[1] == "connect" for c in fake.calls)

    def test_离线的才connect(self, monkeypatch):
        fake = _fake_adb("List of devices attached\nemulator-5554\tdevice\n\n")
        monkeypatch.setattr("common.app_detect.subprocess.check_output", fake)
        cfg = {"device": {"default": "127.0.0.1:5555", "list": [{"id": "127.0.0.1:5555"}]}}
        assert app_detect.ensure_connected(cfg) == ["127.0.0.1:5555"]
        assert ["adb", "connect", "127.0.0.1:5555"] in fake.calls

    def test_connect失败不抛异常且不计入成功(self, monkeypatch):
        fake = _fake_adb("List of devices attached\n\n", connect_ok=False)
        monkeypatch.setattr("common.app_detect.subprocess.check_output", fake)
        cfg = {"device": {"default": "127.0.0.1:5555", "list": [{"id": "127.0.0.1:5555"}]}}
        assert app_detect.ensure_connected(cfg) == []


# ── 列表去重 + 备注名 ──
class TestResolveDevices:

    def _cfg(self):
        return {"device": {"default": "127.0.0.1:5555",
                           "list": [{"id": "127.0.0.1:5555", "name": "模拟器"}]}}

    def test_同设备双寻址去重且优先配置登记(self, monkeypatch):
        fake = _fake_adb(
            "List of devices attached\nemulator-5554\tdevice\n127.0.0.1:5555\tdevice\n\n",
            id_map={"emulator-5554": "b6bffdaffc260364",
                    "127.0.0.1:5555": "b6bffdaffc260364"})
        monkeypatch.setattr("common.app_detect.subprocess.check_output", fake)
        devs = app_detect.resolve_devices(self._cfg())
        assert [d["id"] for d in devs] == ["127.0.0.1:5555"]
        assert devs[0]["label"] == "模拟器 (127.0.0.1:5555)"

    def test_不同设备不去重(self, monkeypatch):
        fake = _fake_adb(
            "List of devices attached\nemulator-5554\tdevice\nemulator-5556\tdevice\n\n",
            id_map={"emulator-5554": "aaaa", "emulator-5556": "bbbb"})
        monkeypatch.setattr("common.app_detect.subprocess.check_output", fake)
        devs = app_detect.resolve_devices(self._curve_cfg())
        assert sorted(d["id"] for d in devs) == ["emulator-5554", "emulator-5556"]

    def test_拿不到标识时不误合并(self, monkeypatch):
        fake = _fake_adb(
            "List of devices attached\nemulator-5554\tdevice\nemulator-5556\tdevice\n\n",
            id_map={})
        monkeypatch.setattr("common.app_detect.subprocess.check_output", fake)
        devs = app_detect.resolve_devices(self._curve_cfg())
        assert sorted(d["id"] for d in devs) == ["emulator-5554", "emulator-5556"]

    def test_非device状态带后缀(self, monkeypatch):
        fake = _fake_adb("List of devices attached\nemulator-5554\toffline\n\n")
        monkeypatch.setattr("common.app_detect.subprocess.check_output", fake)
        devs = app_detect.resolve_devices({})
        assert devs[0]["label"] == "emulator-5554 [offline]"

    def test_无设备返回空列表(self, monkeypatch):
        fake = _fake_adb("List of devices attached\n\n")
        monkeypatch.setattr("common.app_detect.subprocess.check_output", fake)
        assert app_detect.resolve_devices({}) == []

    @staticmethod
    def _curve_cfg():
        """配置里没有登记这两台设备"""
        return {"device": {"default": "127.0.0.1:5555", "list": []}}
