# -*- coding: utf-8 -*-
"""内存占用日志: 每 10 分钟记一条(用户要求 2026-10-12)。

★ 为什么要它: 长时间连续跑用例时, 日志里留下内存时间序列, 事后能看出有没有缓慢上涨(泄漏),
  以及峰值出现在什么时候(峰值常在模板匹配/OCR 这类要整屏位图的时刻)。
  ★ 不引第三方依赖(psutil) —— 打包版每多一个包都要重验收集规则, 这里用 ctypes 调
    GetProcessMemoryInfo(实测: 不声明 argtypes 时 64 位句柄被截断 → 一直取不到值)。
"""
import logging
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import memlog                                            # noqa: E402


def test_interval_is_ten_minutes():
    """用户指定: 每十分钟打印一次"""
    assert memlog.INTERVAL_SECS == 600


@pytest.mark.skipif(os.name != "nt", reason="Windows 专用实现")
def test_rss_reads_positive_numbers_on_windows():
    """能读到正数(且峰值 >= 当前) —— 与 tasklist 交叉验证过(17.2MB vs 17,212K)"""
    cur, peak = memlog.rss_mb()
    assert cur and cur > 0, (cur, peak)
    assert peak >= cur, (cur, peak)


def test_log_once_writes_a_memory_line(monkeypatch):
    recs = []

    class _H(logging.Handler):
        def emit(self, record):
            recs.append(record.getMessage())

    h = _H(level=logging.INFO)
    memlog.log.addHandler(h)
    try:
        memlog.log_once("(测试)")
        memlog.log_once()
    finally:
        memlog.log.removeHandler(h)
    memlog_lines = [m for m in recs if "[内存]" in m]
    assert len(memlog_lines) == 2, recs
    assert "MB" in memlog_lines[0] and "峰值" in memlog_lines[0], memlog_lines[0]
    assert "(测试)" in memlog_lines[0], memlog_lines[0]


def test_start_spawns_a_daemon_thread(monkeypatch):
    """开始记录 = 起一个 daemon 线程(不能拖住退出), 且**立刻**先记一条"""
    import threading
    import time
    before = {t.name for t in threading.enumerate()}
    memlog.start(interval=9999)          # 大间隔: 测试期间不会重复刷
    for _ in range(50):                  # 等线程起来(≤0.5s)
        if any(t.name == "memlog" for t in threading.enumerate()):
            break
        time.sleep(0.01)
    t = next((t for t in threading.enumerate() if t.name == "memlog" and t.name not in before),
             None)
    assert t is not None, "没有起 memlog 线程"
    assert t.daemon, "必须是 daemon(否则关窗口会被它拖住)"


def test_startup_wires_the_memlog():
    """结构性守护: 程序启动时要真的起它, 别哪天被删掉"""
    src = Path(__file__).resolve().parent.parent / "gui" / "main.py"
    assert "memlog" in src.read_text(encoding="utf-8"), "启动没接内存日志"
