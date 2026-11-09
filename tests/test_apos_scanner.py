# -*- coding: utf-8 -*-
"""机器人自动发现：加密/解析/网段/扫描（全部离线）。"""
import json
import socket

from core.apos import scanner


def test_parse_response_maps_vendor_typo():
    raw = ('{"value":[{"hostname":"AutoPack_Sweeper","ip":"192.168.10.63",'
           '"mac":"58:C5:87:6C:9D:D5","modle":"CLA_HS4",'
           '"sn":"B43A89CL800003A","type":"00000005"}]}')
    out = scanner.parse_response(raw)
    assert out == [{
        "model": "CLA_HS4", "sn": "B43A89CL800003A", "ip": "192.168.10.63",
        "mac": "58:C5:87:6C:9D:D5", "hostname": "AutoPack_Sweeper",
        "type": "00000005",
    }]


def test_parse_response_skips_bad_payload():
    assert scanner.parse_response("not json") == []
    assert scanner.parse_response('{"value": []}') == []
    assert scanner.parse_response('{"value": [{"sn": "x"}]}') == []


def test_encrypt_decrypt_roundtrip():
    text = json.dumps({"value": [{"ip": "192.168.1.2", "modle": "M",
                                  "sn": "S"}]})
    enc = scanner._encrypt_text(text).decode("ascii")
    assert scanner._decrypt_text(enc) == text


def test_broadcast_targets_from_ipconfig(monkeypatch):
    text = (
        "以太网适配器 以太网:\n"
        "   IPv4 地址 . . . . . . . . . . . . : 192.168.10.17\n"
        "   子网掩码  . . . . . . . . . . . . : 255.255.255.0\n"
        "WLAN:\n"
        "   IPv4 Address. . . . . . . . . . . : 10.1.2.3\n"
    )
    monkeypatch.setattr(scanner, "_ipconfig", lambda: text)
    assert scanner.local_ips() == ["192.168.10.17", "10.1.2.3"]
    assert scanner.broadcast_targets() == ["192.168.10.255", "10.1.2.255"]


def test_scan_decrypts_and_dedupes(monkeypatch):
    response = json.dumps({"value": [{"ip": "192.168.10.63",
                                      "modle": "CLA_HS4",
                                      "sn": "B43A89CL800003A"}]})
    payload = scanner._encrypt_text(response)

    class FakeSock:
        def __init__(self):
            self._sent = []
            self._sent_once = False

        def setsockopt(self, *a):
            pass

        def bind(self, *a):
            pass

        def settimeout(self, *a):
            pass

        def sendto(self, data, addr):
            self._sent.append((data, addr))

        def recvfrom(self, n):
            if not self._sent_once:
                self._sent_once = True
                return payload, ("192.168.10.63", 5555)
            raise socket.timeout()

        def close(self):
            pass

    monkeypatch.setattr(scanner, "broadcast_targets", lambda: ["192.168.10.255"])
    monkeypatch.setattr(scanner.socket, "socket", lambda *a: FakeSock())
    monkeypatch.setattr(scanner, "_probe_port", lambda *a, **k: True)
    out = scanner.scan(timeout=0.5)
    assert [x["ip"] for x in out] == ["192.168.10.63"]
    assert out[0]["model"] == "CLA_HS4"
    assert out[0]["sn"] == "B43A89CL800003A"


def test_scan_keeps_connected_ip_even_if_probe_fails(monkeypatch):
    """正在连接的那台即使探测失败也要保留(SDK 占用端口时探测可能被拒)。"""
    response = json.dumps({"value": [
        {"ip": "192.168.10.63", "modle": "CLA_HS4", "sn": "A"},
        {"ip": "192.168.10.243", "modle": "CLA_HS4", "sn": "B"},
    ]})
    payload = scanner._encrypt_text(response)

    class FakeSock:
        def __init__(self):
            self._sent_once = False

        def setsockopt(self, *a):
            pass

        def bind(self, *a):
            pass

        def settimeout(self, *a):
            pass

        def sendto(self, data, addr):
            pass

        def recvfrom(self, n):
            if not self._sent_once:
                self._sent_once = True
                return payload, ("192.168.10.63", 5555)
            raise socket.timeout()

        def close(self):
            pass

    monkeypatch.setattr(scanner, "broadcast_targets", lambda: ["192.168.10.255"])
    monkeypatch.setattr(scanner.socket, "socket", lambda *a: FakeSock())
    monkeypatch.setattr(scanner, "_probe_port", lambda *a, **k: False)
    out = scanner.scan(timeout=0.5, keep_ips={"192.168.10.243"})
    assert [x["ip"] for x in out] == ["192.168.10.243"]


def test_scan_filters_devices_without_sdk_port(monkeypatch):
    """UDP 能应答但 8801 连不上 → 不能出现在可连接列表里（用户 2026-11-06）。"""
    response = json.dumps({"value": [
        {"ip": "192.168.10.63", "modle": "CLA_HS4", "sn": "A"},
        {"ip": "192.168.10.216", "modle": "CLA_HS4", "sn": "B"},
    ]})
    payload = scanner._encrypt_text(response)

    class FakeSock:
        def __init__(self):
            self._sent_once = False

        def setsockopt(self, *a):
            pass

        def bind(self, *a):
            pass

        def settimeout(self, *a):
            pass

        def sendto(self, data, addr):
            pass

        def recvfrom(self, n):
            if not self._sent_once:
                self._sent_once = True
                return payload, ("192.168.10.63", 5555)
            raise socket.timeout()

        def close(self):
            pass

    monkeypatch.setattr(scanner, "broadcast_targets", lambda: ["192.168.10.255"])
    monkeypatch.setattr(scanner.socket, "socket", lambda *a: FakeSock())
    monkeypatch.setattr(scanner, "_probe_port",
                        lambda ip, *a, **k: ip == "192.168.10.63")
    out = scanner.scan(timeout=0.5)
    assert [x["ip"] for x in out] == ["192.168.10.63"]


def test_tcp_fallback_uses_reader(monkeypatch):
    monkeypatch.setattr(scanner, "_probe_port",
                        lambda ip, *a, **k: ip == "192.168.1.2")
    seen = []

    def reader(ip):
        seen.append(ip)
        return {"model": "M", "sn": "S"}

    out = scanner.tcp_fallback("192.168.1.0/30", reader)
    assert [x["ip"] for x in out] == ["192.168.1.2"]
    assert out[0]["model"] == "M" and out[0]["sn"] == "S"
    assert seen == ["192.168.1.2"]
