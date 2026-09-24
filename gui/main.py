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


def _run_backup_async():
    """后台做一次用户数据备份 —— 不阻塞启动。

    ★ 必须异步: 用例/模板多的时候要复制几十 MB, 放主线程会让窗口白屏一两秒。
    ★ 必须 daemon: 备份跑着时用户直接关窗口也不该拖住进程退出。
    ★ 必须吞异常: 备份是兜底手段, 它自己失败绝不能影响程序启动。
    """
    import threading

    def _work():
        try:
            from core.backup import make_backup
            make_backup()
        except Exception:
            pass

    threading.Thread(target=_work, name="auto-backup", daemon=True).start()


def main():
    _detach_console()

    from PySide6.QtWidgets import QApplication

    from core.bootstrap import cleanup_update_leftovers, ensure_data_dirs
    from core.logger import setup_logger
    from gui.main_window import MainWindow

    ensure_data_dirs()      # 首次运行: 把 config/locators.yaml 等程序资源铺到数据目录
    cleanup_update_leftovers(os.path.dirname(os.path.abspath(sys.argv[0])))
    setup_logger()
    _run_backup_async()
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
