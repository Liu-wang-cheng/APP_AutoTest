# -*- coding: utf-8 -*-
"""路径定位与配置读写。

BASE_DIR 不依赖 pytest 启动目录 —— GUI 与 pytest 两条入口共用这一套,
从哪个目录启动都能找到配置。
"""
import os
import re

import yaml

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "config", "config.yaml")

_ENCRYPTED_HEADER = b"%TSD-Header"


class YamlFileError(ValueError):
    pass


def load_yaml_file(path):
    """读取 YAML 文件;失败时抛出带文件名与原因说明的 YamlFileError

    裸 UnicodeDecodeError 既不说是哪个文件也不说为什么,排查成本很高,
    这里统一翻译成能直接定位问题的一句话。
    """
    name = os.path.basename(str(path))
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except UnicodeDecodeError:
        try:
            with open(path, "rb") as f:
                head = f.read(16)
        except OSError:
            head = b""
        if head.startswith(_ENCRYPTED_HEADER):
            raise YamlFileError(
                f"{name} 读取失败: 检测到企业透明加密文件头 (%TSD-Header-###%), "
                f"当前进程读到的是密文。处置: 用白名单进程(如 git)从仓库取回明文后重试"
            ) from None
        raise YamlFileError(f"{name} 读取失败: 文件不是 UTF-8 编码") from None
    except yaml.YAMLError as e:
        raise YamlFileError(f"{name} 解析失败: {e}") from None


def load_config():
    """加载统一配置"""
    return load_yaml_file(CONFIG_PATH)


def update_config(updates, path=None):
    """原位更新 config.yaml 的标量键,保留注释与格式(GUI 配置同步入口)

    updates 支持的键:
      'app.name' / 'app.package' / 'app.main_activity'  → app 段标量
      'target_device'                                   → 顶层标量(APP 内设备名称)
      'device.name:<设备id>'                            → 设备列表项的 name(无则补)
    path: 目标文件(默认 config/config.yaml),测试可指向临时文件
    """
    path = path or CONFIG_PATH
    # newline="" 必须显式给: 默认的通用换行模式会把 \r\n 读成 \n,写回时又按
    # os.linesep 把 \n 翻成 \r\n —— Windows 上每次回写都会把整个文件的 LF
    # 变成 CRLF,git diff 全文件变红。空串 = 读写都不做换行转换,原样保留。
    with open(path, encoding="utf-8", newline="") as f:
        lines = f.readlines()
    for spec, value in updates.items():
        if spec.startswith("device.name:"):
            lines = _set_device_name(lines, spec.split(":", 1)[1], value)
        elif "." in spec:
            section, key = spec.split(".", 1)
            lines = _set_scalar(lines, section, key, value)
        else:
            lines = _set_top_scalar(lines, spec, value)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.writelines(lines)


def _set_top_scalar(lines, key, value):
    """更新顶层零缩进标量(如 target_device),保留行内注释;不存在则追加末尾"""
    val = _fmt_scalar(value)
    key_re = re.compile(r"^(" + re.escape(key) + r":)([^\n#]*?)(\s+#.*)?$")
    for i, line in enumerate(lines):
        m = key_re.match(line.rstrip("\r\n"))
        if m:
            eol = "\r\n" if line.endswith("\r\n") else "\n"
            lines[i] = f"{m.group(1)} {val}{m.group(3) or ''}{eol}"
            return lines
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    lines.append(f"{key}: {val}\n")
    return lines


def _fmt_scalar(value):
    """标量转 YAML 文本,含特殊字符时加引号"""
    s = str(value)
    if re.search(r"[:#'\"]|^ |\s$|\n", s):
        s = '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def _set_scalar(lines, section, key, value):
    """在指定 section 下更新标量键,保留行内注释;不存在则插入 section 首位"""
    val = _fmt_scalar(value)
    in_sec = False
    key_re = re.compile(r"^(\s+)(" + re.escape(key) + r":)([^\n#]*?)(\s+#.*)?$")
    for i, line in enumerate(lines):
        m = re.match(r"^([^\s#][^:]*):", line)
        if m:  # 顶层键切换 section
            in_sec = (m.group(1) == section)
            continue
        if in_sec:
            m2 = key_re.match(line.rstrip("\r\n"))
            if m2:
                eol = "\r\n" if line.endswith("\r\n") else "\n"
                # 注释(含对齐空格)原样保留,只替换值本身 → 同值回写字节不变
                lines[i] = f"{m2.group(1)}{m2.group(2)} {val}{m2.group(4) or ''}{eol}"
                return lines
    for i, line in enumerate(lines):
        if re.match(rf"^{re.escape(section)}:\s*(#.*)?$", line.rstrip("\r\n")):
            lines.insert(i + 1, f"  {key}: {val}\n")
            return lines
    return lines  # section 不存在则放弃


def _set_device_name(lines, device_id, value):
    """更新设备列表项的 name;设备条目不存在则追加"""
    val = _fmt_scalar(value)
    id_re = re.compile(r"^(\s*-\s*id:\s*)" + re.escape(device_id) + r"\s*(#.*)?$")
    for i, line in enumerate(lines):
        if id_re.match(line.rstrip("\n")):
            indent = re.match(r"^(\s*)-\s", line).group(1) + "  "
            for j in range(i + 1, len(lines)):
                nxt = lines[j]
                if not nxt.startswith(indent) or not nxt.strip():
                    break
                m = re.match(r"^(\s*name:)([^\n]*)", nxt)
                if m:
                    lines[j] = f"{m.group(1)} {val}\n"
                    return lines
            lines.insert(i + 1, f"{indent}name: {val}\n")
            return lines
    for i, line in enumerate(lines):
        if re.match(r"^\s*list:\s*(#.*)?$", line.rstrip("\r\n")):
            # 追加到 list 块末尾(最后一个非空缩进行之后)
            j = i + 1
            last = i + 1
            while j < len(lines) and lines[j].strip() and lines[j].startswith(" "):
                j += 1
                last = j
            lines.insert(last, f"    - id: {device_id}\n")
            lines.insert(last + 1, f"      name: {val}\n")
            return lines
    return lines
