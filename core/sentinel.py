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
_NODE_ATTR_RE = re.compile(r'\b(text|content-desc|bounds|package)="([^"]*)"')
_ZERO_BOUNDS = re.compile(r"^\[0,0\]\[0,0\]$")

#: **手机系统 UI** 的包名 —— 文本判断一律忽略它们(用户 2026-10-15 实测报的 bug):
#: 层级 dump 里除了被测 APP, 还带着**手机自己的**状态栏/通知栏(com.android.systemui)。
#: 后果很实在: 手机插着 USB 在充电 → 状态栏/通知里有「正在充电」→ 而"确保设备在充电"
#: 这条前置找的就是「充电」⇒ **机器人没在充电也判通过**(拿手机的电量状态顶了机器人的)。
#: 同类风险: 系统通知里的「检查更新」(命中故障词「检查」)、锁屏「等待中」(命中空闲词)。
#: 想再排除别的包(如某个常驻通知的 APP), 见 set_ignore_packages()。
SYSTEM_UI_PACKAGES = ("com.android.systemui",)
_ignore_packages = set(SYSTEM_UI_PACKAGES)


def set_ignore_packages(packages):
    """覆盖"忽略哪些包的文本"(config.yaml 的 runner.text_ignore_packages; 传 None/空=用默认)

    配置项存在的意义: 各手机/模拟器的系统 UI 包名不总是一样(有的 ROM 还有自家的
    com.miui.systemui 之类), 或者某个常驻通知 APP 老在页面上挂着干扰词 —— 不用改代码就能排掉。
    """
    global _ignore_packages
    if packages is None:
        _ignore_packages = set(SYSTEM_UI_PACKAGES)
        return
    if isinstance(packages, str):
        packages = [p for p in re.split(r"[,，、;\s]+", packages) if p]
    merged = set(SYSTEM_UI_PACKAGES)
    merged.update(str(p).strip() for p in (packages or []) if str(p).strip())
    _ignore_packages = merged


def ignore_packages():
    """当前忽略的包名集合(测试/排查用)"""
    return set(_ignore_packages)


_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


def tag_is_system_ui(tag):
    """这个 `<node …>` 标签是不是**手机系统 UI**(状态栏/通知栏)的?

    给"自己解析层级"的调用方用(如 runner._get_all_nodes 抓取数据), 判据与
    visible_nodes 完全一致 —— 两处必须同规则, 否则文本序列会错位。
    """
    attrs = dict(_NODE_ATTR_RE.findall(tag))
    return attrs.get("package") in _ignore_packages


def system_ui_rects(xml):
    """**实测**系统 UI(状态栏/通知栏/导航栏)在屏幕上的矩形 [(x1,y1,x2,y2)]。

    ★ 为什么不用"裁掉顶部 5%"这种比例(2026-10-15 实测):
      1080x1920 上 5% = 96px, 而系统栏实测只到 61px —— 多切 35px; 更危险的是 APP
      自己的标题在 y=104, 距 5% 那条线只剩 **8px**, 换台挖孔屏(状态栏 90~110px)就会
      把 APP 顶部文案一起切掉, 表现为"断言莫名判不成立"这类最难查的回归。
      按**层级里系统 UI 节点的真实 bounds** 算就没有这个猜测: 它随设备/ROM 自适应。
    ★ 只认 ignore_packages() 里的包; 层级里没有它们(精简层级) → 返回空 = 不过滤。
    """
    out = []
    for tag in _NODE_RE.findall(xml or ""):
        attrs = dict(_NODE_ATTR_RE.findall(tag))
        if attrs.get("package") not in _ignore_packages:
            continue
        m = _BOUNDS_RE.search(attrs.get("bounds") or "")
        if m:
            out.append(tuple(int(g) for g in m.groups()))
    return out


def system_ui_regions(xml, top_ratio=0.15, bottom_ratio=0.85):
    """系统 UI **实际遮住的区域** [(x1,y1,x2,y2)] —— 给 OCR 兜底按坐标过滤用。

    ★ 为什么不能直接用系统 UI 节点的原始矩形(实测踩到): 状态栏是**整宽**一条, 而它的
      节点只有各自的小方块(时钟 [10,9][110,51]、电池图标 [989,13][1009,47]) —— 一条
      x=0 起头的通知横幅跟这两个框都不"完全相交", 按原矩形丢就会漏掉它。
      所以: **贴顶/贴底**的系统节点归成一条**整宽带**(状态栏 / 导航栏本来就是整宽窗口),
      中间的系统窗口(悬浮窗、气泡)只按它自己的矩形算 —— 那种不该整宽, 否则会把同一行
      的 APP 文案一起误杀。
    ★ 屏幕宽高从层级里所有节点的最大边界推出来(不额外问设备, 零成本)。
    """
    rects = system_ui_rects(xml)
    if not rects:
        return []
    w = h = 0
    for tag in _NODE_RE.findall(xml or ""):
        m = _BOUNDS_RE.search(dict(_NODE_ATTR_RE.findall(tag)).get("bounds") or "")
        if m:
            w = max(w, int(m.group(3)))
            h = max(h, int(m.group(4)))
    if not w or not h:
        return rects          # 层级里连全屏节点都没有: 退回"按各自原始矩形", 绝不整宽
    # ★ 顶带判据用**两个条件同时成立**(只看屏幕比例会被稀疏层级骗: 没有全屏节点时 h
    #   等于最大节点下沿, 一条底部导航栏就能让"顶带"吞掉整屏 —— 那是灾难性的误杀):
    #   ① 它在屏幕上半部分; ② 它的下沿不超过"系统矩形最小下沿"的 2 倍。
    _min_y2 = min(r[3] for r in rects)
    tops = [r for r in rects
            if r[3] <= h * 0.5 and r[3] <= max(_min_y2 * 2, _min_y2 + 12)]
    bottoms = [r for r in rects if r[1] >= h * bottom_ratio]
    out = []
    if tops:
        out.append((0, 0, w, max(r[3] for r in tops)))          # 状态栏: 整宽
    if bottoms:
        out.append((0, min(r[1] for r in bottoms), w, h))       # 导航栏: 整宽
    mid = [r for r in rects if r not in tops and r not in bottoms]
    out.extend(mid)                                             # 悬浮窗: 按原矩形
    return out


def drop_system_ui_lines(lines, rects):
    """丢掉**整行都落在系统 UI 区域里**的 OCR 结果 —— 返回保留下来的行。

    lines: [(文本, (x1,y1,x2,y2))](见 core/ocr.py::screen_boxes)
    rects: system_ui_regions(xml) 的结果

    ★ 判据严格(用户 2026-10-15 定的方案): 只有**整框**都在区域里才丢; 压线的一律保留
      —— 宁可漏排一条状态栏文案, 也不能把"从状态栏压线往下的 APP 文案"误杀。
    ★ 为什么 OCR 侧还需要它: 树侧已经按包名排除了(visible_nodes), 但 OCR 读的是**像素**,
      没有包名可依 —— 手机状态栏上"看得见"的字(通知横幅、锁屏提示)只能靠坐标排。
    """
    if not rects:
        return list(lines)
    kept = []
    for txt, box in lines:
        x1, y1, x2, y2 = box
        inside = any(x1 >= rx1 and y1 >= ry1 and x2 <= rx2 and y2 <= ry2
                     for rx1, ry1, rx2, ry2 in rects)
        if not inside:
            kept.append((txt, box))
    return kept


def visible_nodes(xml):
    """层级里的 (text, desc, 中心坐标|None) —— **只保留屏幕上真实可见的节点**。

    ★ 为什么要过滤(2026-10-11 用户实测的一类假通过): 界面切走/收起之后, 隐藏控件
      仍留在无障碍树里。真机现场: `断言: 干燥拖布,已完成,已取消` 通过了, 而那一刻的
      截图页面上只有「正在清洁」—— 命中的是**看不见的残留节点**(清洗拖布面板收起时
      留下的"…已完成"文案)。看不见的东西不该算"看到了"。
      判据: `bounds="[0,0][0,0]"`(Android 对未渲染/GONE 的节点就是这么报的) ⇒ 跳过。
      没有 bounds 属性的节点**保持原样**(宁可不误杀 —— 有些精简层级不带 bounds)。
    ★ 再排除**手机系统 UI** 的包(2026-10-15 用户报的真 bug): dump 里除了被测 APP 还带着
      手机自己的状态栏/通知栏 —— "手机在充电"会被"确保设备在充电"这条前置当成机器人
      在充电判通过。见 SYSTEM_UI_PACKAGES。
      同样: 没有 package 属性的节点保持原样(精简层级不带 package, 不能一刀切杀掉)。
    """
    out = []
    for tag in _NODE_RE.findall(xml):
        attrs = dict(_NODE_ATTR_RE.findall(tag))
        if attrs.get("package") in _ignore_packages:
            continue                         # 手机系统 UI(状态栏/通知栏)的话不算数
        b = attrs.get("bounds")
        if b and _ZERO_BOUNDS.match(b):
            continue                         # 未渲染 → 视觉上不存在
        pos = None
        if b:
            m = _BOUNDS_RE.search(b)
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
