# -*- coding: utf-8 -*-
"""tools/ 下几个独立脚本的**真跑**守护(黑盒: 起子进程看输出, 不碰内部实现)。

为什么需要: 这些脚本不在主流程里, 坏了没人知道 —— 而它们比"报错"更坏的失效方式是
**静默空转**: `check_compat.py` 曾经只 listdir 顶层找 `Test_cases/*.yaml`, 而用例早已
按 APP 分组放成 `Test_cases/<组>/<用例>.yaml`, 于是它扫描 0 个文件却打印"全部兼容"
(实测 2026-10-10)。一个只会说 OK 的检查器比没有检查器更糟。
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = str(Path(__file__).resolve().parent.parent)


def _run_tool(*args, timeout=180, env=None):
    # ★ 显式按 UTF-8 解码: 工具入口都会把 stdout 切成 UTF-8(core.console), 而 text=True
    #   在本机(中文系统)会用 cp936 去解 —— 两边对不上就会解出乱码, 断言随之失真。
    return subprocess.run([sys.executable] + list(args), cwd=ROOT,
                          capture_output=True, timeout=timeout,
                          encoding="utf-8", errors="replace", env=env)


def test_check_compat_actually_scans_the_cases():
    """★ 必须扫到用例(递归进 APP 组目录), 不能空转报"全部兼容" """
    r = _run_tool("tools/check_compat.py")
    assert "扫描" in r.stdout, f"没跑起来:\n{r.stdout}\n{r.stderr}"
    m = re.search(r"扫描 (\d+) 个文件", r.stdout)
    assert m, r.stdout[-400:]
    assert int(m.group(1)) > 0, (
        "检查器扫到 0 个文件 —— 用例在 Test_cases/<APP组>/ 下, 必须递归进去;"
        f"空转报 OK 比不检查更糟\n{r.stdout[-400:]}")


def test_check_compat_reports_real_problems(tmp_path):
    """★ 反面对照: 给它一个**真有问题**的用例, 必须报出来(否则上面那条只是"能跑")"""
    bad = tmp_path / "探针.yaml"
    bad.write_text(
        "module: 探针\ncases:\n"
        "- name: 多动作探针\n  steps:\n"
        "  - desc: 一步写了两个动作\n    click: 某按钮\n    set_time: 12\n"
        "  - desc: 参数形态不对\n    room_click: 不是数字\n",
        encoding="utf-8", newline="\n")
    r = _run_tool("tools/check_compat.py", str(bad))
    assert "多动作共存" in r.stdout, r.stdout[-400:]
    assert "room_click 期望数字" in r.stdout, r.stdout[-400:]
    assert r.returncode == 1, "有问题时必须以非 0 退出(否则 CI/脚本里看不出差别)"


def test_check_compat_only_with_foreign_drive_path(tmp_path):
    """传一个**别的盘**上的用例路径不能崩(os.path.relpath 跨盘符会 ValueError)"""
    f = tmp_path / "单文件.yaml"
    f.write_text("module: 单\ncases:\n- name: a\n  steps:\n  - desc: 就一步\n"
                 "    click: 某按钮\n", encoding="utf-8", newline="\n")
    r = _run_tool("tools/check_compat.py", str(f))
    assert "Traceback" not in r.stderr, r.stderr[-400:]
    assert "扫描 1 个文件" in r.stdout, r.stdout[-300:]


@pytest.mark.parametrize("script", ["check_compat.py", "check_render.py",
                                    "verify_release.py",
                                    "verify_update_download.py",
                                    "verify_package.py", "release.py"])
def test_tool_scripts_import_cleanly(script):
    """脚本能被导入(语法/依赖没问题); --help 能出说明的顺带验一下"""
    mod = f"tools/{script}"
    src = open(os.path.join(ROOT, mod), encoding="utf-8").read()
    compile(src, mod, "exec")          # 语法
    r = _run_tool(mod, "--help")
    assert "Traceback" not in r.stderr, r.stderr[-400:]


def test_check_compat_errors_when_named_case_not_found():
    """★ 指名要查的用例找不到时**必须报错退出**。

    不然就是"扫描 0 个文件 + 全部兼容 + 返回 0" —— 一个只会说 OK 的检查器比没有更糟
    (这脚本刚因为组目录的事犯过一次同样的毛病)。
    """
    r = _run_tool("tools/check_compat.py", "根本没这个用例.yaml")
    assert r.returncode == 1, r.stdout[-300:]
    assert "没找到用例" in r.stdout, r.stdout[-300:]


def test_verify_package_skips_launch_when_static_fails():
    """静态检查没过就别再花几十秒去启动(还会铺出沙箱数据), 且要立刻返回非 0"""
    r = _run_tool("tools/verify_package.py", "不存在的产物.exe")
    assert r.returncode == 1, r.stdout[-300:]
    assert "不再启动" in r.stdout, r.stdout[-300:]
    assert "启动验证" not in r.stdout, "静态没过还是去启动了"


@pytest.mark.parametrize("script,args", [
    ("check_compat.py", []),
    ("verify_release.py", ["--help"]),
    ("verify_package.py", ["--help"]),
    ("verify_update_download.py", ["--help"]),
])
def test_tools_survive_non_chinese_console(script, args):
    """★ 英文 Windows 的控制台代码页是 cp1252, 打印中文直接 UnicodeEncodeError 崩掉
    —— **重定向到文件/管道时同样按 ANSI 代码页**, 所以 CI(windows-latest) 上所有会打印
    中文的工具都崩(实测 2026-10-10: CI #4 全红, 本地却完全正常)。

    用 PYTHONIOENCODING=cp1252 精确复现那个环境。工具入口靠 core.console.
    force_utf8_stdout() 把 stdout 切成 UTF-8 来兜底。
    """
    env = dict(os.environ, PYTHONIOENCODING="cp1252")
    r = _run_tool(f"tools/{script}", *args, env=env)
    out = (r.stdout or "") + (r.stderr or "")
    assert "UnicodeEncodeError" not in out, \
        f"{script} 在 cp1252 环境下编码崩了(工具入口少了 force_utf8_stdout?):\n{out[-500:]}"
    assert "Traceback" not in out, out[-500:]


def test_app_entry_also_guards_stdout_encoding():
    """应用入口(gui/main.py)同样要兜底: 用户把输出重定向走时不会因为中文崩"""
    src = open(os.path.join(ROOT, "gui", "main.py"), encoding="utf-8").read()
    assert "force_utf8_stdout" in src, "gui/main.py 没有做输出编码兜底"
    for script in ("check_compat.py", "release.py", "verify_package.py",
                   "verify_release.py", "verify_update_download.py",
                   "check_render.py"):
        text = open(os.path.join(ROOT, "tools", script), encoding="utf-8").read()
        assert "force_utf8_stdout" in text, f"tools/{script} 没有做输出编码兜底"
