# -*- coding: utf-8 -*-
"""5MB 环形日志: 超限后截断重写,只保留最近日志。"""
import logging
import os
import sys
import threading

_LOGGER = None
_lock = threading.Lock()


class RotatingLogHandler(logging.StreamHandler):
    """写满 max_bytes 后从头覆盖(不是切文件,环形)。"""

    def __init__(self, path, max_bytes=5 * 1024 * 1024):
        super().__init__()
        self.path = path
        self.max_bytes = max_bytes
        self.stream = None
        self._pos = 0

    def _open(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.stream = open(self.path, "a", encoding="utf-8")
        self._pos = self.stream.tell()
        return self.stream

    def emit(self, record):
        try:
            msg = self.format(record) + self.terminator
            msg_bytes = msg.encode("utf-8")
            if self.stream is None:
                self.stream = self._open()
            if self._pos + len(msg_bytes) > self.max_bytes:
                self.stream.close()
                self.stream = open(self.path, "w", encoding="utf-8")
                self._pos = 0
            self.stream.write(msg)
            self.stream.flush()
            self._pos += len(msg_bytes)
        except Exception:
            self.handleError(record)


def setup_logger(log_path="reports/test.log", level=logging.INFO):
    """控制台 + 5MB 环形文件。重复调用幂等。

    ★ pythonw.exe 启动时 sys.stderr 是 None —— 此时挂 StreamHandler 会让
    第一次写日志就抛异常。GUI 用 pythonw 启动(为了不弹控制台窗口),所以这里
    必须先判空:没有 stderr 就只写文件日志,别把界面拖崩。
    """
    with _lock:
        global _LOGGER
        logger = logging.getLogger("vacuum_test")
        logger.setLevel(level)
        logger.handlers.clear()
        if sys.stderr is not None:      # pythonw 下为 None
            ch = logging.StreamHandler()
            ch.setLevel(level)
            ch.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
            logger.addHandler(ch)
        fh = RotatingLogHandler(log_path)
        fh.setLevel(level)
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(fh)
        _LOGGER = logger
        return logger


def get_logger():
    """模块级取 logger;未初始化时先落一份默认配置。"""
    if _LOGGER is None:
        return setup_logger()
    return _LOGGER
