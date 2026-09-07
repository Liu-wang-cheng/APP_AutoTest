import os
import re

import yaml

# 项目根目录:所有路径(配置/用例/图片/报告)都以此为基准,
# 不再依赖 pytest 的启动目录
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CONFIG_PATH = os.path.join(BASE_DIR, "config", "config.yaml")


def load_config():
    """加载统一配置"""
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def update_config(updates):
    """原位更新 config.yaml 的标量键,保留注释与格式(GUI 配置同步入口)

    updates 支持的键:
      'app.name' / 'app.package' / 'app.main_activity'  → 对应标量
      'device.name:<设备id>'                            → 设备列表项的 name(无则补)
    """
    with open(CONFIG_PATH, encoding="utf-8") as f:
        lines = f.readlines()
    for spec, value in updates.items():
        if spec.startswith("device.name:"):
            lines = _set_device_name(lines, spec.split(":", 1)[1], value)
        else:
            section, key = spec.split(".", 1)
            lines = _set_scalar(lines, section, key, value)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.writelines(lines)


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
        if re.match(r"^\s*list:\s*(#.*)?$", line.rstrip("\n")):
            insert_at = i + 1
            j = i + 1
            while j < len(lines) and lines[j].strip() and lines[j].startswith(" "):
                if lines[j].lstrip().startswith("- "):
                    insert_at = j + 1
                j += 1
            lines.insert(insert_at, f"    - id: {device_id}\n")
            lines.insert(insert_at + 1, f"      name: {val}\n")
            return lines
    return lines
