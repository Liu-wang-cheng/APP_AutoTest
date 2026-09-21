# -*- coding: utf-8 -*-
"""GUI 入口: python gui/main.py  或  pythonw gui/main.py

用 pythonw.exe 启动不会弹解释器的黑框。但如果它从一个**带控制台的父进程**
启动(如在 cmd / PowerShell 里调用,或某些启动器),Windows 会分配一个控制台
给它 —— 屏幕上就多一个无标题的黑框一直挂着。启动前 FreeConsole() 解绑即可。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _detach_console():
    """把本进程从控制台分离,消除启动时冒出的黑框。

    必须在任何 print / 日志输出**之前**调用 —— 一旦往控制台写了东西,
    就等于重新绑定了它,再解绑也去不掉窗口。
    从资源管理器双击启动时本来没有控制台,FreeConsole 会失败,忽略即可。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.FreeConsole()
    except Exception:
        pass


def main():
    _detach_console()

    from PySide6.QtWidgets import QApplication

    from core.logger import setup_logger
    from gui.main_window import MainWindow

    setup_logger()
    app = QApplication(sys.argv)
    # 应用图标(任务栏/窗口):自绘 乐动品牌融合(O传感器+对勾);ico 含 16~256 多尺寸
    from PySide6.QtGui import QIcon
    import os
    assets = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
    icon_path = os.path.join(assets, "app_icon.ico")
    if not os.path.isfile(icon_path):
        icon_path = os.path.join(assets, "app_icon.png")
    if os.path.isfile(icon_path):
        app.setWindowIcon(QIcon(icon_path))
    win = MainWindow()
    win.show()
    win.raise_()
    win.activateWindow()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
