# -*- coding: utf-8 -*-
"""空闲闸门: 「封顶清理 / 更新残渣」这类重活只在**没有执行用例**时做。

用户要求(2026-10-12): "清理产物封顶和更新残渣这些操作时需要在工具没有执行用例时去做,
减少内存占用"。

★ 为什么要错开
    执行用例时本来就吃内存 —— 一张 1080x1920 的截屏位图约 6MB, 加上模板匹配、OCR 的
    onnxruntime 会话; 而封顶清理要**遍历**整个 Test_cases / Test_img / reports 目录树、
    对每个文件 stat 一遍, 再把结果堆成列表(上万个文件时是实打实的几 MB 加一堆文件句柄)。
    两件事撞在同一秒, 峰值就是两者之和 —— 而这清理既不紧急、也不要求当场做完。
    (启动时那几处尤其容易撞: 备份/封顶都是后台线程, 用户开机就点「执行」就会重叠。)

★ 为什么是"推迟"而不是"跳过"
    直接 `return` 会让产物封顶在最需要它的场景(**长时间连续跑用例**)彻底失效 ——
    跑一夜下来截图和时序证据没人回收, 反而把盘写满。
    所以这里只做一件事: 运行中把调用**记下来**, 本轮执行一结束(`end_run`)立刻补做一遍。
    真跳过的只有一种情况: 记下之后进程就退出了(比如关窗) —— 那也该由下次启动的
    兜底清理接住, 不是新问题。

★ 何时算"在执行用例"
    `gui/runner_thread.RunWorker.run` 进入时 `begin_run()`、退出时(含异常)`end_run(token)`;
    前置条件、用例步骤、报告落盘全在这段区间内。用**令牌集**而不是布尔 —— 嵌套/并发的
    执行入口互不干扰, 迟到/重复的 end_run 也动不了别的轮次的计数(审查 2026-10-12)。

★ 清理函数怎么接(唯一写法)
    挂上 `@idle.gated` 装饰器, 内部实现一行都不用改:

        @idle.gated("这件清理的名字", empty=(0, 0))      # empty = 忙时的空返回值
        def trim_xxx(...): ...

    名字用中文短句、同一件事只排一次; 要按**参数**去重的(如 trim_case_screenshots 按
    用例名), name 传 `lambda *a, **k: f"...({a[1]})"`。装饰器会按**原参数**重调 ——
    补做时不会用默认值。`core/bootstrap.py` 的 7 个清理入口都挂了它, 并且有一条结构性
    守护测试盯着 —— 新增清理函数照做, 否则它就成了漏网的那个。

★ 已知的窄残余(有意保留, 审查 2026-10-12 #16)
    补做线程 pop 出一件清理后、执行期间, 用户点了「执行」—— **这一件**会跑完
    (下一件起才被挡住, 见 _flush_now 的循环内检查)。要根除得让清理函数支持协作式中断,
    收益(几秒的目录遍历重叠)配不上复杂度; `test_flush_stops_when_a_new_run_starts`
    钉住了"下一件必须被挡下"这条底线。
"""
import functools
import threading
import time

from core.logger import get_logger

log = get_logger()

_lock = threading.RLock()
_active = set()                  # 在执行的"令牌"集 —— 非空 = 正在执行用例
_token_seq = 0                   # 令牌发号器(begin_run 发一个, end_run 凭令牌销账)
_pending = []                    # [(名字, 可调用)] —— 运行中被推迟的清理, 保持顺序
_flushing = False                # 补做正在进行(_flush_now 入口串行化, 不并发两趟)


def begin_run():
    """标记"开始执行用例"——此后到达的清理一律推迟。返回令牌, end_run 时带回"""
    global _token_seq
    with _lock:
        _token_seq += 1
        _active.add(_token_seq)
        return _token_seq


#: 运行结束后的**空闲宽限**(秒, 用户 2026-10-14: "清理流程放到工具空闲时再运行"):
#: 结束后先等这么久, 期间没有新运行才真正补做 —— 用户跑完马上又点「执行」的话,
#: 清理会再次让路(待办保留, 下次 end_run 再排)。改小可加速测试。
FLUSH_IDLE_DELAY = 120.0
_flush_timer = None


def end_run(token=None):
    """标记"本轮执行结束"——**等工具空闲**(宽限期无新运行)后把推迟的清理补做一遍。

    token: begin_run 返回的令牌。带上它, "上一轮迟到的收尾"就不会动新一轮的
    计数(审查 2026-10-12); 不带(旧调用/测试)退回减一语义。
    """
    with _lock:
        if token is not None:
            if token not in _active:
                return               # 不属于任何在跑的轮次(重复销账/异物): 不动别人的计数
            _active.discard(token)
        elif _active:
            _active.pop()
    if not _active:
        _schedule_idle_flush()


def _schedule_idle_flush():
    """空闲宽限后补做(用户 2026-10-14): 结束立即刷会撞上"用户马上又点执行" ——
    宽限期内新运行开始, 定时器到点时 is_busy → 直接跳过(待办保留, 下次 end_run
    再排); 真空闲才 flush。多个定时器并发无害(_flush_now 自身串行 + 查闸门)。
    """
    global _flush_timer
    t = threading.Timer(FLUSH_IDLE_DELAY, _idle_flush_fire)
    t.daemon = True
    t.name = "idle-cleanup-wait"
    with _lock:
        _flush_timer = t
    t.start()


def _idle_flush_fire():
    with _lock:
        _flush_timer = None
    if not _active:
        _flush_in_thread()
    # 忙(宽限期内又开始运行) → 待办原样保留, 下一次 end_run 再排


def is_busy():
    """现在是否在执行用例"""
    with _lock:
        return bool(_active)


def gated(name, empty):
    """清理函数的闸门装饰器: 正在执行用例 → 把**原参数**的调用记进待办, 返回空结果。

    name:  字符串; 或 callable(*args, **kwargs) → str(按参数去重, 见 trim_case_screenshots)。
    empty: 忙时的返回值 —— list/tuple 传**常量**(不可变, 共享无碍); 传 callable 则每次
           新造(如 `list`), 需要新鲜可变对象时用后者。
    """
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if is_busy():
                nm = name(*args, **kwargs) if callable(name) else name
                defer(nm, lambda: fn(*args, **kwargs))
                if empty is None:
                    return None
                return empty() if callable(empty) else empty
            return fn(*args, **kwargs)
        return wrapper
    return deco


def defer(name, fn):
    """记下"这件清理待做"; 已经排过同名的不重复排。返回 True=新排入"""
    with _lock:
        if any(n == name for n, _f in _pending):
            return False
        _pending.append((name, fn))
        # ★ "闸门刚开"的缝隙(审查 2026-10-12): is_busy()→defer() 两步之间本轮恰好
        #   结束、补做线程已扫过空队列返回 —— 再不自己触发一次, 这件清理就滞留到
        #   下一次 end_run / 下次启动(本次会话内静默失效)
        orphaned = not _active and not _flushing
    log.info(f"[清理] 正在执行用例, {name} 推迟到工具空闲时再做")
    if orphaned:
        _flush_in_thread()
    return True


def pending_names():
    """当前排着没做的清理名(排查/测试用)"""
    with _lock:
        return [n for n, _f in _pending]


def flush(background=True):
    """把推迟的清理补做掉。

    ★ 默认后台线程: `end_run()` 是执行线程**收尾**时调的, 让它在那儿等目录遍历
      (可能几秒)会把"本轮结束"的信号一起拖后。
    ★ 同步模式(background=False)必须**真的做完才返回**: 已经有一趟在跑时先等它
      结束, 而不是看见 `_flushing` 就走 —— 否则"补做完了"是假的(测试里表现为
      断言时文件还在, 后台线程晚一步才删)。
    """
    if background:
        _flush_in_thread()
        return
    while _flushing:                # 有后台补做在跑: 等它收尾(见 _flush_now 的串行化)
        time.sleep(0.05)
    _flush_now()


def _flush_in_thread():
    with _lock:
        if _flushing or not _pending:
            return
    threading.Thread(target=_flush_now, name="idle-cleanup", daemon=True).start()


def _flush_now():
    """真正干活的一趟; 后台线程与同步 flush 都从这进 —— **串行化**, 不并发两趟。

    ★ 每做一件前先看闸门: 用户又点了「执行」就**停手**, 剩下的等下一轮 —— 不然等于
      把清理挪到了新一轮的执行期间, 白折腾。(pop 出来的那一件除外 —— 已知窄残余,
      见模块 docstring。)
    """
    global _flushing
    while True:
        with _lock:
            if not _flushing:
                _flushing = True
                break
        time.sleep(0.05)            # 已有一趟在跑: 等它收尾再接手(审查 2026-10-12)
    try:
        while True:
            with _lock:
                if not _pending:
                    return
                if _active:
                    # 又开始跑用例了: 原样留着(名字随条目一起留, 免得重复排), 等下一次 end_run
                    log.debug("[清理] 又开始了新一轮执行, 推迟的清理继续等")
                    return
                name, fn = _pending.pop(0)
            try:
                fn()
                log.debug(f"[清理] 补做完成: {name}")
            except Exception as e:          # 清理失败绝不影响任何主流程
                log.debug(f"[清理] 补做 {name} 失败(忽略): {type(e).__name__}: {e}")
    finally:
        with _lock:
            _flushing = False


def reset():
    """清空状态(仅测试用) —— 丢弃排队项、等在跑的补做收尾(最多 5s), 不执行任何清理。

    ★ 等**而不是强清**(审查 2026-10-12 #17): 强清 _flushing 会让上一条测试的补做
      线程与下一条测试的并发起来; 守护线程指向的还是上一条测试的临时目录。
    """
    global _token_seq, _flushing
    deadline = time.monotonic() + 5
    while True:
        with _lock:
            if not _flushing or time.monotonic() > deadline:
                break
        time.sleep(0.05)
    with _lock:
        _token_seq = 0
        _active.clear()
        _pending.clear()
        _flushing = False
