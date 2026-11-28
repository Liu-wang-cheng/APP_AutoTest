# -*- coding: utf-8 -*-
"""core.logger → GUI「运行日志」页签的桥接。

★ 为什么单独成模块: main_window.py 有 3000 行上限(见结构守护测试)。
★ 为什么要能摘除: 窗口关闭/销毁后, 后台线程(APOS/SSH/扫描)晚到的日志如果
  再 emit 到已析构的 QObject, 会直接 access violation 闪退(crash.log 里就是
  `logging → main_window emit` 这条栈, 用户 2026-11-28 排查)。
"""
import logging


class GuiLogHandler(logging.Handler):
    """把日志记录转成回调; close_bridge() 后不再转发。"""

    def __init__(self, emit_fn, level=logging.INFO):
        super().__init__(level)
        self.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
        self._emit = emit_fn
        self._alive = True

    def emit(self, record):
        if not self._alive:
            return
        try:
            self._emit(self.format(record))
        except Exception:
            pass

    def close_bridge(self):
        self._alive = False
