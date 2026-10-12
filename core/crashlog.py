# -*- coding: utf-8 -*-
"""崩溃取证 —— 让"闪退"留下痕迹(用户 2026-10-12: 跑到第 19 轮静默退出, 日志里什么都没有)。

★ 背景: 日志停在 `[前置] 电量等待结束,当前 48%`(前置失败阻断路径), 之后**一行都没有**,
  既没有 Traceback、也没有半截行 —— 也就是说进程被"直接干掉"了。可能的来源:
    · 原生崩溃(段错误/abort): Qt / opencv / onnxruntime 的 C++ 层出问题;
    · 未捕获的 Python 异常: 打包版是 pythonw, **stderr 无处可去** → 平时完全静默;
    · 被外部结束(任务管理器/系统内存不足)。
  这三种在现有代码里**无法区分**, 所以这里补三条互补的取证手段(都不引第三方依赖):

    ① faulthandler —— 原生崩溃时把**各线程的 Python 栈**写进 reports/crash.log;
    ② sys.excepthook / threading.excepthook —— 未捕获异常(含子线程)写进正常日志;
    ③ Qt 消息处理器 —— Qt 自己的 warning/critical/fatal 也进日志
       (fatal 会 abort, 有它至少能定位到哪一句触发的)。

  另外配合 gui/main_window.py 里那几行"退出前留痕"(正常关窗 / 更新重启),
  下次再看日志就能判断: 是**优雅退出**还是**被干掉**。
"""
import os
import sys
import threading
import traceback

from core.logger import get_logger

log = get_logger()

_crash_file = None          # 必须一直开着: faulthandler 写的是这个句柄


def _data_dir():
    from core.driver import DATA_DIR
    return DATA_DIR


def install(data_dir=None, qt=True):
    """装上三条取证手段; 任何一条失败都不影响程序启动

    ★ data_dir 缺省(生产启动)时**幂等**: 重复调用直接返回, 不覆盖旧句柄、不重装钩子
      (审查 2026-10-12 —— 重装会泄漏上一个 crash.log 句柄, Windows 上文件被占住删不掉)。
      测试显式传 data_dir, 每次都是全新安装。
    """
    global _crash_file
    if _crash_file is not None and data_dir is None:
        return
    # ① 原生崩溃栈 → reports/crash.log
    try:
        root = data_dir or _data_dir()
        path = os.path.join(root, "reports", "crash.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # buffering=1: 行缓冲, 崩的那一刻已经落盘; 必须保住这个句柄(局部变量会被回收)
        _crash_file = open(path, "a", buffering=1, encoding="utf-8", errors="replace")
        import faulthandler
        faulthandler.enable(file=_crash_file, all_threads=True)
        log.info(f"[取证] 原生崩溃(段错误/abort)的调用栈将写入 {path}")
    except Exception as e:
        log.warning(f"[取证] faulthandler 未启用(忽略): {type(e).__name__}: {e}")

    # ② 未捕获的 Python 异常(打包版 stderr 是空的, 这类异常平时没人看得见)
    def _hook(exc_type, exc, tb):
        try:
            log.error("[未捕获异常]\n" + "".join(
                traceback.format_exception(exc_type, exc, tb)))
        except Exception:
            pass

    sys.excepthook = _hook
    try:
        def _thread_hook(args):          # py3.8+: 子线程里的未捕获异常
            _hook(args.exc_type, args.exc_value, args.exc_traceback)
        threading.excepthook = _thread_hook
    except Exception:
        pass

    # ③ Qt 自己的消息(含 fatal: 它会直接 abort 进程)
    if qt:
        try:
            from PySide6.QtCore import QtMsgType, qInstallMessageHandler

            def _qt_handler(mode, _ctx, message):
                try:
                    if mode == QtMsgType.QtFatalMsg:
                        log.error(f"[Qt 致命] {message}")
                    elif mode in (QtMsgType.QtCriticalMsg, QtMsgType.QtWarningMsg):
                        log.warning(f"[Qt] {message}")
                except Exception:
                    pass

            qInstallMessageHandler(_qt_handler)
        except Exception as e:
            log.debug(f"[取证] Qt 消息处理器未装上(忽略): {e}")


def disable():
    """撤销取证(仅测试用): 关 faulthandler、合上 crash.log 句柄 ——
    不然测试的临时目录里那个文件一直被占着, Windows 上删不掉"""
    global _crash_file
    try:
        import faulthandler
        faulthandler.disable()
    except Exception:
        pass
    f, _crash_file = _crash_file, None
    if f is not None:
        try:
            f.close()
        except Exception:
            pass
