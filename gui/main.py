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


def _run_ocr_selfcheck_async():
    """后台确认 OCR 兜底**真的能用**, 并在日志里留下结论 —— 不阻塞启动。

    ★ 为什么(2026-10-11 用户报"OCR 还在报错"): 打包版漏收 rapidocr 的运行时依赖
      (pyclipper 等)时, 日志照样先打"RapidOCR 已加载", 直到识别那一刻才炸, 每步刷一条
      "[OCR] 识别失败(忽略)" —— 看上去像偶发, 其实兜底一直死着。真跑一次自检,
      要么"自检通过", 要么一次说清原因。
    ★ 异步: 构造 + 识别一次约 1~2 秒, 放主线程会让启动明显变慢。
    ★ 顺带成为**构建闸门**: tools/verify_package.py 会启动打包产物并检查这行日志,
      没有"自检通过"就直接判打包失败(防止再发一个 OCR 是死的版本)。
    """
    import threading

    def _work():
        try:
            import time as _t
            _t.sleep(3)                     # 让启动先完成(与更新检查错开)
            from core import ocr
            ocr.self_check()
        except Exception:
            pass

    threading.Thread(target=_work, name="ocr-selfcheck", daemon=True).start()


def _prune_debug_async():
    """后台给 Test_img/debug/ 封顶 —— 调试产物(点击标记/失败截图)不该无限堆。

    用户要求(2026-10-11): "测试的调试图片也要限制一下"。测试期有 conftest 每轮清,
    打包版没有, 跑几个月能堆到几个 GB。策略见 core.bootstrap.prune_debug_artifacts。

    ★ 它只在**没有执行用例**时真的动手(用户要求 2026-10-12: "清理产物封顶…需要在工具
      没有执行用例时去做, 减少内存占用")。启动后台线程与"用户开机就点执行"很容易重叠,
      闸门会把这一趟推迟到本轮跑完再做(见 core/idle.py), 这里不用额外判断。
    """
    import threading

    def _work():
        try:
            from core.bootstrap import prune_run_artifacts
            prune_run_artifacts()          # 用例截图按次数 / 报告与失败截图按份数封顶
        except Exception:
            pass

    threading.Thread(target=_work, name="prune-debug", daemon=True).start()


def _start_memlog():
    """起"每 10 分钟记一次内存"的后台线程(用户要求 2026-10-12)。

    长时间跑用例时, 日志里留下内存时间序列, 事后能看出有没有缓慢上涨; 不引第三方依赖
    (见 core/memlog.py)。
    """
    try:
        from core import memlog
        memlog.start()
    except Exception:
        pass


def _install_crashlog():
    """装崩溃取证(原生崩溃栈 / 未捕获异常 / Qt 消息) —— 见 core/crashlog.py。

    ★ 为什么: 用户跑到第 19 轮时进程静默消失, 日志里连一行报错都没有 —— 没有取证手段
      就只能猜。装好之后三种"闪退"来源各自都会留痕。
    """
    try:
        from core import crashlog
        crashlog.install()
    except Exception as e:
        # ★ 别静默(审查 2026-10-12): 取证装不上 = 退回"闪退无迹可查", 至少留一行
        try:
            from core.logger import get_logger as _gl
            _gl().warning(f"[取证] 崩溃取证安装失败(闪退将无迹可查): "
                          f"{type(e).__name__}: {e}")
        except Exception:
            pass


def main():
    _detach_console()

    # ★ stdout/stderr 编码兜底: 英文 Windows(cp1252)下打印中文会 UnicodeEncodeError;
    #   把输出重定向到文件/管道时同样按 ANSI 代码页 —— 不看有没有控制台, 所以这条
    #   对"用户把程序输出接到别处"的情形同样有用(2026-10-10 在 CI 上实测踩到)
    from core.console import force_utf8_stdout
    force_utf8_stdout()

    from PySide6.QtWidgets import QApplication

    from core.bootstrap import cleanup_update_leftovers, ensure_data_dirs
    from core.logger import setup_logger
    from gui.main_window import MainWindow

    ensure_data_dirs()      # 首次运行: 把 config/locators.yaml 等程序资源铺到数据目录
    # ★ 更新残渣清理同样过"空闲闸门"(用户 2026-10-12): 这里在窗口出现前跑, 正常是空闲的;
    #   万一有执行线程在跑, 它会把自己推迟到本轮结束(见 core/idle.py)。
    cleanup_update_leftovers(os.path.dirname(os.path.abspath(sys.argv[0])))
    setup_logger()
    _run_backup_async()
    _run_ocr_selfcheck_async()
    _prune_debug_async()
    _start_memlog()
    _install_crashlog()
    app = QApplication(sys.argv)
    # 应用图标(任务栏/窗口):自绘 乐动品牌融合(O传感器+对勾);ico 含 16~256 多尺寸
    # ★ 不要在 main() 里再 `import os` —— 函数内 import 会把 os 变成**整个函数**
    #   的局部变量, 上面 cleanup_update_leftovers(os.path...) 那行就会
    #   UnboundLocalError(打包后启动即崩, 产物验证抓到过)。模块级已导入。
    from PySide6.QtGui import QIcon
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
    rc = app.exec()
    # ★ 留痕: 能走到这里 = **界面主循环正常结束**(正常退出)。排查"闪退"时, 没有这行
    #   又没有"[退出] 收到关窗请求", 就说明进程是被硬结束的(见 core/crashlog.py)。
    from core.logger import get_logger as _gl
    _gl().info("[退出] 界面主循环结束, 进程正常退出")
    # ★ 硬退出, 跳过解释器/C++ 析构(用户 2026-10-12 排查闪退时定位的第二类竞态):
    #   正常退出时 MainWindow 被回收 → Qt 析构它的 children —— 设备扫描/更新检查/
    #   更新下载这些 QThread 都挂在窗口下, **还在跑就被析构** = 和 RunWorker 同一个
    #   qFatal("QThread: Destroyed while thread is still running") → 表现为"关窗闪退"
    #   (下载更新要几分钟, 期间关窗必中)。无 parent 的 RunWorker 退出时同理。
    #   os._exit 让"析构运行中的线程"这个前提不复存在; 日志每条 emit 即刻 flush,
    #   关窗留痕在 closeEvent 里已落盘, 这里不丢东西。下载中断的半截文件由下次启动
    #   的 cleanup_update_leftovers 接住(设计如此)。
    import logging as _logging
    _logging.shutdown()
    os._exit(rc if isinstance(rc, int) else 0)


if __name__ == "__main__":
    sys.exit(main())
