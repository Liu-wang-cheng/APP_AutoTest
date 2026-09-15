"""APP 与设备自动检测(基于 adb 命令,不依赖 uiautomator2 连接,轻量快速)"""
import re
import subprocess

try:
    from pypinyin import lazy_pinyin
except ImportError:  # 未安装时退化为仅原名匹配(ASCII 名称不受影响)
    def lazy_pinyin(s):
        return [s]


def _adb_shell(device_id, *args, timeout=8):
    cmd = ["adb", "-s", device_id, "shell", *args]
    out = subprocess.check_output(cmd, timeout=timeout)
    return out.decode("utf-8", errors="replace")


def list_devices():
    """adb devices → [{'id':..., 'state':'device'|'offline'|...}] 含真机与模拟器"""
    out = subprocess.check_output(["adb", "devices"], timeout=5).decode("utf-8", errors="replace")
    devices = []
    for line in out.splitlines()[1:]:
        parts = line.strip().split()
        if len(parts) >= 2:
            devices.append({"id": parts[0], "state": parts[1]})
    return devices


_TCP_ADDR_RE = re.compile(r"^[A-Za-z0-9._\-]+:\d+$")


def is_tcp_addr(device_id):
    """'127.0.0.1:5555' → True;'emulator-5554' → False"""
    return bool(_TCP_ADDR_RE.match(device_id or ""))


def configured_addrs(cfg):
    """配置里 host:port 形式的设备地址(device.default + device.list[].id),去重保序

    模拟器常常只能通过 TCP 寻址(需先 adb connect),而 adb 不会自动列出,
    这些地址就是要主动补连的目标。
    """
    device_cfg = cfg.get("device") or {}
    candidates = [device_cfg.get("default", "")]
    candidates += [d.get("id", "") for d in device_cfg.get("list") or []]
    addrs = []
    for did in candidates:
        if is_tcp_addr(did) and did not in addrs:
            addrs.append(did)
    return addrs


def _device_key(device_id):
    """同设备的去重键: android_id;探测不到返回 None

    不能用 get-serialno —— 它对 TCP 设备返回的就是 ip:port 本身;
    ro.serialno 在模拟器上又常为空。只有 android_id 跨 transport 一致。
    """
    try:
        return _adb_shell(device_id, "settings", "get", "secure", "android_id",
                          timeout=3).strip() or None
    except Exception:
        return None


def ensure_connected(cfg, timeout=3):
    """把配置里登记、但当前不在线的 host:port 地址 connect 上

    返回本次连接成功的地址。失败静默跳过(模拟器没开是常态)。
    已在线的不重复 connect。
    """
    online = {d["id"] for d in list_devices()}
    connected = []
    for addr in configured_addrs(cfg):
        if addr in online:
            continue
        try:
            subprocess.check_output(["adb", "connect", addr], timeout=timeout)
            connected.append(addr)
        except Exception:
            continue
    return connected


def resolve_devices(cfg):
    """可展示的设备列表 [{'id','state','name','label'}]

    先补齐配置里登记的 TCP 设备,再按 android_id 去重(同一台模拟器经
    emulator-XXXX 与 127.0.0.1:YYYY 两种寻址只留一条),优先保留配置里
    登记过的 id,最后挂上配置里的备注名。
    """
    ensure_connected(cfg)
    device_cfg = cfg.get("device") or {}
    entries = device_cfg.get("list") or []
    names = {d.get("id", ""): d.get("name", "") for d in entries}
    registered = [d.get("id", "") for d in entries]
    default = device_cfg.get("default", "")

    def priority(did):
        if did in registered:
            return 0
        if did == default:
            return 1
        return 2

    merged = {}
    for dev in list_devices():
        did = dev["id"]
        key = _device_key(did) or did      # 探测不到标识 → 按 id 独立,不误合并
        cur = merged.get(key)
        if cur is None or priority(did) < priority(cur["id"]):
            merged[key] = dev

    result = []
    for dev in merged.values():
        did = dev["id"]
        name = names.get(did, "")
        label = f"{name} ({did})" if name else did
        if dev["state"] != "device":
            label += f" [{dev['state']}]"
        result.append({"id": did, "state": dev["state"], "name": name, "label": label})
    return result


def list_packages(device_id, third_party_only=True):
    """列出设备已安装包名(默认只取第三方应用)"""
    args = ["pm", "list", "packages"] + (["-3"] if third_party_only else [])
    out = _adb_shell(device_id, *args)
    return [l.strip()[len("package:"):] for l in out.splitlines()
            if l.strip().startswith("package:")]


# 显示名与包名毫无字面关系的 APP:名称(小写去空格) → 额外关键词
# SmartThings 的包名是 com.samsung.android.oneconnect(OneConnect 是它的内部名),
# 靠名称切词切不出任何共同子串,只能在这里登记
_APP_ALIASES = {
    "smartthings": ("oneconnect",),
}


def name_keywords(app_name):
    """APP 名称 → 匹配关键词;中文转拼音后取长度>=2 的连续窗口

    涂鸦智能 → tu ya zhi neng → {tuyazhineng, tuyazhi, tuya, yazhi, zhineng...}
    其中 "tuya" 能命中 com.tuya.smartiot

    显示名与包名对不上的 APP(如 SmartThings)由 _APP_ALIASES 补关键词。
    """
    name = app_name.strip().lower()
    if not name:
        return []
    if re.fullmatch(r"[a-z0-9._\- ]+", name):  # 英文/包名片段直接用
        compact = name.replace(" ", "")
        kws = {compact, compact.replace(".", "")}
    else:
        pys = lazy_pinyin(name)
        kws = set()
        n = len(pys)
        for i in range(n):
            for j in range(i + 2, n + 1):
                kws.add("".join(pys[i:j]))
    kws.update(_APP_ALIASES.get(name.replace(" ", ""), ()))
    return sorted(k for k in kws if len(k) >= 2)


def match_packages(app_name, packages):
    """按关键词对包名打分,返回 [(package, 分数)] 降序;分数=命中的最长关键词"""
    kws = name_keywords(app_name)
    if not kws:
        return []
    scored = []
    for pkg in packages:
        low = pkg.lower()
        best = max((len(k) for k in kws if k in low), default=0)
        if best:
            scored.append((pkg, best))
    # 同分时取更短的包名(更接近厂商根包,如 com.tuya.smartiot 优先于
    # com.example.tuya.helper 这类内嵌关键词的包)
    scored.sort(key=lambda x: (-x[1], len(x[0]), x[0]))
    return scored


def _parse_brief_activity(text, package):
    """解析 resolve-activity --brief 输出中的 '包名/活动' 行,返回活动名或 None"""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(package + "/"):
            return line.split("/", 1)[1].strip()
    return None


def detect_main_activity(device_id, package):
    """检测 LAUNCHER 启动页: resolve-activity 优先,dumpsys 兜底,失败返回 None"""
    try:
        out = _adb_shell(device_id, "cmd", "package", "resolve-activity", "--brief",
                         "-c", "android.intent.category.LAUNCHER", package)
        activity = _parse_brief_activity(out, package)
        if activity:
            return activity
    except Exception:
        pass
    try:
        out = _adb_shell(device_id, "dumpsys", "package", package)
        m = re.search(re.escape(package) + r"/([A-Za-z0-9_.$]+)", out)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None
