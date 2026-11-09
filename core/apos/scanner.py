# -*- coding: utf-8 -*-
"""机器人自动发现：复刻 AutopackDebugger 的 UDP 广播扫描，TCP 8801 兜底。

协议（2026-11-05 真机验证）：
  · 广播 x.x.x.255:5555，请求明文 "find ldrobot"
  · AES-128-CBC + PKCS7，结果 Base64
  · 监听 UDP 5550；响应 Base64 → AES-128-CBC（NoPadding，手动去填充）
  · JSON: {"value":[{"hostname","ip","mac","modle","sn","type"}]}
  · 显示名 = modle-sn-ip；响应里的 sn 即 CustomSN

★ AES key/IV 是厂商协议常量，只在本模块内使用，不写日志、不入文档。
★ 扫描只针对本机网卡所在 /24，且由用户操作触发。
"""
import base64
import ipaddress
import json
import locale
import re
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from core.logger import get_logger

log = get_logger()

_KEY = b"3J74PRUE5TKPJP32"
_IV = b"QB8GC2X6WK39FF93"
_DISCOVER_TEXT = "find ldrobot"
_SEND_PORT = 5555
_RECV_PORT = 5550
SDK_TCP_PORT = 8801

_scan_lock = threading.Lock()


def _encrypt_text(text):
    padder = padding.PKCS7(128).padder()
    data = padder.update(text.encode("utf-8")) + padder.finalize()
    enc = Cipher(algorithms.AES(_KEY), modes.CBC(_IV)).encryptor()
    raw = enc.update(data) + enc.finalize()
    return base64.b64encode(raw)


def _decrypt_text(text):
    raw = text.strip().encode("utf-8")
    raw += b"=" * (-len(raw) % 4)
    raw = base64.b64decode(raw)
    dec = Cipher(algorithms.AES(_KEY), modes.CBC(_IV)).decryptor()
    plain = dec.update(raw) + dec.finalize()
    # 厂商响应使用 PaddingMode.None，末尾是 PKCS7 填充或 NUL
    n = plain[-1] if plain else 0
    if 1 <= n <= 16 and plain[-n:] == bytes([n]) * n:
        plain = plain[:-n]
    return plain.decode("utf-8", "replace").rstrip("\x00 ")


def parse_response(raw):
    """解析一台机器人的响应 → [{model, sn, ip, mac, hostname, type}]。"""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        data = json.loads(raw)
    except Exception:
        return []
    values = data.get("value") if isinstance(data, dict) else None
    if not isinstance(values, list):
        return []
    out = []
    for item in values:
        if not isinstance(item, dict):
            continue
        ip = str(item.get("ip") or "").strip()
        if not ip:
            continue
        out.append({
            "model": str(item.get("modle") or "").strip(),   # 厂商拼写
            "sn": str(item.get("sn") or "").strip(),
            "ip": ip,
            "mac": str(item.get("mac") or "").strip(),
            "hostname": str(item.get("hostname") or "").strip(),
            "type": str(item.get("type") or "").strip(),
        })
    return out


def _decode_ipconfig():
    """读取 ipconfig 输出（中英文 Windows 都能解析）。"""
    try:
        raw = subprocess.run(["ipconfig"], capture_output=True, timeout=10).stdout
    except Exception:
        return ""
    for enc in (locale.getpreferredencoding(False), "gbk", "utf-8"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", "replace")


def _ipconfig():
    return _decode_ipconfig()


def local_ips():
    """本机 IPv4 地址（排除回环/链路本地/广播）。"""
    text = _ipconfig() or ""
    ips = re.findall(r"IPv4[^:：]*[:：]\s*([0-9]+(?:\.[0-9]+){3})", text, re.I)
    if not ips:
        try:
            ips = socket.gethostbyname_ex(socket.gethostname())[2]
        except Exception:
            ips = []
    out = []
    for ip in ips:
        try:
            addr = ipaddress.IPv4Address(ip)
        except ValueError:
            continue
        if addr.is_loopback or addr.is_link_local or addr.is_unspecified:
            continue
        if ip.startswith("255."):
            continue
        if ip not in out:
            out.append(ip)
    return out


def broadcast_targets():
    """每个本机 /24 网段的广播地址。"""
    targets = []
    for ip in local_ips():
        prefix = ".".join(ip.split(".")[:3])
        b = prefix + ".255"
        if b not in targets:
            targets.append(b)
    return targets


def local_cidrs():
    """本机所在的 /24 网段（TCP 兜底扫描用）。"""
    out = []
    for ip in local_ips():
        cidr = ".".join(ip.split(".")[:3]) + ".0/24"
        if cidr not in out:
            out.append(cidr)
    return out


def scan(timeout=4.0, cancel_event=None, targets=None, verify=True, keep_ips=None):
    """UDP 广播扫描，返回 [{model, sn, ip, ...}]，按 IP 排序。

    verify=True: 再探测一次 TCP 8801，只返回**此刻确实能连**的设备
    （用户 2026-11-06: 列表里不许出现连不上的 IP）。UDP 能应答不代表 SDK
    端口开着 —— 休眠/刚开机的机器人可能只回广播。

    keep_ips: 这些 IP 跳过探测直接保留（正在连的那台：SDK 已占用 8801，
    再探测可能被机器人拒绝，但它显然是可连接的）。

    端口被占用（例如 AutopackDebugger 正在扫描）会抛 OSError，由 UI 显示原因。
    """
    targets = targets or broadcast_targets()
    payload = _encrypt_text(_DISCOVER_TEXT)
    result = {}
    with _scan_lock:
        recv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        recv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            recv.bind(("", _RECV_PORT))
        except OSError as e:
            recv.close()
            raise OSError(f"UDP {_RECV_PORT} 被占用（AutopackDebugger 正在扫描？）: {e}")
        recv.settimeout(0.4)
        send = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        send.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            for target in targets:
                try:
                    send.sendto(payload, (target, _SEND_PORT))
                except OSError as e:
                    log.debug(f"[扫描] 广播 {target} 失败(忽略): {e}")
            deadline = time.time() + max(0.5, float(timeout))
            # ★ 广播本身会丢包(实测同一批机器人某次会少一两台): 1s/2.2s/3.4s
            #   各补发一次, 用多次广播覆盖漏收。
            resend_times = (1.0, 2.2, 3.4)
            resend_idx = 0
            start = time.time()
            while time.time() < deadline:
                if cancel_event is not None and cancel_event.is_set():
                    break
                if (resend_idx < len(resend_times)
                        and time.time() - start >= resend_times[resend_idx]):
                    for target in targets:
                        try:
                            send.sendto(payload, (target, _SEND_PORT))
                        except OSError:
                            pass
                    resend_idx += 1
                try:
                    raw, peer = recv.recvfrom(65535)
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    text = _decrypt_text(raw.decode("utf-8", "replace"))
                except Exception as e:
                    log.debug(f"[扫描] {peer[0]} 响应解密失败: {e}")
                    continue
                for item in parse_response(text):
                    result[item["ip"]] = item
        finally:
            send.close()
            recv.close()
    items = sorted(result.values(),
                   key=lambda x: tuple(int(p) for p in x["ip"].split(".")))
    if verify and items:
        # 只保留 SDK 端口(8801)此刻可连的设备: 能连才允许出现在 IP 列表里。
        # 机器人忙时首次探测可能超时，重试一次，避免把在线设备误过滤掉。
        keep = {str(ip).strip() for ip in (keep_ips or ()) if str(ip).strip()}
        udp_count = len(items)
        with ThreadPoolExecutor(max_workers=min(16, len(items))) as ex:
            checks = list(ex.map(
                lambda it: (it["ip"] in keep) or _probe_port_retry(it["ip"]),
                items))
        items = [it for it, ok in zip(items, checks) if ok]
        log.debug(f"[扫描] UDP 应答 {udp_count} 台, 8801 可连 {len(items)} 台")
    return items


def _probe_port_retry(ip, timeout=1.2, tries=2):
    for attempt in range(max(1, tries)):
        if _probe_port(ip, SDK_TCP_PORT, timeout):
            return True
        if attempt + 1 < tries:
            time.sleep(0.2)
    return False


def _probe_port(ip, port=SDK_TCP_PORT, timeout=0.3):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def tcp_fallback(cidr, reader, workers=64, timeout=0.3, cancel_event=None):
    """TCP 8801 探测 + reader(ip) 补全 model/sn。

    reader: 可调用对象，入参 ip，返回 {"model","sn","ip"} 或 None。
    """
    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return []
    hosts = list(net.hosts())[:512]
    opened = []
    with ThreadPoolExecutor(max_workers=min(workers, max(1, len(hosts)))) as ex:
        futs = {ex.submit(_probe_port, str(ip), SDK_TCP_PORT, timeout): str(ip)
                for ip in hosts}
        for fut in as_completed(futs):
            if cancel_event is not None and cancel_event.is_set():
                break
            try:
                if fut.result():
                    opened.append(futs[fut])
            except Exception:
                pass
    out = []
    for ip in sorted(opened, key=lambda s: tuple(int(p) for p in s.split("."))):
        try:
            info = reader(ip) or {}
        except Exception:
            info = {}
        out.append({"model": info.get("model", ""), "sn": info.get("sn", ""),
                    "ip": ip, "mac": info.get("mac", ""),
                    "hostname": "", "type": ""})
    return out
