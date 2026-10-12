# -*- coding: utf-8 -*-
"""定期把自身内存占用写进日志(用户要求 2026-10-12: "每十分钟日志打印内存占用大小")。

★ 为什么要它: 长时间连续跑用例时, 光看任务管理器不方便 —— 日志里留下一条时间序列,
  事后能看出"内存是不是在慢慢涨"(泄漏), 以及峰值出现在什么时候(峰值常常出现在
  模板匹配/OCR 这类要整屏位图的时刻)。

★ 不引第三方依赖(psutil): 打包版每多一个包, 就要在 spec 与资源闸门里再验一遍
  (实测栽过 ch_ppocr_v3_det、pyclipper 两次)。这里直接用 ctypes 调 Windows 的
  GetProcessMemoryInfo; 非 Windows 用 resource.getrusage 兜底, 取不到就静默跳过。
"""
import ctypes
import os
import sys
import threading
import time

from core.logger import get_logger

log = get_logger()

INTERVAL_SECS = 600          # 10 分钟一次(用户指定)


class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


_win_fn = None


def _win_getmem():
    """GetProcessMemoryInfo(K32 版优先) —— ★ 必须显式声明 argtypes/restype:
    不声明的话 ctypes 按 c_int 处理, 64 位**进程句柄会被截断**, 调用直接失败
    (实测: 返回 0 → 一直是 None)。"""
    global _win_fn
    if _win_fn is None:
        try:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            fn = getattr(k32, "K32GetProcessMemoryInfo", None)
            if fn is None:
                fn = ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo
            fn.argtypes = [ctypes.c_void_p,
                           ctypes.POINTER(_PROCESS_MEMORY_COUNTERS), ctypes.c_uint32]
            fn.restype = ctypes.c_int
            _win_fn = fn
        except Exception:
            _win_fn = False          # 失败过就别反复试
    return _win_fn or None


def rss_mb():
    """当前进程内存 → (当前 MB, 峰值 MB); 取不到返回 (None, None)"""
    try:
        if os.name == "nt":
            counters = _PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(counters)
            fn = _win_getmem()
            if fn is None:
                return None, None
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            if fn(handle, ctypes.byref(counters), counters.cb):
                return (counters.WorkingSetSize / 1048576.0,
                        counters.PeakWorkingSetSize / 1048576.0)
            return None, None
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        mb = peak / 1048576.0 if sys.platform == "darwin" else peak / 1024.0
        return mb, mb
    except Exception:
        return None, None


def log_once(tag=""):
    """记一条内存日志(取不到就什么都不做)"""
    cur, peak = rss_mb()
    if cur is None:
        return None
    log.info(f"[内存]{tag} 当前 {cur:.0f}MB, 峰值 {peak:.0f}MB")
    return cur


def start(interval=INTERVAL_SECS):
    """起一个 daemon 线程: 立刻记一条, 之后每 interval 秒记一条。

    ★ daemon: 用户直接关窗口也不该被它拖住退出; 记录失败绝不影响主流程。
    """
    def _work():
        try:
            log_once("(启动)")
        except Exception:
            pass
        while True:
            time.sleep(interval)
            try:
                log_once()
            except Exception:
                pass

    threading.Thread(target=_work, name="memlog", daemon=True).start()
