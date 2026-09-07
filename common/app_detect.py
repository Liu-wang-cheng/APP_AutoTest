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


def list_packages(device_id, third_party_only=True):
    """列出设备已安装包名(默认只取第三方应用)"""
    args = ["pm", "list", "packages"] + (["-3"] if third_party_only else [])
    out = _adb_shell(device_id, *args)
    return [l.strip()[len("package:"):] for l in out.splitlines()
            if l.strip().startswith("package:")]


def name_keywords(app_name):
    """APP 名称 → 匹配关键词;中文转拼音后取长度>=2 的连续窗口

    涂鸦智能 → tu ya zhi neng → {tuyazhineng, tuyazhi, tuya, yazhi, zhineng...}
    其中 "tuya" 能命中 com.tuya.smartiot
    """
    name = app_name.strip().lower()
    if not name:
        return []
    if re.fullmatch(r"[a-z0-9._\- ]+", name):  # 英文/包名片段直接用
        compact = name.replace(" ", "")
        return [k for k in {compact, compact.replace(".", "")} if len(k) >= 2]
    pys = lazy_pinyin(name)
    windows = set()
    n = len(pys)
    for i in range(n):
        for j in range(i + 2, n + 1):
            windows.add("".join(pys[i:j]))
    return sorted(k for k in windows if len(k) >= 2)


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
