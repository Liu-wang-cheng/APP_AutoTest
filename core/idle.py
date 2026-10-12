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
    `gui/runner_thread.RunWorker.run` 进入时 `begin_run()`、退出时(含异常)`end_run()`;
    前置条件、用例步骤、报告落盘全在这段区间内。用**计数**而不是布尔 —— 万一将来出现
    嵌套/并发的执行入口, 少一次 end_run 不会把闸门永久焊死。

★ 清理函数怎么接(唯一写法)
    在最前面加三行, 内部实现一行都不用改:

        from core import idle
        if idle.is_busy():                      # 正在跑用例 → 推迟(见 core/idle.py)
            idle.defer("这件清理的名字", lambda: <再来一次同样的调用>)
            return <空结果>

    `defer` 的名字用中文短句、同一件事只排一次; `lambda` 里**必须把参数原样带上**
    (漏一个, 补做时就用的是默认值)。`core/bootstrap.py` 的 7 个清理入口都是这个形态,
    并且有一条结构性守护测试盯着它们 —— 新增清理函数照做, 否则它就成了漏网的那个。
"""
import threading

from core.logger import get_logger

log = get_logger()

_lock = threading.RLock()
_cond = threading.Condition(_lock)   # 用同一个锁: "补做"完成时唤醒等待者
_depth = 0                       # >0 = 正在执行用例
_pending = []                    # [(名字, 可调用)] —— 运行中被推迟的清理, 保持顺序
_names = set()                   # 同一件事只排一次(封顶清理每次失败都会调一次)
_flushing = False                # 补做正在进行, 避免 repeat 叠加


def begin_run():
    """标记"开始执行用例"——此后到达的清理一律推迟"""
    global _depth
    with _lock:
        _depth += 1


def end_run():
    """标记"本轮执行结束"——把运行中推迟的清理补做一遍(后台线程, 不拖住调用方)"""
    global _depth
    with _lock:
        _depth = max(0, _depth - 1)
        if _depth:
            return
    _flush_in_thread()


def is_busy():
    """现在是否在执行用例"""
    with _lock:
        return _depth > 0


def defer(name, fn):
    """记下"这件清理待做"; 已经排过同名的不重复排。返回 True=新排入"""
    with _lock:
        if name in _names:
            return False
        _names.add(name)
        _pending.append((name, fn))
    log.info(f"[清理] 正在执行用例, {name} 推迟到本轮结束后再做")
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
    ★ 每做一件前先看闸门: 用户又点了「执行」就**停手**, 剩下的等下一轮 —— 不然等于
      把清理挪到了新一轮的执行期间, 白折腾。
    """
    if background:
        _flush_in_thread()
        return
    with _lock:
        while _flushing:            # Condition.wait 会临时放锁, 让人家做完
            _cond.wait(0.05)
    _flush_now()


def _flush_in_thread():
    global _flushing
    with _lock:
        if _flushing or not _pending:
            return
        _flushing = True
    threading.Thread(target=_flush_now, name="idle-cleanup", daemon=True).start()


def _flush_now():
    global _flushing
    with _lock:
        _flushing = True
    try:
        while True:
            with _lock:
                if not _pending:
                    return
                if _depth:
                    # 又开始跑用例了: 原样留着(名字也留着, 免得重复排), 等下一次 end_run
                    log.debug("[清理] 又开始了新一轮执行, 推迟的清理继续等")
                    return
                name, fn = _pending.pop(0)
                _names.discard(name)
            try:
                fn()
                log.debug(f"[清理] 补做完成: {name}")
            except Exception as e:          # 清理失败绝不影响任何主流程
                log.debug(f"[清理] 补做 {name} 失败(忽略): {type(e).__name__}: {e}")
    finally:
        with _lock:
            _flushing = False
            _cond.notify_all()


def reset():
    """清空状态(仅测试用) —— 计数归零、丢弃排队项, 不执行任何清理"""
    global _depth, _flushing
    with _lock:
        _depth = 0
        _flushing = False
        _pending.clear()
        _names.clear()
        _cond.notify_all()
