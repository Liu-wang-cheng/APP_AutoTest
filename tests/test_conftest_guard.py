# -*- coding: utf-8 -*-
"""安全阀守护: --mode 非 real 时真机用例必须在收集阶段就被跳过(不碰真机)。

这是源项目踩过坑之后刻意设计的机制 —— 裸跑 pytest tests/ 绝不能误操控扫地机。
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_用例不能被重复收集():
    """同一个 module::case 只能收集一次。

    Test_cases/ 下若同时存在「汇总文件」和「拆分文件」(内容等价),两个都会被扫到,
    每个用例于是执行两遍 —— 报告里步骤数翻倍、两轮前置与截图交织,极难察觉。
    实测踩过: 快速建图跑了 2 遍(13 步 = 4 + 9),日志里出现两次「已进入 SE3L 设备页面」。
    """
    sys.path.insert(0, str(ROOT))
    from tests.test_yaml_runner import collect_cases
    cases = collect_cases()
    keys = [f"{m}::{n}" for m, n, *_ in cases]
    dup = sorted({k for k in keys if keys.count(k) > 1})
    assert not dup, f"这些用例被重复收集(检查 Test_cases/ 是否两套等价文件并存): {dup}"


def test_collect_cases_returns_list():
    """collect_cases 可被无设备调用,返回列表"""
    sys.path.insert(0, str(ROOT))
    from tests.test_yaml_runner import collect_cases
    cases = collect_cases()
    assert isinstance(cases, list)
    for module, name, steps, priority, wait, *_ in cases:
        assert isinstance(steps, list)
        assert priority in ("P0", "P1")


def test_yaml_runner_skipped_without_real_mode():
    """裸跑(mock 模式)时 test_yaml_runner 必须全部 skip,且不启动设备 fixture。

    这里跑的是真实执行而非 --collect-only: 后者不显示 skip 状态(用例没被 setup),
    验证不了安全阀。全 skip 时 pytest 不会执行 setup,所以 device fixture 不会启动,
    不会碰真机。
    """
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_yaml_runner.py",
         "-q", "-p", "no:cacheprovider", "-rs"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=300)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "skipped" in out.lower(), f"未看到 skip(安全阀失效?):\n{out}"
    # 不应有任何 passed/failed —— 真机用例必须一个都没跑
    assert " passed" not in out, out
    assert " failed" not in out, out


def test_每个BASE_DIR绑定都被隔离():
    """★ 结构性守护: 凡绑定 BASE_DIR 的模块都必须被钉到临时目录。

    事故(2026-09-24 审查发现): 项目里普遍写 `from core.driver import BASE_DIR`,
    这把名字绑定到**各自模块**; patch `core.driver.BASE_DIR` 对它们完全无效。
    tests/test_room_zones.py 就是这么以为隔离好了, 实际每跑一次单测都把假的分区
    标注图写进用户真实的 Test_img/debug/。

    这个测试让"漏隔离"在新增模块时**立刻被测出来**, 而不是等到发现用户数据被写脏。
    """
    import ast
    import importlib
    sys.path.insert(0, str(ROOT))
    from tests.conftest import BASE_DIR_MODULES
    from core.driver import BASE_DIR as REAL_ROOT

    # ① 静态扫描: 有没有模块绑定了 BASE_DIR 却不在隔离清单里
    found = set()
    for sub in ("core", "gui", "vlm"):
        base = ROOT / sub
        if not base.is_dir():
            continue
        for p in base.rglob("*.py"):
            tree = ast.parse(p.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (isinstance(node, ast.ImportFrom)
                        and node.module == "core.driver"
                        and any(a.name == "BASE_DIR" for a in node.names)):
                    found.add(".".join(p.relative_to(ROOT).with_suffix("").parts))
    missing = sorted(found - set(BASE_DIR_MODULES))
    assert not missing, (
        f"这些模块绑定了 BASE_DIR 却没进 tests/conftest.py 的 BASE_DIR_MODULES, "
        f"跑测试会往真实仓库写产物: {missing}")

    # ② 运行时: 清单里的模块确实被钉到临时目录(而不是仓库根)
    for name in BASE_DIR_MODULES:
        try:
            mod = importlib.import_module(name)
        except ImportError:
            continue                    # 可选依赖缺失
        val = getattr(mod, "BASE_DIR", None)
        if val is not None:
            assert not str(val).startswith(str(REAL_ROOT)), (
                f"{name}.BASE_DIR 仍指向真实仓库({val}), 隔离夹具没生效")

    # ③ vision.TEMPLATE_DIR 是模块级常量, patch BASE_DIR 对它无效 —— 必须单独钉
    import core.vision as vision
    if hasattr(vision, "TEMPLATE_DIR"):
        assert not str(vision.TEMPLATE_DIR).startswith(str(REAL_ROOT)), (
            "vision.TEMPLATE_DIR 未被隔离(它是 import 时就拼好的模块级常量)")
