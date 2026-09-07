import logging
import os


class RotatingLogHandler(logging.FileHandler):
    """循环日志：超过 max_bytes 从头覆盖"""

    def __init__(self, filename, max_bytes=5 * 1024 * 1024):
        self.max_bytes = max_bytes
        self._pos = 0
        super().__init__(filename, mode="a", encoding="utf-8", delay=True)

    def emit(self, record):
        try:
            msg = self.format(record) + self.terminator
            msg_bytes = msg.encode("utf-8")
            # 检查是否需要截断
            if self.stream is None:
                self.stream = self._open()
            current_size = self.stream.tell()
            if current_size + len(msg_bytes) > self.max_bytes:
                self.stream.seek(0)
                self.stream.truncate()
                self._pos = 0
            self.stream.write(msg)
            self.stream.flush()
        except Exception:
            self.handleError(record)


def setup_logger(log_path="reports/test.log", level=logging.INFO):
    """初始化日志：控制台 + 循环文件（5MB）"""
    logger = logging.getLogger("vacuum_test")
    logger.setLevel(level)
    logger.handlers.clear()

    # 控制台
    ch = logging.StreamHandler()
    ch.setLevel(level)
    ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
    logger.addHandler(ch)

    # 循环文件
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    fh = RotatingLogHandler(log_path, max_bytes=5 * 1024 * 1024)
    fh.setLevel(level)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)

    return logger
