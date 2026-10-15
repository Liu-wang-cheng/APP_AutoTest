# -*- coding: utf-8 -*-
"""设备前置准备: 重启APP / 等待充电 / 等待地图加载 / 电量门槛。

GUI 的四个勾选项与 pytest 入口共用这里的 prepare()。
"""
import re
import time

import cv2

from core import ocr, vision
from core.driver import split_texts as _split_texts   # 多值文本拆分(与断言共用同一套规则)
from core.logger import get_logger

log = get_logger()


def get_device_id(cfg, device_option="auto"):
    """根据 --device 参数或 config.yaml 解析设备 ID"""
    if device_option and device_option != "auto":
        return device_option
    device_cfg = cfg["device"]
    if device_cfg["default"] != "auto":
        return device_cfg["default"]
    return device_cfg["list"][0]["id"]


def get_battery_level(d):
    """提取**扫地机插件**的电量百分比(如 100), 读不到返回 -1。

    ★ 真机实测(2026-09-24): SmartThings 插件页的文案是 `电池 100%%`
      (前面带「电池 」前缀, 且百分号是**双写**的)。旧实现用 `text="(\\d+)%"`
      要求文本以"数字%"开头 → 永远匹配不到, 一直返回 -1(用户报过"界面显示 -1%")。
      现在: ① 优先认带「电池/电量」字样那条里的百分比(避免进度/湿度等别的百分比
      混进来); ② 退而求其次取任意文本里的百分比(兼容 text 就是 "83%" 的旧 APP)。
    ⚠ 只读 text 属性 —— **手机自己**的电量在系统状态栏的 content-desc 里
      (`正在充电，已完成百分之 80。`), 那不是这里要的插件电量。
    """
    xml = d.dump_hierarchy()
    texts = re.findall(r'text="([^"]*)"', xml)
    for kw in ("电池", "电量"):
        hits = [int(m) for t in texts if kw in t
                for m in re.findall(r'(\d+)\s*%', t)]
        if hits:
            return max(hits)
    hits = [int(m) for t in texts for m in re.findall(r'(\d+)\s*%', t)]
    if hits:
        return max(hits)
    # ★ OCR 兜底: 插件页树稀疏时 text 里什么都没有, 但屏幕上电量的数字是可见的
    if ocr.available():
        ocr_texts = [t for t, _ in ocr.screen_texts(d)]
        for kw in ("电池", "电量"):
            hits = [int(m) for t in ocr_texts if kw in t
                    for m in re.findall(r'(\d+)\s*%', t)]
            if hits:
                return max(hits)
        hits = [int(m) for t in ocr_texts for m in re.findall(r'(\d+)\s*%', t)]
        if hits:
            log.info(f"[OCR兜底] 电量由截图识别读出: {max(hits)}%")
            return max(hits)
    return -1


# 「正在充电」的判定文本(可配置; 不同 APP/机型说法不同)
DEFAULT_CHARGING_TEXT = "充电"


def is_charging(d, text=None):
    """检查是否处于充电状态(充电中 / 正在充电 / 充电完成 ...)。

    ★ 用户要求(2026-09-24): 判定文本要能编辑, 并且**支持多文本**
      (逗号/顿号/分号/换行分隔, 任一命中即算在充电)。
    ★ 轮询里带 WebView 预热 —— 设备页是插件页, 文本要完整层级 dump 才进无障碍树。
    """
    texts = _split_texts(text) or _split_texts(DEFAULT_CHARGING_TEXT)
    xml = warm_webview(d)        # 预热一次, 下面复用 —— 否则 _any_present 内部会再 dump 一次
    return _any_present(d, texts, xml=xml)


def click_template(d, img_name, timeout=10):
    """SIFT 定位并点击模板图片,成功返回坐标,失败返回 None"""
    img_path = vision.resolve_template(img_name)
    end = time.time() + timeout
    while time.time() < end:
        screen = d.screenshot(format="opencv")
        pos = vision.find_in_gray(cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY), img_path)
        if pos:
            d.click(*pos)
            return pos
        time.sleep(2)
    return None


def _sleep(seconds, should_cancel=None):
    """可中断等待: 收到停止请求立刻返回 False(未睡满), 睡满返回 True。

    ★ 前置里的裸 `time.sleep` 会让"停止"完全失效 —— 实测最坏情况: 用户点停止后
      worker 还卡在 restart_app 的 sleep(10) / _wait_foreground 的 20s 轮询 /
      ensure_map_loaded 的 6×10s 里, 要等 1~2 分钟才响应, 用户以为卡死就去关窗口
      (那会引出线程生命周期的问题)。有 should_cancel 时以 0.2s 粒度响应取消。

    ★ 没有 should_cancel 时走**裸 time.sleep**: 既零额外开销, 也保住测试的可替换性
      (测试常把 time.sleep 换成 no-op 跳过等待; 若这里也走轮询, 时间不推进就会忙循环)。
    """
    if not should_cancel:
        time.sleep(seconds)
        return True
    end = time.time() + max(0.0, seconds)
    while True:
        if should_cancel():
            return False
        remain = end - time.time()
        if remain <= 0:
            return True
        time.sleep(min(0.2, remain))


def _wait_foreground(d, package, timeout=20, should_cancel=None):
    """轮询等待 APP 处于前台;超时返回 False(疑似闪退)"""
    end = time.time() + timeout
    while time.time() < end:
        if should_cancel and should_cancel():
            return False                # 用户请求停止: 别再等
        try:
            if d.app_current().get("package") == package:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def _heal_if_dead(d, package, should_cancel=None):
    """APP 不在前台(疑似闪退)时补拉一次;拉不起来抛 RuntimeError"""
    if _wait_foreground(d, package, timeout=5, should_cancel=should_cancel):
        return
    log.warning("[前置] APP 中途退出(疑似闪退),补一次启动")
    d.app_start(package)
    if not _wait_foreground(d, package, timeout=20, should_cancel=should_cancel):
        raise RuntimeError("APP 补拉后仍未到前台,请检查模拟器环境")


def restart_app(d, cfg, enter_page=True, should_cancel=None):
    """重启 APP 并进入目标设备页面

    带启动自愈(2026-09-17):模拟器转译层偶发"启动即闪退",检测到 APP 没到
    前台就自动补拉(最多 3 次)。正常启动时前台轮询取代固定 sleep(10),
    几乎零额外开销;只有真闪退才多花一次重启的时间。

    should_cancel: 收到停止请求时尽快退出(所有等待都走可中断的 _sleep)——
    否则这一段最坏要 90s+ 才响应"停止"。
    """
    app_cfg = cfg["app"]
    package = app_cfg["package"]
    d.app_stop(package)
    _sleep(2, should_cancel)

    started = False
    for i in range(3):
        if should_cancel and should_cancel():
            return
        d.app_start(package, app_cfg.get("main_activity"))
        if _wait_foreground(d, package, timeout=20, should_cancel=should_cancel):
            started = True
            break
        log.warning(f"[前置] APP 启动后未到前台(疑似闪退),第 {i + 1}/3 次自愈重启")
        d.app_stop(package)
        _sleep(3, should_cancel)
    if not started:
        if should_cancel and should_cancel():
            return
        raise RuntimeError(f"APP {package} 反复启动失败(疑似持续闪退),请检查模拟器")

    _sleep(8, should_cancel)            # 首页数据加载
    device_name = cfg.get("target_device", "")
    if enter_page and device_name:
        _heal_if_dead(d, package, should_cancel)   # 进设备页前:APP 中途闪退兜底
        if d(text=device_name).exists(timeout=10):
            d(text=device_name).click()
            _sleep(10, should_cancel)
            _heal_if_dead(d, package, should_cancel)   # 进设备页后:偶发闪退兜底
            log.info(f"[前置] 已进入 {device_name} 设备页面")
        else:
            log.warning(f"[前置] 未找到设备 {device_name}")


# 页面标志文本(2026-09-24 真机 dump 实测):
#   设备页底部是「设备控制 / 服务」; 列表页底部是「主页 / 设备 / 生活 / 日常程序 / 菜单」
DEVICE_PAGE_MARKS = ("设备控制", "服务")
LIST_PAGE_MARKS = ("主页", "日常程序", "收藏")
DEFAULT_DEVICE_READY_TEXT = "设备控制,服务"


def _wait_texts(d, texts, timeout, should_cancel=None):
    """在 timeout 内轮询: texts 任一出现 → 返回命中的那个; 超时返回 None。

    ★ 直接复用 `text_present` —— 它已包含 WebView 预热 + **OCR 兜底**, 这里不再
      自己拼一套只读无障碍树的判断(插件页树稀疏时那套会一直判不到)。
      每次轮询只 dump 一次, 所有候选共用(逐个 _any_present 会 dump N 次)。
    """
    end = time.time() + timeout
    round_no = 0
    while True:
        round_no += 1
        if should_cancel and should_cancel():
            return None
        xml = warm_webview(d)
        for t in texts:
            if text_present(d, [t], xml=xml, round_no=round_no):
                return t
        if time.time() >= end:
            return None
        time.sleep(1)


def _enter_device_page(d, device_name, should_cancel=None):
    """从**列表页**点设备名进设备页; 不在列表页就先退一层再试(最多 3 层)。

    ⚠ 只在"确认处于列表页"时才点: 设备页上设备名是 WebView 的标题(铺满全屏),
      盲点它会点到页面中央的按钮(如「暂停」)——真机踩过这种坑的风险很高。
    """
    if not device_name:
        return False
    for attempt in range(3):
        if should_cancel and should_cancel():
            return False
        if _any_present(d, LIST_PAGE_MARKS) and d(text=device_name).exists(timeout=5):
            d(text=device_name).click()
            _sleep(8, should_cancel)    # 等插件页加载
            return True
        if _any_present(d, DEVICE_PAGE_MARKS):
            return False                # 已经在设备页(只是没确认到), 别再乱点
        log.info(f"[前置] 当前不在设备列表页, 返回上一层再试(第 {attempt + 1}/3 次)")
        d.press("back")
        _sleep(3, should_cancel)
    return False


def ensure_device_page(d, device_name="", ready_text=DEFAULT_DEVICE_READY_TEXT,
                       timeout=30, rounds=3, should_cancel=None):
    """确保已进入设备页: **用特定文本确认**; 确认不了就重新执行进入操作。

    ★ 用户要求(2026-09-24): 进入设备页也要是一个前置条件, 而且必须靠特定文本
      确认真的进去了; 判断失败就再进一次。
      原来的实现是"点了设备名就假定成功", 实际可能还停在列表页或页面没加载完。
    ★ 单轮确认默认 30s: 插件页文本要预热才可读(见 warm_webview)。

    返回 True 已确认进入 / False 多轮后仍确认不到(按前置语义会阻断本轮执行)。
    should_cancel: 收到停止请求时尽快退出(本函数最坏耗时 110s+)
    """
    texts = _split_texts(ready_text) or _split_texts(DEFAULT_DEVICE_READY_TEXT)
    rounds = max(1, int(rounds))
    for r in range(rounds):
        if should_cancel and should_cancel():
            return False
        hit = _wait_texts(d, texts, timeout, should_cancel)
        if hit:
            log.info(f"[前置] 已确认进入设备页(命中「{hit}」)")
            return True
        log.warning(f"[前置] 第 {r + 1}/{rounds} 次未确认进入设备页, 重新执行进入操作...")
        _enter_device_page(d, device_name, should_cancel)
    log.warning(f"[前置] {rounds} 轮仍未确认进入设备页({ready_text}), 继续执行——后续步骤会暴露")
    return False


# ★ 这里曾有 plugin_page_unreadable() / device_page_readable() / PLUGIN_ACTIVITY_HINT:
#   用来判断"插件页整页文本读不出来, 就重进一次设备页"。
#   删掉的原因(2026-09-24 审查): ① 全项目零调用方, 是从未接线的死代码;
#   ② 判据本身("可见文本 < 5 条 ⇒ 页面主体没暴露")已经活在 core/ocr.py 的 sparse() 里 ——
#      text_present 遇到这种情况会直接走 OCR 兜底(截图识别), 比"重进一次再赌它能恢复"
#      更直接可靠。故删除, 别再往回加。


def ensure_charging(d, timeout=1200, on_progress=None, should_cancel=None,
                    ready_text=None, device_name="", recharge_tpl=None,
                    recharge_text=None):
    """确保设备处于充电状态(未充电则回充),默认超时 20 分钟;should_cancel 可中断等待

    ready_text:    充电判定文本(可配置, 多个用逗号/顿号分隔);留空用默认「充电」
    recharge_tpl:  回充按钮的**模板名**(按当前 APP 组的模板目录解析);
                   留空用默认「开始回充.png」
    recharge_text: 回充按钮的**文本**(可选)。填了就先用文本点, 文本命不中再落到模板。

    ★ 为什么回充按钮要能配(2026-10-11 用户要求): 各 APP 的按钮/图标不一样, 有的组
      就是拿本组的模板图去点的 —— 写死「开始回充.png」时, 换个组就得改代码。
    """
    charging = is_charging(d, ready_text)
    if charging:
        # ★ 命中信息(用户 2026-10-15): 多判定词时看得出按哪个判的(与断言留痕同思路)。
        #   判定仍以 is_charging 为准(测试桩接口不动); 这里只为日志补细节, 失败忽略。
        try:
            texts = _split_texts(ready_text) or _split_texts(DEFAULT_CHARGING_TEXT)
            xml = warm_webview(d)
            hit, _src, raw, _pos = text_present_detail(d, texts, xml=xml)
        except Exception:
            hit, raw = None, ""
        log.info("[前置] 设备处于充电状态"
                 + (f": 命中「{hit}」← {raw!r}" if hit else ""))
        return True
    tpl = (recharge_tpl or "").strip() or "开始回充.png"
    text = (recharge_text or "").strip()
    log.info(f"[前置] 设备未充电,点击回充按钮(文本:{text or '未配'}, 模板:{tpl})...")
    clicked = False
    if text:
        el = d(textContains=text)
        if el.exists(timeout=3):
            el.click()
            clicked = True
            log.info(f"[前置] 已按文本点击回充按钮「{text}」")
        else:
            log.info(f"[前置] 没找到文本「{text}」的回充按钮, 改用模板「{tpl}」")
    if not clicked:
        pos = click_template(d, tpl)
        if pos:
            clicked = True
            log.info(f"[前置] 已按模板点击回充按钮「{tpl}」@ ({pos[0]}, {pos[1]})")
        else:
            # 不直接判失败: 机器可能已经在回充路上(只是状态文本还没变), 继续等更稳
            log.warning(f"[前置] 文本/模板都没点到回充按钮(模板:{tpl}), 继续等待充电状态")
    time.sleep(2)
    if d(textContains="确认").exists(timeout=3):
        d(textContains="确认").click()
        log.info("[前置] 已点击确认弹窗")
    end = time.time() + timeout
    while time.time() < end:
        if is_charging(d, ready_text):
            log.info("[前置] 设备已进入充电状态")
            return True
        if should_cancel and should_cancel():
            log.info("[前置] 收到停止请求,中断充电等待")
            return False
        elapsed = int(time.time() - end) + timeout
        batt = get_battery_level(d)
        msg = f"等待充电中... 已等 {elapsed}s, 电量 {batt}%"
        log.info(f"[前置] {msg}")
        if on_progress:
            on_progress(msg)
        time.sleep(30)
    log.warning(f"[前置] 超时 {timeout // 60} 分钟,设备仍未进入充电状态")
    return False


# 设备页上"点一下就能清掉悬浮提示"的空白处 —— 1080x1920 实测: 点这里能清掉
# SmartThings 插件弹出的「清扫记录(未完成)」通知卡片, 且不会误触房间/按钮。
DEFAULT_BLANK_POINT = (540, 660)


def preconditions_for_group(app_group):
    """取该 APP 组要用的前置项列表, 返回 (items, source)。

    ★ 用户要求(2026-09-24): 前置条件与 APP 组绑定 —— 切到某组用例时执行那组的前置。
      取值优先级:
        1. 组目录 `Test_cases/<组>/preconditions.yaml`   → source="group"
        2. config.yaml 的全局 preconditions(老配置兜底)  → source="global"
        3. 内置 DEFAULT_PRECONDITIONS                    → source="default"
      第 2 条保证"还没分组配置过的组"沿用用户现有那套参数, 不会静默换成默认值。
    """
    from core.driver import load_group_preconditions, load_preconditions
    items = load_group_preconditions(app_group)
    if items:
        return items, "group"
    try:
        items = load_preconditions()
    except Exception:
        items = None
    if items:
        return items, "global"
    return [dict(x) for x in DEFAULT_PRECONDITIONS], "default"


def warm_webview(d):
    """触发一次完整层级请求(dump) —— WebView 内容"预热"用。

    ★ 真机实测(2026-09-24, SmartThings 插件页):
      · 插件页的文本(正在吸尘 / 电池 83%)**只有完整层级 dump 才会让它出现在
        无障碍树里**; 轻量的 `d(textContains=...).exists()` 不触发构建 ——
        于是"反复 exists 查询"30 秒也读不到(用例断言就是这么超时的)。
      · dump 频率越高越快: 每 5s dump 一次 → 25~30s 内容出现; 只 dump 一次
        然后干等 → 54s 才出现。
      所以文本类轮询(断言/等待/前置)在每次轮询里顺带 dump 一次, 既触发构建、
      又不额外增加等待。dump 失败不影响主流程(返回空串)。
    """
    try:
        return d.dump_hierarchy()
    except Exception as e:      # 设备抖动/超时都不该拖垮判断
        log.debug(f"[warm] 层级 dump 失败(忽略): {e}")
        return ""


def _parse_point(value):
    """把配置里的坐标文本解析成 (x, y);空/非法返回 None(调用方用默认点)。

    分隔符与其它多值字段一致(半角/全角逗号、顿号、分号都行) —— 中文输入法下
    很容易打出全角逗号, 不能因为分隔符不对就静默不点。
    """
    parts = _split_texts(value)
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def tap_blank(d, point=None):
    """点一下页面空白处: 清掉悬浮的提示卡片(清扫记录通知等)。

    用户要求: 这类通知会挡在页面上影响后续判断, 需要一步"点空白处"清掉。
    坐标可用前置条件里的「点击坐标」覆盖; 不填则用实测默认点(见 DEFAULT_BLANK_POINT)。
    """
    x, y = point or DEFAULT_BLANK_POINT
    d.click(int(x), int(y))
    log.info(f"[前置] 已点击空白处 ({x}, {y}) 清理页面提示")
    return True


#: 节点标签 / 属性解析(判断类要用 bounds 判断"是否真的渲染了")
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


class FaultStateError(AssertionError):
    """设备进入故障态(故障哨兵词命中) —— 等待/断言立即失败, 报错带**整段**故障原文。

    继承 AssertionError: 各处"断言失败"的处理路径(步骤失败/重试/报告)原样生效。
    """


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


def text_present_detail(d, texts, xml=None, round_no=None):
    """判断类的**详情版**: 返回 (命中的候选, 命中来源, 命中的原文, 坐标|None)。

    来源三种: "text"(节点文本) / "desc"(无障碍描述 content-desc) / "ocr"(截图识别)。
    没命中返回 (None, "", "", None)。

    ★ 只认**可见**节点(见 visible_nodes) —— 隐藏残留不算命中。
    ★ 为什么要区分来源(2026-10-11 用户报"页面根本没这些文本却判通过"):
      本函数同时扫 text 与 content-desc, 而 u2 那边 `textContains` 只看文本、
      `description` 是**精确**匹配 —— 于是会出现"u2 查不到、这里查得到", 命中的可能是
      某个控件的**无障碍描述**里的子串。不写清来源, 人只能看到"命中了", 无法判断是谁顶的。
    ★ 故障/空闲哨兵(用户 2026-10-13): 页面出现故障词 → 直接 FaultStateError(带整段
      原文); 出现空闲词 → 立即判不成立(不再轮询/OCR)。等待的文本不可能在故障/待机
      状态下出现, 傻等满轮次只会把"设备故障"掩盖成"超时"。
    """
    wanted = [t for t in (texts or []) if t]
    if not wanted:
        return None, "", "", None
    if xml is None:
        xml = warm_webview(d)
    nodes = visible_nodes(xml)
    kind, word, full = scan_status_words(nodes=nodes)   # 复用同一次解析(2026-10-13)
    if kind in ("fault", "idle"):
        # ★ 误触防护(用户 2026-10-13): 必须连续 N 轮命中才触发(默认 3)。
        #   单帧命中(如 toast / 状态栏瞬闪的"等待"字样)不做事, 计数归零。
        fp = fingerprint_xml(xml)
        n = streak_status_word(kind, True, fp)
        need = _status_words.get(f"{kind}_streak", 3)
        if n >= need:
            if kind == "fault":
                raise FaultStateError(
                    f"检测到故障状态(连续{n}次): 「{word}」← {full!r}")
            log.info(f"[状态哨兵] 设备空闲(连续{n}次:{word}), "
                     f"等待目标 {wanted} 不可能出现 → 判不成立 ← {full!r}")
            return None, "", "", None
        # 还没到阈值: 记录这一次, 不触发
        log.debug(f"[状态哨兵] {kind} 命中 {n}/{need} 次(本轮:「{word}」)—— 等等再看")
    else:
        # 本轮没命中任何状态词 → 对应类别计数清零(xml 切换时由 fingerprint 变更
        # 间接处理, 这里是同 xml 下的"不命中")
        fp = fingerprint_xml(xml)
        streak_status_word("fault", False, fp)
        streak_status_word("idle", False, fp)
    for idx, attr in ((0, "text"), (1, "desc")):        # 文本优先, 描述其次
        for node in nodes:
            raw = node[idx]
            if not raw:
                continue
            for t in wanted:
                if t in raw:
                    return t, attr, raw, node[2]
    # ★ OCR 兜底的两条路(用户定稿 2026-10-11):
    #   ① **页面主体没暴露**(树稀疏, 插件页切视图后的典型症状) → **每轮都允许** OCR ——
    #      这种页面上原生本来就什么都读不到, 不兜底等于断言必挂;
    #   ② **正常页面** → 按 **5 轮循环**兜底(前 4 轮只读无障碍树, 第 5/10/15… 轮截图识别)。
    #      正常页面多这一道的原因: 有时文字**画在屏幕上但没进无障碍树**(WebView 局部),
    #      只读树会一直判不到。代价是每 5 轮多约 1 秒。
    #   ★ 单次判断(round_no=None, 没给 timeout 的"只查一次")不受轮次限制 —— 那是唯一机会。
    if ocr.available() and (ocr.sparse(xml) or ocr.round_allows_ocr(round_no)):
        # ★ 全文哨兵(2026-10-13): 识别一次(帧缓存), **所有行**都过故障词 —— 故障弹窗
        #   画在屏上但不进无障碍树时, 只有 OCR 这条路能抓到; 哪怕等待目标没命中也要扫。
        #   ★ 同样走 streak 防护(2026-10-13 用户补充): 单帧瞬时 toast 不触发。
        lines = list(ocr.screen_texts(d))          # [(行文本, 坐标)]
        ocr_fault = None
        for line, _pos in lines:
            fw, fword = _word_in(line, _status_words["fault"])
            if fw:
                ocr_fault = (fword, line)
                break
        if ocr_fault:
            fp_ocr = fingerprint_xml(xml) + "|ocr"
            n = streak_status_word("fault", True, fp_ocr)
            need = _status_words.get("fault_streak", 3)
            if n >= need:
                raise FaultStateError(
                    f"检测到故障状态(连续{n}次,OCR): 「{ocr_fault[0]}」← {ocr_fault[1]!r}")
            log.debug(f"[状态哨兵] OCR 故障命中 {n}/{need} 次(本轮:「{ocr_fault[0]}」)")
        else:
            fp_ocr = fingerprint_xml(xml) + "|ocr"
            streak_status_word("fault", False, fp_ocr)
        for t in wanted:
            for line, pos in lines:
                if t in line:
                    log.info(f"[OCR兜底] 无障碍树里没有, 截图识别命中「{t}」"
                             f"← OCR 读到的是 {line!r}" + (f" @ {pos}" if pos else ""))
                    return t, "ocr", line, pos
    return None, "", "", None


#: 命中来源 → 日志里的中文名(用户 2026-10-11: 光写"命中了"看不出**是谁顶的**)
HIT_SOURCES = {"text": "节点文本",
               "desc": "无障碍描述(看不见的控件)",
               "ocr": "截图识别(OCR)"}


def hit_suffix(hit):
    """命中详情 → 日志后缀, 如 `命中「今天」← 来自节点文本: '今天,已取消'`。

    用户 2026-10-15: 多值条件("今天,已取消,结尾")的日志只打了整串条件,
    看不出**按哪个候选判的** —— if / wait_for / 断言多值 统一补这一句。

    hit: `(命中的候选, 来源, 命中的原文)` 或 None(没命中)。
    命中来源就是候选本身(节点文本恰好等于候选)时只写「候选」, 不啰嗦。
    """
    if not hit:
        return ""
    value, src, raw = hit
    if src == "text" and raw == value:
        return f"命中「{value}」"
    where = HIT_SOURCES.get(src, src)
    return f"命中「{value}」← 来自{where}: {raw!r}"


def text_present(d, texts, xml=None, round_no=None):
    """页面上有没有这些文本 —— **判断类统一入口**(原生优先, 树稀疏时 OCR 兜底)。

    ① 原生: 一次层级 dump, 在 **text 与 content-desc 两个属性**里做子串匹配, 且**只看
       真实可见的节点**(未渲染的隐藏节点不算 —— 否则收起的面板文案会把断言顶通过)。
    ② OCR: 仅当"原生没命中"且"页面主体没暴露"(可见文本 < 5 条, 插件页切视图后的
       典型症状)时, 截图识别一次(RapidOCR ≈1s/张, 同一帧 1.5s 缓存)。
       正常页面一次 OCR 都不做 —— 不给判断加开销。

    xml: 调用方已经 dump 过就传进来, 省一次 dump(如断言轮询里)。
    ★ 需要知道"命中来自哪儿"的调用方用 text_present_detail(断言就靠它把日志写清楚)。
    """
    return text_present_detail(d, texts, xml=xml, round_no=round_no)[0] is not None


def _any_present(d, texts, xml=None):
    """texts 里任一文本出现在屏幕上(兼容旧调用点: 转发到 text_present)。

    xml: 调用方已经 warm 过一次就传进来 —— 否则这里会**再 dump 一次**,
         轮询里等于把设备 I/O 翻倍(见各调用点的注释)。

    ⚠ 历史坑: 多值曾用 `d(textMatches="a|b")` —— Android 的 textMatches 是
      **整串匹配**(Pattern.matches), 不是子串匹配, 于是"多文本反而比单文本更差"
      (真机实测: 单值「满电」→ True, 「充电,满电」→ False)。子串语义必须自己来。
    """
    return text_present(d, texts, xml=xml)

def ensure_map_loaded(d, timeout=10, device_name="", rounds=6,
                      ready_text="地图编辑", loading_text="地图正在加载",
                      should_cancel=None):
    """等待地图加载:正常 10s 内就能加载出来;超时自动退出重进设备页面

    ★ ready_text / loading_text 可配置(GUI 前置条件里可编辑) —— 换 APP 时
      页面上的就绪/加载中文案不同, 硬编码会让检查永远不通过。
      两个字段都支持**多个文本**(逗号/顿号/分号/换行分隔):
      就绪 = 命中任一; 加载中 = 任一仍在 ⇒ 未就绪。

    每轮等 timeout 秒,没就绪就 back 退出设备页、重新点进设备页触发地图
    重新加载,最多 rounds 轮。全轮失败才放行告警(后续步骤会给出明确失败)。
    """
    ready, loading = _split_texts(ready_text), _split_texts(loading_text)
    for r in range(rounds):
        end = time.time() + timeout
        while time.time() < end:
            if should_cancel and should_cancel():
                return False
            # ★ WebView 文本预热(dump 才会让插件页文本进树); 一次 dump 供下面两个
            #   判断复用 —— 这个循环每 2s 一轮, 不复用就是每轮 3 次 dump
            xml = warm_webview(d)
            hit, _src, raw, _pos = text_present_detail(d, ready, xml=xml)
            if hit and not _any_present(d, loading, xml=xml):
                # ★ 日志要带**命中的就绪文本**(用户 2026-10-13): 只写"已加载"的话,
                #   配了多个就绪词时看不出是按哪个判的(与断言的命中留痕同一思路)
                log.info(f"[前置] 地图已加载: 命中「{hit}」← {raw!r}"
                         + (f"(第{r + 1}轮)" if r else ""))
                return True
            _sleep(2, should_cancel)
        if r < rounds - 1 and device_name:
            # 退出设备页回到列表再重新进入,触发地图重新加载
            log.warning(f"[前置] 地图 {timeout}s 未就绪,退出重进设备页面(第 {r + 1} 次)")
            d.press("back")
            _sleep(3, should_cancel)
            if d(text=device_name).exists(timeout=10):
                d(text=device_name).click()
                _sleep(5, should_cancel)
    log.warning(f"[前置] 地图 {rounds} 轮重进后仍未就绪,继续执行")
    return False


def ensure_text_check(d, wait_text="", absent_text="", timeout=60,
                      on_timeout="none", name="", on_progress=None,
                      should_cancel=None):
    """通用文本检查(用户可自定义新增的前置项): 等文本出现/消失 → 超时执行操作。

    wait_text    等待**出现**的文本(""=不要求)
    absent_text  等待**消失**的文本(""=不要求); 两者可组合(都满足才算通过)
                 两者都支持**多个文本**(逗号/顿号/分号/换行分隔):
                 等待出现 = 命中任一即可; 等待消失 = 全部都不在才算通过
    timeout      等待秒数
    on_timeout   超时后的操作: none(仅报告失败)/ back(按返回键)/ click:文本(点击该文本)
    返回 True 通过 / False 超时未满足
    """
    waits, absents = _split_texts(wait_text), _split_texts(absent_text)
    label = name or (wait_text or absent_text or "文本检查")
    if not waits and not absents:
        log.info(f"[前置] {label}: 未配置判断文本,跳过")
        return True

    def _ok(xml):
        # ★ 故障哨兵(2026-10-13): _any_present 的快速路径不经 text_present_detail
        #   → 故障词不会自动 raise。_ok 显式扫一次, 撞上走 streak 防护(2026-10-13),
        #   行为与 _assert_locator 一致, 整段原文进 reason(由 prepare_items 接住)
        kind, word, full = scan_status_words(xml=xml)
        if kind == "fault":
            fp = fingerprint_xml(xml)
            n = streak_status_word("fault", True, fp)
            need = _status_words.get("fault_streak", 3)
            if n >= need:
                raise FaultStateError(
                    f"检测到故障状态(连续{n}次): 「{word}」← {full!r}")
        else:
            fp = fingerprint_xml(xml)
            streak_status_word("fault", False, fp)
        if waits and not _any_present(d, waits, xml=xml):
            return False
        if absents and _any_present(d, absents, xml=xml):
            return False
        return True

    end = time.time() + timeout
    while time.time() < end:
        # ★ 预热一次供两个判断复用(原本最多 dump 3 次)
        xml = warm_webview(d)
        # ★ 故障哨兵命中会从这里抛 FaultStateError —— 由 prepare_items 接住,
        #   把**整段故障原文**记成该前置项的失败原因(进结果表与报告)
        if _ok(xml):
            # ★ 命中信息(用户 2026-10-15): 等待出现时写明按哪个文本判的
            hit = _any_present(d, waits, xml=xml) if waits else None
            extra = f": 命中「{hit}」" if hit else ""
            log.info(f"[前置] {label}: 条件已满足{extra}")
            return True
        if should_cancel and should_cancel():
            log.info(f"[前置] {label}: 收到停止请求")
            return False
        if on_progress:
            on_progress(f"等待 {label}...")
        time.sleep(2)
    log.warning(f"[前置] {label}: {timeout}s 内未满足条件")
    # 超时后的操作(用户配置): none=仅报告失败 / back=按返回键 / click:文本=点击该文本
    if on_timeout == "back":
        d.press("back")
        log.info(f"[前置] {label}: 超时 → 已按返回键")
    elif on_timeout.startswith("click:"):
        target = on_timeout[6:].strip()
        if target and d(textContains=target).exists(timeout=3):
            d(textContains=target).click()
            log.info(f"[前置] {label}: 超时 → 已点击「{target}」")
        else:
            log.warning(f"[前置] {label}: 超时 → 未找到可点击的「{target}」")
    return False


def ensure_battery(d, min_level=50, timeout=1800, on_progress=None, should_cancel=None):
    """确保电量达标,默认 >50%,兜底 30 分钟防止无限等待;should_cancel 可中断等待"""
    battery = get_battery_level(d)
    if battery < 0:
        log.info("[前置] 未读取到电量信息,跳过电量检查")
        return True
    if battery >= min_level:
        log.info(f"[前置] 电量 {battery}%,达标")
        return True
    log.info(f"[前置] 电量 {battery}%,不足 {min_level}%,等待充电...")
    end = time.time() + timeout
    while time.time() < end:
        if should_cancel and should_cancel():
            log.info("[前置] 收到停止请求,中断电量等待")
            return False
        time.sleep(30)
        battery = get_battery_level(d)
        if battery >= min_level or battery <= 0:
            break
    log.info(f"[前置] 电量等待结束,当前 {battery}%")
    return battery >= min_level or battery <= 0


# GUI「添加前置条件」可选的类型 → 展示名 + 可编辑参数(字段: 键/标签/默认值/类型)
PRECONDITION_TYPES = {
    "restart": {"label": "重启 APP", "params": []},
    "charging": {"label": "等待充电", "params": [
        {"key": "timeout", "label": "超时(秒)", "default": 1200, "type": "int"},
        {"key": "ready_text", "label": "充电判定文本", "default": DEFAULT_CHARGING_TEXT,
         "type": "text",
         "hint": "看到这些文本即算在充电; 多个用逗号/顿号分隔(中英文逗号均可), 任一命中即可"},
        # ★ 用户要求(2026-10-11): 回充按钮要能配 —— 各 APP/各组的按钮图标不一样,
        #   有的组就是拿**本组的模板图**去点的(模板按当前 APP 组目录解析)
        {"key": "recharge_tpl", "label": "回充按钮模板名", "default": "开始回充.png",
         "type": "text",
         "hint": "填本组 templates 目录里的模板文件名(如 开始回充.png / 回充.png); "
                 "留空 = 用默认「开始回充.png」"},
        {"key": "recharge_text", "label": "回充按钮文本(可选)", "default": "",
         "type": "text",
         "hint": "填了就**先用文本点**(如 回充); 文本命不中再落到模板。留空只用模板"}]},
    "map_load": {"label": "等待地图加载", "params": [
        {"key": "timeout", "label": "单轮超时(秒)", "default": 10, "type": "int"},
        {"key": "rounds", "label": "重试轮数", "default": 6, "type": "int"},
        {"key": "ready_text", "label": "就绪文本", "default": "地图编辑", "type": "text",
         "hint": "多个用逗号分隔, 命中任一即算就绪。如: 地图编辑,地图,清扫地图"},
        {"key": "loading_text", "label": "加载中文本", "default": "地图正在加载",
         "type": "text",
         "hint": "多个用逗号分隔, 任一仍存在即算未就绪"}]},
    "battery": {"label": "电量门槛", "params": [
        {"key": "min_level", "label": "最低电量(%)", "default": 50, "type": "int"},
        {"key": "timeout", "label": "超时(秒)", "default": 1800, "type": "int"}]},
    "enter_device": {"label": "进入设备页(按文本确认)", "params": [
        {"key": "device_name", "label": "设备名称", "default": "", "type": "text",
         "hint": "留空 = 用配置里的「设备名称」"},
        {"key": "ready_text", "label": "已进入的判定文本", "default": DEFAULT_DEVICE_READY_TEXT,
         "type": "text",
         "hint": "看到这些文本才算真的进了设备页; 多个用逗号/顿号分隔(中英文逗号均可), 任一命中即可"},
        {"key": "timeout", "label": "单轮确认超时(秒)", "default": 30, "type": "int",
         "hint": "插件页文本需要预热才可读, 建议 ≥30"},
        {"key": "rounds", "label": "判定失败重进次数", "default": 3, "type": "int"}]},
    "tap_blank": {"label": "点击空白处(清掉页面提示)", "params": [
        {"key": "point", "label": "点击坐标 x,y",
         "default": f"{DEFAULT_BLANK_POINT[0]},{DEFAULT_BLANK_POINT[1]}", "type": "text",
         "hint": "默认坐标实测可清掉设备页弹出的清扫记录通知；换 APP/分辨率时改这里(半角/全角逗号都行)"}]},
    "steps": {"label": "自定义步骤(像用例一样写步骤)", "params": [
        {"key": "name", "label": "前置条件名称", "default": "自定义步骤", "type": "text",
         "hint": "显示在执行结果/报告里"},
        {"key": "steps_yaml", "label": "步骤(YAML)", "default": "", "type": "text_area",
         "hint": "格式与用例步骤相同, 每行一条, 例如:\n- desc: 点击开始清扫\n  click: 开始清扫.png\n- desc: 等待充电\n  assert: 充电中\n  timeout: 0"}]},
    # 注: 原「自定义(检测文本→执行操作)」类型已按用户要求移除 ——
    # 有「自定义步骤」即可覆盖(等待文本可用 assert/wait_for 步骤表达)。
    # ensure_text_check 函数保留, 仅用于兼容既有配置。
}

# 默认前置项(首次使用/未配置时)
DEFAULT_PRECONDITIONS = [
    {"type": "restart", "enabled": True},
    # ★ 用户要求(2026-09-24): 进设备页要作为默认执行项 —— 并且必须靠文本确认
    #   真的进去了(重启后可能还停在列表页/页面没加载完), 确认不了就重进。
    #   排在 charging/map_load 之前: 那两项都要在设备页上操作。
    {"type": "enter_device", "enabled": True,
     "ready_text": DEFAULT_DEVICE_READY_TEXT, "timeout": 30, "rounds": 3},
    {"type": "charging", "enabled": True, "timeout": 1200},
    {"type": "map_load", "enabled": True, "timeout": 10, "rounds": 6,
     "ready_text": "地图编辑", "loading_text": "地图正在加载"},
    {"type": "battery", "enabled": True, "min_level": 50, "timeout": 1800},
]


def _run_one(d, cfg, item, on_progress, should_cancel, collect=None,
             shot_subdir=None, on_step_start=None):
    """执行单个前置项(按 type 分发);返回 True/False。

    collect: 传一个 list 进来时, 「自定义步骤」型前置会把**每一步的结果**(含截图)
    追加进去 —— GUI 好把前置内部的步骤也一条条落表/进报告(用户要求 2026-10-12)。
    shot_subdir: 内部截图目录名, 原样透传给 ActionRunner(见 prepare_items)。
    on_step_start(desc, attempt): 内部每步开始时回调(desc 带**前置项名前缀**)。
    """
    t = item.get("type")
    if t == "restart":
        restart_app(d, cfg, should_cancel=should_cancel)
        return True
    if t == "charging":
        return ensure_charging(d, timeout=int(item.get("timeout", 1200)),
                               on_progress=on_progress, should_cancel=should_cancel,
                               ready_text=item.get("ready_text"),
                               device_name=cfg.get("target_device", ""),
                               recharge_tpl=item.get("recharge_tpl"),
                               recharge_text=item.get("recharge_text"))
    if t == "enter_device":
        return ensure_device_page(
            d,
            device_name=item.get("device_name") or cfg.get("target_device", ""),
            ready_text=item.get("ready_text") or DEFAULT_DEVICE_READY_TEXT,
            timeout=int(item.get("timeout", 30)),
            rounds=int(item.get("rounds", 3)),
            should_cancel=should_cancel)
    if t == "map_load":
        return ensure_map_loaded(
            d, timeout=int(item.get("timeout", 10)),
            device_name=cfg.get("target_device", ""),
            rounds=int(item.get("rounds", 6)),
            ready_text=item.get("ready_text") or "地图编辑",
            loading_text=item.get("loading_text") or "地图正在加载",
            should_cancel=should_cancel)
    if t == "battery":
        return ensure_battery(d, min_level=int(item.get("min_level", 50)),
                              timeout=int(item.get("timeout", 1800)),
                              on_progress=on_progress, should_cancel=should_cancel)
    if t == "tap_blank":
        return tap_blank(d, _parse_point(item.get("point")))
    if t == "steps":
        # 前置条件也可以是「一串用例步骤」(用户要求: 像用例一样写步骤)
        steps = item.get("steps") or []
        if not steps:
            log.info(f"[前置] {item.get('name') or '自定义步骤'}: 未配置步骤,跳过")
            return True
        from core.runner import ActionRunner
        runner = ActionRunner(d, (cfg.get("runner") or {}),
                              case_name=item.get("name") or "前置步骤",
                              # ★ 与正式用例保持一致: 漏传 device_name 时, 插件页整页
                              #   读不出文本的"重进设备页"就用不了 —— 同一条断言在
                              #   前置里失败、在用例里却能过(实测 device_name 为空串)。
                              device_name=cfg.get("target_device", ""),
                              # ★ 让"停止"能穿透到这串前置步骤(见 ActionRunner.stopped):
                              #   否则用户点停止后它会一路跑完
                              cancel_check=should_cancel)
        if shot_subdir:
            runner.shot_subdir = shot_subdir   # 截图进本轮的「用例名-第N次」目录(见上)
            runner.shot_prefix = "前置"        # 与用例步骤同目录, 靠前缀避免同名覆盖
        # ★ 每一步都留截图(用户 2026-10-13: "还有一些前置的自定义步骤没有截图"):
        #   以前只有点击类/显式 screenshot 的步骤才截, wait/assert/if 这些子步骤
        #   什么都没有 —— 与"前置也默认截图"的初衷(2.0.6)相悖。
        runner.always_screenshot = True
        # ★ 子步骤耗时(用户 2026-10-13: "前置执行时的if子步骤为何没有算耗时"):
        #   用例路径的耗时是 runner_thread 的 on_result 包装算的, 前置 runner 原来没有
        #   —— collect 到的原始结果没有 elapsed 字段。results 里存的就是传回调的
        #   **同一个 dict**(core/runner.py 在 append 之后回调), 原地盖章即可。
        last_ts = {"t": time.time()}

        def _on_step_start(desc, attempt=1):
            last_ts["t"] = time.time()          # 耗时从"本步开始"算(与用例路径同语义)
            if on_step_start:
                try:
                    # ★ 前缀必须用 _item_label(带"自定义步骤(1步)"的计数后缀) ——
                    #   完成行的 desc 就是它(_emit_pre_result), 两边对不上,
                    #   界面的「执行中」行就地改 PASS 就会失效变成重复行
                    on_step_start(f"{_item_label(item)}: {desc}", attempt)
                except Exception as e:          # 界面回调异常不影响前置执行
                    log.debug(f"[前置] 步骤开始回调异常(忽略): {e}")

        def _on_result(r):
            now = time.time()
            r["elapsed"] = round(now - last_ts["t"], 1)
            last_ts["t"] = now
        runner.on_step_start = _on_step_start
        runner.on_result = _on_result
        ok = runner.run_steps(steps)
        if collect is not None:
            collect.extend(runner.results)      # 内部每一步的结果(含截图)
        log.info(f"[前置] {item.get('name') or '自定义步骤'}: "
                 f"{'通过' if ok else '有步骤失败'}")
        return ok
    if t == "text_check":
        return ensure_text_check(
            d, wait_text=item.get("wait_text") or "",
            absent_text=item.get("absent_text") or "",
            timeout=int(item.get("timeout", 60)),
            on_timeout=item.get("on_timeout") or "none",
            name=item.get("name") or "", on_progress=on_progress,
            should_cancel=should_cancel)
    log.warning(f"[前置] 未知类型: {t}, 跳过")
    return None


def prepare_items(d, cfg, items, on_progress=None, should_cancel=None,
                  on_item_done=None, shot_subdir=None, on_step_start=None):
    """按前置项列表依次执行 → [{"key","desc","ok","elapsed"(,"reason")}]

    key 供报告/结果去重(同类型多实例带序号), desc 是展示名(含参数摘要),
    elapsed 是该项自己的耗时(秒); reason 是失败原因(如故障整段原文, 2026-10-13)。

    shot_subdir: 「自定义步骤」型前置的内部截图目录名(screenshots/ 下) ——
    不给则退回用前置名当目录(每轮同路径互相覆盖, 且封顶清理不认, 审查 2026-10-12)。

    on_step_start(desc, attempt): 「自定义步骤」型前置的**每一步开始**时回调
    (desc 已带前置项名前缀) —— GUI 用它插"执行中"行(2026-10-13, 与用例同体验)。

    ★ on_item_done(result): **每执行完一条立刻回调** —— GUI 用它把结果逐条落表,
      用户要求"一条一条显示结果和耗时, 而不是全部执行完再一次性刷出来"。
    """
    results = []

    def _finish(idx, desc, ok, elapsed=0.0, sub=None, reason=None, skipped=False):
        r = {"key": f"pre{idx}", "desc": desc, "ok": bool(ok), "elapsed": elapsed,
             "sub": list(sub or [])}
        if reason:
            r["reason"] = reason     # 失败原因(如故障原文) → 结果表/报告/阻断详情可见
        if skipped:
            # ★ "未执行" ≠ "失败"(用户 2026-10-15: 点停止后剩下的前置项全显示 FAIL):
            #   用户停止时还没轮到的项根本没跑, 结果表按**中性**显示, 也不计入阻断
            r["skipped"] = True
        results.append(r)
        if on_item_done:
            try:
                on_item_done(r)
            except Exception as e:      # 回调出错不能影响前置执行本身
                log.warning(f"[前置] 结果回调异常(忽略): {e}")
        return r

    for i, item in enumerate(items or []):
        if not item.get("enabled", True):
            continue
        desc = _item_label(item)
        if should_cancel and should_cancel():
            _finish(i, desc, False, skipped=True)   # 未执行(用户停止), 不是失败
            continue
        t0 = time.time()
        sub = []
        try:
            ok = _run_one(d, cfg, item, on_progress, should_cancel, collect=sub,
                          shot_subdir=shot_subdir, on_step_start=on_step_start)
        except FaultStateError as e:
            # ★ 故障哨兵(2026-10-13): 设备报故障 → 该前置项立即失败, **原因(整段
            #   原文)进结果与报告**, 不再只有干巴巴的"超时或失败"
            log.warning(f"[前置] {desc}: {e}")
            _finish(i, desc, False, time.time() - t0, sub=sub, reason=str(e))
            continue
        if ok is None:
            continue                    # 该项跳过(如未配置文本)
        _finish(i, desc, ok, time.time() - t0, sub=sub)
    return results


def _item_label(item):
    """前置项展示名(带关键参数, 便于报告里区分同类型多实例)"""
    t = item.get("type")
    if t == "steps":
        base = item.get("name") or "自定义步骤"
        n = len(item.get("steps") or [])
        return f"{base}({n}步)"
    if t == "text_check":
        base = item.get("name") or "文本检查"
        conds = []
        if item.get("wait_text"):
            conds.append(f"出现「{item['wait_text']}」")
        if item.get("absent_text"):
            conds.append(f"消失「{item['absent_text']}」")
        return base + ("(" + "+".join(conds) + ")" if conds else "")
    labels = {"restart": "重启 APP", "charging": "等待充电",
              "map_load": "地图加载", "battery": "电量门槛",
              "tap_blank": "点击空白处", "enter_device": "进入设备页"}
    base = labels.get(t, str(t))
    extra = ""
    if t == "battery":
        extra = f"≥{item.get('min_level', 50)}%"
    elif t == "map_load" and item.get("ready_text"):
        extra = f"({item['ready_text']})"
    elif t == "charging":
        bits = []
        if item.get("timeout"):
            bits.append(f"{int(item['timeout']) // 60}分钟")
        if item.get("recharge_text"):
            bits.append(f"文本:{item['recharge_text']}")
        if item.get("recharge_tpl"):            # 只显示**自己配过**的模板名(默认不占地方)
            bits.append(f"模板:{item['recharge_tpl']}")
        extra = f"({', '.join(bits)})" if bits else ""
    elif t == "tap_blank":
        x, y = _parse_point(item.get("point")) or DEFAULT_BLANK_POINT
        extra = f"({x},{y})"
    elif t == "enter_device":
        extra = f"确认文本: {item.get('ready_text') or DEFAULT_DEVICE_READY_TEXT}"
    return f"{base}{extra}"


def prepare(d, cfg, restart=True, charging=True, map_load=True, battery=True,
            on_progress=None, should_cancel=None):
    """完整前置流程,按需组合(GUI 勾选项 / pytest 全开);should_cancel 支持中途取消

    返回 {restart, charging, map_load, battery} → True 通过 / False 未通过(超时等)
    / None 未勾选(不检查)。返回值供 GUI 写入执行结果与报告(conftest 忽略)。
    """
    results = {"restart": None, "charging": None, "map_load": None, "battery": None}
    if restart:
        restart_app(d, cfg)
        results["restart"] = True
    if should_cancel and should_cancel():
        # 中断时只有「勾选了但还没执行到」的项算未完成(False);未勾选保持 None
        for key, enabled in (("charging", charging), ("map_load", map_load),
                             ("battery", battery)):
            if enabled and results[key] is None:
                results[key] = False
        return results
    if charging:
        results["charging"] = ensure_charging(d, on_progress=on_progress,
                                              should_cancel=should_cancel)
    if map_load:
        results["map_load"] = ensure_map_loaded(d, device_name=cfg.get("target_device", ""))
    if battery:
        results["battery"] = ensure_battery(d, on_progress=on_progress,
                                            should_cancel=should_cancel)
    return results
