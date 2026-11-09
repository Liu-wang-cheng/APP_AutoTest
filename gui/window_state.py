# -*- coding: utf-8 -*-
"""窗口几何记忆：关窗保存大小/位置/最大化，下次启动还原。

从 main_window.py 拆出来（主窗口行数有 3000 上限的守卫）。
"""
import os

from PySide6.QtCore import QSettings

_SETTINGS = ("vacuum_test", "case_studio")
_KEY = "ui/window_geometry"


def _is_pytest():
    """测试环境不读写真实设置(否则大量 GUI 测试会覆盖用户保存的尺寸)。"""
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


def restore_geometry(window):
    """还原上次窗口几何；没有记录/失败返回 False(调用方走默认尺寸)。"""
    if _is_pytest():
        return False
    try:
        geo = QSettings(*_SETTINGS).value(_KEY)
    except Exception:
        return False
    if not geo:
        return False
    try:
        return bool(window.restoreGeometry(geo))
    except Exception:
        return False


def save_geometry(window):
    """保存当前窗口几何(大小/位置/是否最大化)。"""
    if _is_pytest():
        return
    try:
        QSettings(*_SETTINGS).setValue(_KEY, window.saveGeometry())
    except Exception:
        pass


def reset_geometry():
    """清掉保存的几何(测试/排障用)。"""
    try:
        QSettings(*_SETTINGS).remove(_KEY)
    except Exception:
        pass
