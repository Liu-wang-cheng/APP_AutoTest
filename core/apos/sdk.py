# -*- coding: utf-8 -*-
"""AutopackDebugger SDK 目录管理：校验、复制、license 提取。

用户 2026-11-05 定的方式：首次选择 AutopackDebugger 目录，把 DLL 和 license
**复制到工具自己的 `BASE_DIR\\apos\\`**，之后不再依赖原目录。
- 只复制 3 个 DLL + license，不复制厂商代码；
- license 从 `AposSdkHelp.py` 的 `self.apos_license = "..."` 提取（正则，不执行）；
- OTA 更新只替换 exe + `_internal\\`，`apos\\` 会保留。
"""
import hashlib
import json
import re
import shutil
import time
from pathlib import Path

from core.driver import BASE_DIR

FILES = ("librpc_base.dll", "libapos_client.dll")
#: 可选依赖：属性上传流要用它构造请求/解析事件；缺了只影响曲线高频刷新
#: （退回批量读），不影响连接和表格参数。
OPTIONAL_FILES = ("BinpackCLR.dll",)
ZMQ_GLOB = "libzmq*.dll"
LICENSE_NAME = "license.dat"
MANIFEST = "sdk_manifest.json"


def default_sdk_dir():
    """工具自己的 SDK 目录（exe 同级 apos\\；源码运行时是仓库根 apos\\）"""
    return Path(BASE_DIR) / "apos"


def check_sdk(path, strict=True):
    """检查目录里 SDK 是否齐全。

    返回 {"ok": bool, "missing": [文件名], "path": str}
    strict=True 时 license 也算必需；strict=False 允许只检查 DLL。
    """
    p = Path(path)
    missing = []
    for name in FILES:
        if not (p / name).is_file():
            missing.append(name)
    if not any(p.glob(ZMQ_GLOB)):
        missing.append(ZMQ_GLOB)
    if strict and not (p / LICENSE_NAME).is_file():
        missing.append(LICENSE_NAME)
    return {"ok": not missing, "missing": missing, "path": str(p)}


def extract_license(source_dir):
    """从 AposSdkHelp.py 提取 license 字符串；不执行该文件。"""
    helper = Path(source_dir) / "AposSdkHelp.py"
    if not helper.is_file():
        raise FileNotFoundError("AposSdkHelp.py")
    text = helper.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r'self\.apos_license\s*=\s*["\']([^"\']+)["\']', text)
    if not m:
        raise ValueError("AposSdkHelp.py 里没有找到 apos_license")
    return m.group(1)


def binpack_path(sdk_dir=None):
    """BinpackCLR.dll 的位置；找不到返回 None。

    优先工具自己的 apos\\；老版本没有复制过该文件时，回退到
    `sdk_manifest.json` 里记录的厂商目录（只读使用，不执行厂商代码）。
    """
    p = Path(sdk_dir) if sdk_dir else default_sdk_dir()
    own = p / OPTIONAL_FILES[0]
    if own.is_file():
        return own
    try:
        manifest = json.loads((p / MANIFEST).read_text(encoding="utf-8"))
        src = Path(manifest.get("source", ""))
        if src.is_dir() and (src / OPTIONAL_FILES[0]).is_file():
            return src / OPTIONAL_FILES[0]
    except (OSError, ValueError):
        pass
    return None


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def install_sdk(source_dir, target_dir=None):
    """把 DLL + license 复制到工具目录。

    返回 check_sdk 的结果；源目录缺文件/复制失败会抛异常，由调用方显示原因。
    """
    src = Path(source_dir)
    dst = Path(target_dir) if target_dir else default_sdk_dir()
    dst.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        src_file = src / name
        if not src_file.is_file():
            raise FileNotFoundError(name)
        if src_file.resolve() != (dst / name).resolve():
            shutil.copy2(src_file, dst / name)
    zmq = sorted(src.glob(ZMQ_GLOB))
    if not zmq:
        raise FileNotFoundError(ZMQ_GLOB)
    if zmq[0].resolve() != (dst / zmq[0].name).resolve():
        shutil.copy2(zmq[0], dst / zmq[0].name)
    for name in OPTIONAL_FILES:
        optional = src / name
        if optional.is_file() and optional.resolve() != (dst / name).resolve():
            shutil.copy2(optional, dst / name)
    license_text = extract_license(src)
    (dst / LICENSE_NAME).write_text(license_text, encoding="utf-8")

    manifest = {
        "source": str(src.resolve()),
        "installed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "files": {p.name: _sha256(p) for p in
                  [dst / n for n in FILES] + [dst / zmq[0].name]
                  + [dst / n for n in OPTIONAL_FILES if (dst / n).is_file()]},
    }
    (dst / MANIFEST).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return check_sdk(dst)


def load_license(sdk_dir=None):
    """读取工具目录里的 license.dat；缺文件返回空串（连接时会给出明确错误）。"""
    p = Path(sdk_dir) if sdk_dir else default_sdk_dir()
    try:
        return (p / LICENSE_NAME).read_text(encoding="utf-8").strip()
    except OSError:
        return ""
