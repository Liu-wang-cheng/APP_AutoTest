# -*- coding: utf-8 -*-
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import core.registry as reg


def test_只导入runner就要注册全部动作():
    """只 import core.runner 时,动作表必须已经是满的。

    这个 bug 极其隐蔽: 注册表为空时 `_execute` 遍历空表,一个动作都不执行,
    但也不抛任何异常 —— `run_steps` 于是把每一步都记成 **PASS**。
    跑全量单测时看不出问题(某个测试文件顺带 import 了 core.actions 就补齐了注册),
    只有单独跑真机用例(`pytest tests/test_yaml_runner.py`)才会暴露:
    报告全绿,实际什么都没做。

    必须在**子进程**里验证 —— 当前进程早被别的测试导入过 core.actions 了。
    """
    code = ("import sys; sys.path.insert(0, r'%s');"
            "from core.runner import ActionRunner;"
            "from core import registry as reg;"
            "print(len(reg.ACTIONS))" % ROOT)
    out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, f"子进程失败: {out.stderr}"
    assert out.stdout.strip() == "25", (
        f"只导入 runner 时动作表有 {out.stdout.strip()} 个动作(应为 25) —— "
        f"core/runner.py 是不是漏了 `import core.actions`?")


@pytest.fixture(autouse=True)
def _clean():
    backup = dict(reg.ACTIONS)
    reg.ACTIONS.clear()
    yield
    reg.ACTIONS.clear()
    reg.ACTIONS.update(backup)


def test_register_and_priority_order():
    @reg.action("a", priority=50)
    def _a(r, s): ...

    @reg.action("b", priority=10)
    def _b(r, s): ...

    @reg.action("c", priority=90)
    def _c(r, s): ...

    order = [x.key for x in reg.dispatch_order()]
    assert order == ["b", "a", "c"]


def test_duplicate_key_raises():
    @reg.action("dup", priority=1)
    def _x(r, s): ...

    with pytest.raises(ValueError):
        @reg.action("dup", priority=2)
        def _y(r, s): ...


def test_fixed_bool_keys():
    @reg.action("back", priority=10, fixed_bool=True)
    def _b(r, s): ...

    @reg.action("swipe", priority=20)
    def _s(r, s): ...

    assert reg.fixed_bool_keys() == {"back"}
