# -*- coding: utf-8 -*-
"""设备状态哨兵: 故障/空闲词表 + 连续命中计数 + 层级/XML 扫描(结构整理 2026-10-15)。

从 core/session.py 抽出的一整块**纯逻辑**(词表 + 计数 + 解析), 与"前置准备流程"无关:
  · 逐帧扫描可见文本 → 命中故障词(报错, 带整段原文)/空闲词(等待目标不会再出现);
  · **连续 N 轮**才触发(默认 3, 可在 config.yaml 的 runner.fault_streak/idle_streak 调),
    防掉半秒就消失的 toast / 状态栏瞬闪的"等待"字样造成误判;
  · xml 指纹变化视为换页, 计数不继承。

★ 行为与抽出来之前**逐字一致**(只搬位置): session.py 仍 re-export 这些名字,
  所以 `from core.session import scan_status_words` 之类的老用法照旧。
★ 阈值/词表在 GUI 的「故障检测」按钮里可改(StatusWordsDialog → session.set_status_words)。
"""
import re

from core.driver import split_texts as _split_texts   # 多值拆分: 与断言/前置同一套规则


_NODE_RE = re.compile(r"<node\b[^>]*>")
_NODE_ATTR_RE = re.compile(r'\b(text|content-desc|bounds)="([^"]*)"')
_ZERO_BOUNDS = re.compile(r"^\[0,0\]\[0,0\]$")


def visible_nodes(xml):
    """层级里的 (text, desc, 中心坐标|None) —— **只保留屏幕上真实可见的节点**。

    ★ 为什么要过滤(2026-10-11 用户实测的一类假通过): 界面切走/收起之后, 隐藏控件
      仍留在无障碍树里。真机现场: `断言: 干燥拖布,已完成,已取消` 通过了, 而那一刻的
      截图页面上只有「正在清洁」—— 命中的是**看不见的残留节点**(清洗拖布面板收起时
      留下的"…已完成"文案)。看不见的东西不该算"看到了"。
      判据: `bounds="[0,0][0,0]"`(Android 对未渲染/GONE 的节点就是这么报的) ⇒ 跳过。
      没有 bounds 属性的节点**保持原样**(宁可不误杀 —— 有些精简层级不带 bounds)。
    """
    out = []
    for tag in _NODE_RE.findall(xml):
        attrs = dict(_NODE_ATTR_RE.findall(tag))
        b = attrs.get("bounds")
        if b and _ZERO_BOUNDS.match(b):
            continue                         # 未渲染 → 视觉上不存在
        pos = None
        if b:
            m = re.match(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", b)
            if m:
                x1, y1, x2, y2 = (int(g) for g in m.groups())
                pos = ((x1 + x2) // 2, (y1 + y2) // 2)
        out.append((attrs.get("text", ""), attrs.get("content-desc", ""), pos))
    return out




#: 故障/空闲哨兵(用户 2026-10-13 定稿): 等待与断言的轮询里, 页面出现故障词 → 直接
#: 报错(带整段原文); 出现空闲词 → 立即判不成立(不再傻等)。可在 config.yaml 的
#: runner 段用 fault_texts / idle_texts 覆盖(逗号/顿号分隔)。
#:
#: 误触防护(用户 2026-10-13): 必须**连续 N 轮**都扫到同种状态才触发, 防止半秒就
#: 消失的 toast / 状态栏瞬闪的"等待"字样让用例在峰值瞬间被误判 —— 数值在
#: runner.fault_streak / runner.idle_streak 里(1-10, 默认 3)。三轮间的扫描间隔 = 调用方
#: 自己的轮询节奏(若 if 条件 ~2s/轮, _assert_locator ~1.5s/轮, 全文 OCR ~5s/轮)。
DEFAULT_FAULT_WORDS = ("故障", "错误", "卡住", "被困", "问题", "检查", "失败", "异常")
DEFAULT_IDLE_WORDS = ("待机", "空闲", "等待")
DEFAULT_FAULT_STREAK = 3        # "3 次以上"才触发(用户 2026-10-13)
DEFAULT_IDLE_STREAK = 3
_status_words = {"fault": DEFAULT_FAULT_WORDS, "idle": DEFAULT_IDLE_WORDS}
#: 连续命中计数: {(xml 指纹, 类别): 次数} —— 每轮 +=1(命中) / *= 0(未命中);
#: xml 切换(同节点集合变了)就当另一页, 重置对应类别的计数。
#: 并发访问靠 GIL 的 dict 原子操作成立(session 无锁; 值只会整数增减, 读到旧值无害)
_streak_counters = {}


def set_status_words(fault_texts="", idle_texts="",
                     fault_streak=None, idle_streak=None):
    """用配置覆盖哨兵词表与连续阈值(传空串/None=保留现值; ActionRunner 初始化时调)

    streak 越界(<1 或 >10)按默认 3 收尾 —— 用户在 GUI 里手填异常值也不会让整个测试
    循环一次就触发。
    """
    if str(fault_texts or "").strip():
        _status_words["fault"] = tuple(_split_texts(fault_texts))
    if str(idle_texts or "").strip():
        _status_words["idle"] = tuple(_split_texts(idle_texts))
    if fault_streak is not None:
        _status_words["fault_streak"] = _clamp_streak(fault_streak)
    if idle_streak is not None:
        _status_words["idle_streak"] = _clamp_streak(idle_streak)
    # 词表/阈值改了 → 计数清零(防"上次累积刚好 2 次, 改词后立刻凑齐 3 次误触发")
    _streak_counters.clear()


def _clamp_streak(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return DEFAULT_FAULT_STREAK
    return max(1, min(n, 10))


def reset_status_words():
    """把状态词与 streak 全部还原成内置默认(测试 conftest 用, 防测试间状态污染)"""
    _status_words.clear()
    _status_words["fault"] = DEFAULT_FAULT_WORDS
    _status_words["idle"] = DEFAULT_IDLE_WORDS
    _streak_counters.clear()


def _word_in(text, words):
    """text 里命中哪个词 → (词, True); 没命中 → (None, False)"""
    for w in words:
        if w and w in text:
            return w, True
    return None, False


def scan_status_words(xml=None, nodes=None):
    """扫**可见节点**里的故障/空闲词 → (kind, 词, 整段原文) / (None, None, None)。

    ★ 返回**整段原文**(用户 2026-10-13: 日志与报错要把带故障词的整段话打出来,
      如「滚刷被困, 请清理后重试」, 只打一个"被困"字没法定位)。
    ★ 故障优先: 先全树扫故障词, 再扫空闲词 —— 两个信号同时在屏时故障说了算。
    ★ 只扫可见节点(text/desc), 与 text_present_detail 同一可见性规则。
    nodes: 调用方已解析的可见节点(text_present_detail 传进来复用, 免得每轮把
    XML 解析两遍 —— 2026-10-13 优化)。
    """
    if nodes is None:
        if not xml:
            return None, None, None
        nodes = visible_nodes(xml)
    for kind in ("fault", "idle"):
        for node in nodes:
            for raw in (node[0], node[1]):
                if not raw:
                    continue
                w, hit = _word_in(raw, _status_words[kind])
                if hit:
                    return kind, w, raw
    return None, None, None


def streak_status_word(kind, hit, xml_fingerprint):
    """给调用方用: 把"本轮命中 kind?" + xml 指纹送进来, 返回**当前连续次数**。

    ★ 误触防护(用户 2026-10-13): 每次状态变化都自增/清零, 阈值到才告诉调用方"可以触发了"
    —— 阈值由 set_status_words(fault_streak=...) 配(默认 3)。xml 切换视为另一页,
    **不**继承旧计数(否则换页那一下第一帧就凑齐 3 次)。
    hit: True / False(本轮是否命中 kind 类)。"未命中另一类"只清零**对应类别**的计数
    (故障 vs 空闲 各自独立, 不会互相重置 —— 避免"刚凑齐 3 次空闲被一个错误清零")。
    返回值: int, 当前累计次数(0 表示"未在累计中" / 此次未命中而清零了)。
    """
    target = _streak_counters.setdefault(xml_fingerprint, {"fault": 0, "idle": 0})
    if hit:
        target[kind] = target.get(kind, 0) + 1
    else:
        target[kind] = 0
    return target[kind]


def fingerprint_xml(xml):
    """给一帧 XML 算指纹 —— 节点文本前 32 字符的拼接(可读够区分页面, 不区分瞬时闪烁)。

    调 streak_status_word 之前先算好; 同一页多轮调用应传入相同指纹, 跨页指纹会变
    → streak 自然重置(误触防护的天然保障)。
    """
    if not xml:
        return ""
    parts = []
    for m in re.finditer(r'(?:text|content-desc)="([^"]*)"', xml):
        s = m.group(1)
        if s:
            parts.append(s[:32])
    return "|".join(parts[:20])
