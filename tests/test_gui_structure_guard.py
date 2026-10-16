# -*- coding: utf-8 -*-
"""结构守护: gui 模块拆分之后不许悄悄退化(结构整理 2026-10-15)。

背景: `gui/main_window.py` 曾经是 4568 行的单体(样式常量 + 十几个控件 + 各对话框 +
步骤卡片 + 后台线程 + 主窗口全挤在一起)。2026-10-15 按职责拆成
`gui/style.py` / `widgets.py` / `dialogs.py` / `step_card.py` / `threads.py`,
并在 main_window.py 末尾**显式 re-export**,好让 `main_window.X` 的老用法继续有效。

这三条守护分别防三件已经发生过的退化:
  ① 文件重新长回单体(拆了白拆);
  ② 新类只加在新模块里、忘了 re-export → 测试/工具里的 `main_window.X` 静默变 None;
  ③ 同一个类被复制到两个模块 → 界面用的是 A、测试改的是 B(改名/加参数后各改一半)。
"""
import ast
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

#: 拆分出来的兄弟模块(新增请同步这里)
SPLIT_MODULES = ("style", "widgets", "dialogs", "step_card", "threads")

#: main_window.py 的行数上限 —— 只想让它装 MainWindow 与 re-export
MAIN_WINDOW_MAX_LINES = 3000


def _top_level_names(path):
    """模块顶层定义的类/函数/常量名(不含 import 进来的)"""
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    out = set()
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)):
            out.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out.add(t.id)
    return out


def test_main_window_stays_under_line_budget():
    """★ main_window.py 不许再长回单体: 超了就说明该往兄弟模块里放"""
    p = ROOT / "gui" / "main_window.py"
    n = len(p.read_text(encoding="utf-8").splitlines())
    assert n <= MAIN_WINDOW_MAX_LINES, (
        f"gui/main_window.py 已经 {n} 行(上限 {MAIN_WINDOW_MAX_LINES}) —— "
        f"新东西请放进 gui/{'|'.join(SPLIT_MODULES)}.py, 别让主窗口再变回单体")


def test_moved_definitions_are_reexported():
    """★ 拆出去的类/常量必须仍能从 main_window 取到(`main_window.X` 用法不能失效)"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from gui import main_window as mw
    missing = []
    for mod in SPLIT_MODULES:
        names = _top_level_names(ROOT / "gui" / f"{mod}.py")
        for n in sorted(names):
            if n.startswith("__"):
                continue
            if not hasattr(mw, n):
                missing.append(f"{mod}.{n}")
    assert not missing, (
        "这些定义搬到了兄弟模块却忘了在 gui/main_window.py 末尾 re-export: "
        f"{missing}")


def test_no_definition_duplicated_across_gui_modules():
    """★ 同一个名字不许在多个 gui 模块里各定义一份(否则改一处漏一处)

    豁免: 全大写常量、以及 `log = get_logger()` 这类各模块各自持有的句柄。
    """
    EXEMPT = {"log"}
    seen = {}
    dup = []
    for mod in ("main_window",) + SPLIT_MODULES:
        for n in _top_level_names(ROOT / "gui" / f"{mod}.py"):
            if n.startswith("__") or n in EXEMPT:
                continue
            if n in seen and not n.isupper():
                dup.append((n, seen[n], mod))
            seen.setdefault(n, mod)
    assert not dup, f"这些名字在多个 gui 模块里重复定义: {dup}"


def test_no_star_imports_anywhere():
    """★ 全项目不许 `from x import *`。

    为什么单列一条守护(2026-10-16): 转发层(gui/main_window.py)与 core/session.py 里
    有个**只列"转出去的名字"**的 `__all__` —— 那是为了让静态检查(pyflakes)别把
    "为副作用而导入"的名字当成笔误(它不认 noqa, CI 的静态检查曾因此一直红)。
    代价是 `import *` 拿到的将不是"该模块的全部公开名"。本项目本来就一处都没用,
    这里把这条钉住: 谁想用 `import *`, 先把 `__all__` 补全。
    """
    bad = []
    for p in ROOT.rglob("*.py"):
        if ".venv" in p.parts or "dist" in p.parts or "build" in p.parts:
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8",
                                            errors="ignore").splitlines(), 1):
            s = line.strip()
            if s.startswith("from ") and s.endswith("import *"):
                bad.append(f"{p.relative_to(ROOT)}:{i}")
    assert not bad, f"这些地方用了 `import *`(会绕过 __all__ 之外的公开名): {bad}"
