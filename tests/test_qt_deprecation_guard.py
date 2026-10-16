# -*- coding: utf-8 -*-
"""Qt 弃用 API 的守护(2026-10-16)。

起因: CI 的测试日志里攒了 **26 条**同一类警告 ——

    DeprecationWarning: Function: 'QMouseEvent.pos() const' is marked as deprecated

全是鼠标坐标用了 Qt5 时代的 `event.pos()`。它把小信号全盖住了(那一轮总共 27 条警告,
真问题只有 1 条), 而且 PySide6 以后真删掉这个方法时会直接崩在按钮上。

Qt6 的正确写法: `event.position()`(QPointF) → 需要整数坐标再 `.toPoint()`。
项目里统一走 `gui.widgets.mouse_xy(ev)`, 所以这里只守一条: **gui/ 的代码里不许再出现 `ev.pos()`**
(注释/文档字符串里解释"为什么不用它"不算 —— 见下面的 tokenize 过滤)。
"""
import os
import re
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 事件参数的老名字(代码里的约定俗成): event / ev / e
_EVENT_POS = re.compile(r"\b(event|ev|e)\.pos\(\)")


def _code_line_numbers(path):
    """这一行里有没有**真正的代码**(不是注释、也不是字符串/文档字符串)。

    ★ 为什么不用"行首是不是 #"这种土办法: `gui/widgets.py` 的 mouse_xy 文档字符串里正
      解释着"为什么不用 ev.pos()" —— 那也是 `ev.pos()`, 土办法会把自己绊倒。
    """
    nums = set()
    with open(path, encoding="utf-8") as f:
        for tok in tokenize.generate_tokens(f.readline):
            if tok.type in (tokenize.COMMENT, tokenize.ENDMARKER,
                            tokenize.NL, tokenize.NEWLINE, tokenize.INDENT,
                            tokenize.DEDENT):
                continue
            nums.add(tok.start[0])
    return nums


def test_no_deprecated_event_pos_in_gui():
    """★ gui/ 里不许用已弃用的 `ev.pos()` —— 一律走 `mouse_xy(ev)`"""
    bad = []
    for p in sorted((ROOT / "gui").glob("*.py")):
        code = _code_line_numbers(p)
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if i in code and _EVENT_POS.search(line):
                bad.append(f"gui/{p.name}:{i}  {line.strip()}")
    assert not bad, (
        "这些地方还在用已弃用的 QMouseEvent.pos()(PySide6 会刷 DeprecationWarning):\n  "
        + "\n  ".join(bad) + "\n改成 mouse_xy(ev)(见 gui/widgets.py)")


def test_mouse_xy_helper_exists_and_is_used():
    """★ 统一入口得真存在、真按 Qt6 语义取值, 而且两个文件都在用它"""
    sys.path.insert(0, str(ROOT))
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])      # 造 QMouseEvent 需要 QApplication
    from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from gui.widgets import mouse_xy

    ev = QMouseEvent(QEvent.MouseButtonPress, QPointF(3.7, 9.2), QPointF(3.7, 9.2),
                     Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    got = mouse_xy(ev)
    # QPointF(3.7, 9.2).toPoint() → (4, 9): Qt 是**四舍五入**, 不是截断
    assert isinstance(got, QPoint) and (got.x(), got.y()) == (4, 9), got
    for name in ("widgets.py", "dialogs.py"):
        assert "mouse_xy" in (ROOT / "gui" / name).read_text(encoding="utf-8"), \
            f"gui/{name} 应该用 mouse_xy 取鼠标坐标"
