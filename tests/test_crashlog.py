# -*- coding: utf-8 -*-
"""崩溃取证: 让"闪退"留下痕迹(用户 2026-10-12: 跑到第 19 轮静默退出, 日志里什么都没有)。

背景: 日志停在 `[前置] 电量等待结束,当前 48%`(前置失败阻断路径), 之后一行都没有 ——
既无 Traceback 也无半截行, 说明进程是被"直接干掉"的(原生崩溃 / 未捕获异常 / 被外部结束),
而当时**没有任何手段能区分**。这三条互补手段就是为下一次能查而加的。
"""
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import crashlog                                          # noqa: E402


def test_install_creates_crash_log_and_hooks(tmp_path):
    """装上后: crash.log 建好、未捕获异常钩子挂上"""
    old_main, old_thread = sys.excepthook, getattr(sys, "threading", None)
    import threading
    old_thread_hook = threading.excepthook
    try:
        crashlog.install(data_dir=str(tmp_path), qt=False)      # qt=False: 别动测试里的 Qt
        assert os.path.isfile(tmp_path / "reports" / "crash.log"), "crash.log 没建"
        assert sys.excepthook is not old_main, "主线程异常钩子没挂上"
        assert threading.excepthook is not old_thread_hook, "线程异常钩子没挂上"
    finally:
        sys.excepthook = old_main
        threading.excepthook = old_thread_hook


def test_uncaught_exception_lands_in_the_log(tmp_path):
    """未捕获异常(主线程/子线程)都要写进正常日志 —— 打包版 stderr 是空的, 平时完全静默"""
    import threading
    recs = []

    class _H(logging.Handler):
        def emit(self, record):
            recs.append(record.getMessage())

    h = _H(level=logging.INFO)
    crashlog.log.addHandler(h)
    try:
        crashlog.install(data_dir=str(tmp_path), qt=False)
        try:
            raise ValueError("主线程炸了")
        except Exception:
            sys.excepthook(*sys.exc_info())

        def _boom():
            raise RuntimeError("子线程炸了")

        t = threading.Thread(target=_boom)
        t.start()
        t.join()
    finally:
        crashlog.log.removeHandler(h)
    joined = "\n".join(recs)
    assert "[未捕获异常]" in joined, recs
    assert "主线程炸了" in joined and "子线程炸了" in joined, joined


def test_exit_breadcrumbs_exist():
    """结构性守护: 三条"退出留痕"必须都在 —— 下次才能区分"优雅退出"与"被硬结束"

    (关窗 / 更新重启 os._exit 之前 / 主循环正常结束)
    """
    root = Path(__file__).resolve().parent.parent
    mw = (root / "gui" / "main_window.py").read_text(encoding="utf-8")
    main = (root / "gui" / "main.py").read_text(encoding="utf-8")
    assert "[退出] 收到关窗请求" in mw, "关窗没留痕"
    assert "应用更新: 已启动替换脚本" in mw, "更新重启(os._exit)之前没留痕"
    assert "界面主循环结束" in main, "主循环结束没留痕"
    assert "crashlog" in main, "启动没装崩溃取证"
