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
    # ★ 走 visible_nodes(而不是自己裸扫 text=): 包名与可见性都要过滤(用户 2026-10-16 那一类
    #   问题的同源漏洞) —— 手机状态栏/通知栏里的「电池 20%」不算机器人的电量, 收起面板留下的
    #   隐藏节点也不算。仍**只读 text 属性**(与原来一致): 手机自己的电量在 content-desc 里,
    #   这里要的是插件页的文本。
    from core.sentinel import visible_nodes
    texts = [t for t, _d, _p in visible_nodes(xml) if t]
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
        # ★ 识别前抹掉系统栏(同 2.0.25): 否则手机状态栏那个「93%」会被当机器人的电量
        from core.sentinel import system_ui_regions
        ocr_texts = [t for t, _ in ocr.screen_texts(
            d, exclude=system_ui_regions(xml))]
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


#: 前置检测的**执行间隔**(秒) —— 用户 2026-10-16: "每个前置检测的执行间隔设置成默认5s"。
#: 两个地方都用它:
#:   ① 前置项之间: 上一项结束 → 等这么久再跑下一项(设备页/插件页要时间稳定下来);
#:   ② 等待型检查(等待充电 / 电量门槛)的**轮询间隔** —— 原来写死 30s: 设备刚回到充电座
#:      也要等半分钟才被发现, 日志里也只看到每 30s 一条, "它到底在等什么"看不出来。
#: 每个前置项都能在自己的参数里改(见 PRECONDITION_COMMON_PARAMS)。
PRECONDITION_INTERVAL_DEFAULT = 5

#: 回充按钮模板的**最低可信分** —— 低于它就多半点错了按钮(见 ensure_charging 的预警)。
#: 引擎阈值是 0.62(命中即算), 但"刚好压线"的命中在真机上就是"点错按钮":
#: 用户 2026-10-16 的日志里回充模板 0.62 命中在"启动"按钮上, 机器人反而继续清扫。
RECHARGE_MIN_SCORE = 0.75


def _safe_battery(d):
    """读电量(只给日志留痕用): 读不到返回 -1, **绝不**因为读电量把前置搞崩"""
    try:
        return get_battery_level(d)
    except Exception as e:
        log.debug(f"[前置] 读电量失败(忽略): {e}")
        return -1


def page_sample(d, xml=None, limit=10):
    """页面上现在有哪些文本(给"未命中"留证据);取不到返回 []。

    ★ 为什么要它(用户 2026-10-16): "我给了 充电,满电 两个文本为何都识别不到" ——
      只写"未命中"的话, 人只能自己去复现; 把**当时页面上的文本**记下来, 一眼就能看出
      "那会儿页面显示的是『正在清洁』, 根本没在充电", 而不是"文本配置写错了"。

    ★ 与 `runner` 的 [FAIL诊断]「当前页面文本」**故意不同**: 那边不过滤系统 UI(排查时
      要全屏都看到了什么); 这里要过滤 —— 手机状态栏的「正在充电，已完成百分之 97」要是
      混进来, 恰好会把"机器人到底在不在充电"这件事看反(那正是这条日志要回答的问题)。
    """
    out, seen = [], set()

    def _add(v):
        v = (v or "").strip()
        if v and v not in seen:
            seen.add(v)
            out.append(v)

    try:
        xml = warm_webview(d) if xml is None else xml
        for text, desc, _pos in visible_nodes(xml):
            _add(text)
            _add(desc)
            if len(out) >= limit:
                return out
        # ★ 无障碍树里没几条文本时补一次截图识别(插件页/WebView 常见): 只看树的话,
        #   日志可能只写得出"页面文本: []" —— 那等于没留证据(OCR 兜底本来就在文本
        #   判定链路上, 见 text_present 的 sparse 处理)。
        if len(out) < 3:
            try:
                from core import ocr
                from core.sentinel import system_ui_regions
                for t, _pos in ocr.screen_texts(
                        d, exclude=system_ui_regions(xml)):
                    _add(t)
                    if len(out) >= limit:
                        break
            except Exception as e:      # OCR 兜底拿不到就算了(它只是日志增强)
                log.debug(f"[前置] 页面文本 OCR 取样失败(忽略): {e}")
    except Exception as e:              # 取样只是日志增强, 不能影响前置本身的判定
        log.debug(f"[前置] 页面文本取样失败(忽略): {e}")
    return out


def click_template(d, img_name, timeout=10, should_cancel=None, detail=None):
    """SIFT 定位并点击模板图片,成功返回坐标,失败返回 None

    ★ should_cancel(结构守护 2026-10-15): 这里原来用**裸 time.sleep(2)** 轮询 ——
      前置里点模板按钮(回充/开始清扫等)时按「停止」, 得等满 timeout 才响应。本模块
      10 行之下就有可中断的 `_sleep(seconds, should_cancel)`, 之前漏用了。
    ★ detail(用户 2026-10-16): 传入一个 dict 时, 把**匹配分/尺度**填进去 —— 用户日志里
      "点了回充却一直在放电"这类问题, 只看坐标看不出是"点错了按钮"; 有分数才看得出
      (实测: 回充模板 0.62 分命中在"启动"按钮上, 0.62 就是引擎阈值本身, 一点余量都没有)。
    """
    img_path = vision.resolve_template(img_name)
    end = time.time() + timeout
    while time.time() < end:
        if should_cancel and should_cancel():
            log.info("[前置] 收到停止请求, 中止模板点击")
            return None
        screen = d.screenshot(format="opencv")
        pos = vision.find_in_gray(cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY), img_path,
                                  detail=detail)
        if pos:
            d.click(*pos)
            return pos
        if not _sleep(2, should_cancel):      # 未睡满 = 期间收到停止
            return None
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
    # ★ 未命中留痕(用户 2026-10-16): 查了哪些确认文本 + 当时页面上有什么
    log.warning(f"[前置] {rounds} 轮仍未确认进入设备页(确认文本「{'、'.join(texts)}」"
                f"都未出现) ← 当前页面文本: {page_sample(d)}, 继续执行——后续步骤会暴露")
    return False


# ★ 这里曾有 plugin_page_unreadable() / device_page_readable() / PLUGIN_ACTIVITY_HINT:
#   用来判断"插件页整页文本读不出来, 就重进一次设备页"。
#   删掉的原因(2026-09-24 审查): ① 全项目零调用方, 是从未接线的死代码;
#   ② 判据本身("可见文本 < 5 条 ⇒ 页面主体没暴露")已经活在 core/ocr.py 的 sparse() 里 ——
#      text_present 遇到这种情况会直接走 OCR 兜底(截图识别), 比"重进一次再赌它能恢复"
#      更直接可靠。故删除, 别再往回加。


def ensure_charging(d, timeout=1200, on_progress=None, should_cancel=None,
                    ready_text=None, device_name="", recharge_tpl=None,
                    recharge_text=None, interval=PRECONDITION_INTERVAL_DEFAULT):
    """确保设备处于充电状态(未充电则回充),默认超时 20 分钟;should_cancel 可中断等待

    ready_text:    充电判定文本(可配置, 多个用逗号/顿号分隔);留空用默认「充电」
    recharge_tpl:  回充按钮的**模板名**(按当前 APP 组的模板目录解析);
                   留空用默认「开始回充.png」
    recharge_text: 回充按钮的**文本**(可选)。填了就先用文本点, 文本命不中再落到模板。
    interval:      轮询间隔(秒, 默认 5) —— 见 PRECONDITION_INTERVAL_DEFAULT。

    ★ 为什么回充按钮要能配(2026-10-11 用户要求): 各 APP 的按钮/图标不一样, 有的组
      就是拿本组的模板图去点的 —— 写死「开始回充.png」时, 换个组就得改代码。

    ★ 留痕(用户 2026-10-16): "未命中"也要说清**查了哪些词、页面上当时有什么**; 等超时
      还要说清**电量是升是降**(不升反降 = 回充根本没生效, 不是"状态文本没更新")。
      原来只写一句"超时 20 分钟", 用户只能猜是设备问题还是模板问题。
    """
    texts = _split_texts(ready_text) or _split_texts(DEFAULT_CHARGING_TEXT)
    charging = is_charging(d, ready_text)
    if charging:
        # ★ 命中信息(用户 2026-10-15): 多判定词时看得出按哪个判的(与断言留痕同思路)。
        #   判定仍以 is_charging 为准(测试桩接口不动); 这里只为日志补细节, 失败忽略。
        try:
            xml = warm_webview(d)
            hit, _src, raw, _pos = text_present_detail(d, texts, xml=xml)
        except Exception:
            hit, raw = None, ""
        log.info("[前置] 设备处于充电状态"
                 + (f": 命中「{hit}」← {raw!r}" if hit else ""))
        return True
    # ★ 未命中也要留痕(用户 2026-10-16): 查了哪些判定词 + 页面上真正有什么文本。
    #   只写"未充电"时, "我给了 充电,满电 两个词为何都识别不到" 只能靠人复现去猜。
    try:
        sample = page_sample(d)
        log.info(f"[前置] 未命中充电判定文本「{'、'.join(texts)}」"
                 f"(判定: 任一命中即算充电) ← 当前页面文本: {sample}")
    except Exception as e:
        log.debug(f"[前置] 页面文本取样失败(忽略): {e}")
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
        info = {}
        pos = click_template(d, tpl, should_cancel=should_cancel, detail=info)
        if pos:
            clicked = True
            score, scale = info.get("score"), info.get("scale")
            extra = (f", 匹配分 {score:.2f}"
                     + (f"@尺度 {scale:.2f}x" if scale else "")) if score is not None else ""
            log.info(f"[前置] 已按模板点击回充按钮「{tpl}」@ ({pos[0]}, {pos[1]}){extra}")
            # ★ 低置信度预警(用户 2026-10-16 的真实事故): 那台设备的回充模板只有 0.62 分
            #   (== 引擎阈值, 零余量)、还要放大 1.2 倍才命中 —— 结果点在了"启动"按钮上,
            #   机器人没去回充反而继续清扫(电量 45%→34%), 而日志看不出来。
            if score is not None and score < RECHARGE_MIN_SCORE:
                log.warning(
                    "[前置] 回充按钮**匹配置信度低**(%.2f < %.2f) —— 很可能点错了按钮; "
                    "请在该设备上用用例编辑区的「裁剪模板」重截「%s」"
                    "(把整个按钮盘连图标一起裁进去)", score, RECHARGE_MIN_SCORE, tpl)
        else:
            # 不直接判失败: 机器可能已经在回充路上(只是状态文本还没变), 继续等更稳
            log.warning(f"[前置] 文本/模板都没点到回充按钮(模板:{tpl}), 继续等待充电状态")
    time.sleep(2)
    if d(textContains="确认").exists(timeout=3):
        d(textContains="确认").click()
        log.info("[前置] 已点击确认弹窗")
    end = time.time() + timeout
    start_batt = _safe_battery(d)              # 起点电量(超时时用来说明"到底有没有回充")
    hinted = False
    last_batt, last_report = None, 0
    while time.time() < end:
        if is_charging(d, ready_text):
            log.info("[前置] 设备已进入充电状态"
                     + (f"(命中判定文本「{ready_text or DEFAULT_CHARGING_TEXT}」)"
                        if ready_text else ""))
            return True
        if should_cancel and should_cancel():
            log.info("[前置] 收到停止请求,中断充电等待")
            return False
        elapsed = int(time.time() - end) + timeout
        batt = get_battery_level(d)
        # ★ 同"电量门槛": 只在**电量变化**时打点(用户 2026-10-16), 但那一行照旧带等待时间
        #   (`已等 {elapsed}s, 电量 {batt}%`)。轮询间隔已经缩到 5s(见
        #   PRECONDITION_INTERVAL_DEFAULT), 再按时间打点会 12 行/分钟地刷屏。
        #   读不到电量(<0)时退回按时间打点, 否则等待期会一片空白。
        if (batt != last_batt) or (batt < 0 and elapsed - last_report >= 30):
            last_batt, last_report = batt, elapsed
            msg = f"等待充电中... 已等 {elapsed}s, 电量 {batt}%"
            log.info(f"[前置] {msg}")
            if on_progress:
                on_progress(msg)
        # 点了回充 1 分钟还没充上 → 提示"可能没点中"(一次就够, 别刷屏)
        if not hinted and clicked and elapsed >= 60:
            hinted = True
            log.warning(f"[前置] 已点击回充按钮 {elapsed}s 仍未进入充电状态 —— "
                        f"按钮可能没点中/点错(模板「{tpl}」); 可继续等待, 也可按停止后"
                        f"用「裁剪模板」重截该模板")
        time.sleep(max(1, int(interval)))
    batt_end = _safe_battery(d)
    log.warning(f"[前置] 超时 {timeout // 60} 分钟,设备仍未进入充电状态"
                f"(判定文本「{'、'.join(texts)}」始终未出现)")
    if start_batt >= 0 and batt_end >= 0:
        if batt_end < start_batt:
            log.warning(f"[前置] 等待期间电量 {start_batt}% → {batt_end}%(不升反降) —— "
                        f"回充**没有生效**: 机器人多半还在清扫/执行任务。请确认回充按钮"
                        f"(模板「{tpl}」)在当前状态下点对了位置, 或用「裁剪模板」重截")
        elif batt_end >= start_batt:
            log.warning(f"[前置] 等待期间电量 {start_batt}% → {batt_end}%(未下降) —— "
                        f"回充可能已生效但状态文本没更新; 建议把该状态的文案加进"
                        f"「充电判定文本」(当前: {'、'.join(texts)})")
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
# ── 设备状态哨兵(词表/连续计数/可见节点扫描)已抽到 core/sentinel.py(结构整理 2026-10-15):
#    纯逻辑一整块搬走, 这里 re-export 保持 `session.X` 的老用法不变 ──
from core.sentinel import (
    _NODE_ATTR_RE, _NODE_RE, _ZERO_BOUNDS, DEFAULT_FAULT_STREAK,
    DEFAULT_FAULT_WORDS, DEFAULT_IDLE_STREAK, DEFAULT_IDLE_WORDS, _clamp_streak,
    fingerprint_xml, reset_status_words, scan_status_words, set_status_words,
    _status_words, _streak_counters, streak_status_word, visible_nodes, _word_in,
)

# ★ 转出去的名字要"被用一次", 静态检查 (pyflakes) 才不当它是笔误(它不认 noqa;
#   2026-10-16 之前 CI 的静态检查就是被这一行绊红的)。列进 `__all__` 是 Python 里
#   表达"这些是本模块对外提供的名字"的标准写法。
#   注: 本模块的 `__all__` 只列这批**转出去的**名字 —— 所以别在本项目里写
#   `from core.session import *`(tests/test_gui_structure_guard.py 的
#   test_no_star_imports_anywhere 会把 `import *` 直接拦下)。
__all__ = ["_NODE_ATTR_RE", "_NODE_RE", "_ZERO_BOUNDS", "DEFAULT_FAULT_STREAK",
           "DEFAULT_FAULT_WORDS", "DEFAULT_IDLE_STREAK", "DEFAULT_IDLE_WORDS",
           "_clamp_streak", "fingerprint_xml", "reset_status_words",
           "scan_status_words", "set_status_words", "_status_words",
           "_streak_counters", "streak_status_word", "visible_nodes", "_word_in"]


class FaultStateError(AssertionError):
    """设备进入故障态(故障哨兵词命中) —— 等待/断言立即失败, 报错带**整段**故障原文。

    继承 AssertionError: 各处"断言失败"的处理路径(步骤失败/重试/报告)原样生效。
    """


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
        # ★ 排掉**手机系统 UI**上的字(用户 2026-10-15): 树侧已按包名排除, 但 OCR 读的是
        #   像素、没有包名可依 —— 手机状态栏/通知栏上"看得见"的字(通知横幅、锁屏提示、
        #   电量文案)只能按坐标排。判据用**实测**的系统 UI 矩形(不是猜比例), 且只有整行
        #   落在矩形内才丢(压线的不动) —— 见 sentinel.system_ui_rects/drop_system_ui_lines。
        from core.sentinel import drop_system_ui_lines, system_ui_regions
        _regions = system_ui_regions(xml)
        # ★ 识别**之前**就把系统栏抹黑(用户 2026-10-16: "应该把手机系统那一段排除掉识别
        #   才对") —— 事后过滤只是兜底: 抹黑后那一段压根不参与识别, 日志里也不会出现
        #   "系统栏的字命中了什么"
        _boxes_all = list(ocr.screen_boxes(d, exclude=_regions))
        _boxes = drop_system_ui_lines(_boxes_all, _regions)
        if len(_boxes) != len(_boxes_all):
            _lost = [t for t, b in _boxes_all if (t, b) not in _boxes]
            log.info(f"[OCR] 忽略手机系统 UI 内的 {len(_lost)} 行识别结果: {_lost}")
        lines = [(t, (b[0], b[1])) for t, b in _boxes]     # 后续按 (行文本, 左上角) 用
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
    # ★ 未命中留痕(用户 2026-10-16: "前置检测的测试项中还有哪些没加命中和未命中信息"):
    #   说清查了哪些就绪/加载中文本 + 当时页面上有什么文本 —— 只写"未就绪"没法判断是
    #   文案配错了、还是页面真没加载出来。
    log.warning(f"[前置] 地图 {rounds} 轮重进后仍未就绪(就绪文本「{'、'.join(ready)}」"
                f"都未出现, 加载中文本「{'、'.join(loading)}」)"
                f" ← 当前页面文本: {page_sample(d)}, 继续执行")
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
    # ★ 未命中留痕(用户 2026-10-16): 说清查了哪些文本 + 当时页面上有什么
    want = []
    if waits:
        want.append("等待出现「" + "、".join(waits) + "」")
    if absents:
        want.append("等待消失「" + "、".join(absents) + "」")
    log.warning(f"[前置] {label}: {timeout}s 内未满足条件({'; '.join(want)})"
                f" ← 当前页面文本: {page_sample(d)}")
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


def ensure_battery(d, min_level=50, timeout=1800, on_progress=None, should_cancel=None,
                   interval=PRECONDITION_INTERVAL_DEFAULT):
    """确保电量达标,默认 >50%,兜底 30 分钟防止无限等待;should_cancel 可中断等待

    interval: 轮询间隔(秒, 默认 5, 见 PRECONDITION_INTERVAL_DEFAULT)。
    ★ 等待过程要**可见**(用户 2026-10-16): 原来只有开头一句"不足 50%,等待充电..." +
      结尾一句 —— 用户日志里那 6 分钟是一片空白, 分不清"在等"还是"卡死"。
    """
    battery = get_battery_level(d)
    if battery < 0:
        log.info("[前置] 未读取到电量信息,跳过电量检查")
        return True
    if battery >= min_level:
        log.info(f"[前置] 电量 {battery}%,达标")
        return True
    start_batt = battery
    log.info(f"[前置] 电量 {battery}%,不足 {min_level}%,等待充电...")
    end = time.time() + timeout
    last_batt, last_report = None, 0.0
    while time.time() < end:
        if should_cancel and should_cancel():
            log.info("[前置] 收到停止请求,中断电量等待")
            return False
        time.sleep(max(1, int(interval)))
        battery = get_battery_level(d)
        if battery >= min_level or battery <= 0:
            break
        waited = int(timeout - (end - time.time()))
        # ★ 只在**电量变化**时打点(用户 2026-10-16: "电量等待的打印太频繁了, 要按电量变化
        #   才打印, 不要按等待时间" —— 但"等待时间也要同步打印出来")。所以还是同一行:
        #   `已等 {waited}s, 电量 {battery}%`, 只是**触发时机**改成电量变了才打。
        #   读不到电量(<0)时退回按时间打点: 那种情况下"电量变化"永远等不到, 会一片空白。
        if (battery != last_batt) or (battery < 0 and waited - last_report >= 30):
            last_batt, last_report = battery, waited
            msg = f"等待电量达标({min_level}%)... 已等 {waited}s, 电量 {battery}%"
            log.info(f"[前置] {msg}")
            if on_progress:
                on_progress(msg)
    log.info(f"[前置] 电量等待结束,当前 {battery}%")
    if battery < min_level and battery > 0 and battery < start_batt:
        log.warning(f"[前置] 等待期间电量 {start_batt}% → {battery}%(不升反降) —— "
                    f"设备没在充电, 请检查回充是否成功")
    return battery >= min_level or battery <= 0


#: 每个前置项都带的**通用参数**(GUI 的编辑框里跟在类型参数后面一起显示)。
#: 用户 2026-10-16: "每个前置检测的执行间隔设置成默认5s" + "超时了也没暂停用例也没弹窗
#: 提醒, 还会继续后面的前置检测" —— 前者落到 interval, 后者落到 soft。
PRECONDITION_COMMON_PARAMS = [
    {"key": "interval", "label": "执行间隔(秒)", "default": PRECONDITION_INTERVAL_DEFAULT,
     "type": "int",
     "hint": "本项**开始前**的等待(第一项不加), 也是等待型检查(等待充电/电量门槛)的"
             "轮询间隔; 默认 5"},
    {"key": "soft", "label": "失败不阻断(继续后面的前置)", "default": False, "type": "bool",
     "hint": "默认不勾: 本项未通过就停下来等你处理(弹窗: 重试/取消), 后面的前置项不再执行"},
]


def item_interval(item):
    """前置项的执行间隔(秒, 默认 5) —— 用户可改;<=0 表示不等"""
    try:
        v = int(float(item.get("interval", PRECONDITION_INTERVAL_DEFAULT)))
    except (TypeError, ValueError):
        v = PRECONDITION_INTERVAL_DEFAULT
    return max(0, v)


def item_is_soft(item):
    """本项失败是否"不阻断"(继续跑后面的前置项)?

    默认按**类型**给: 进入设备页/地图加载这两个从设计上就是"确认不了也继续执行 ——
    后续步骤会暴露"(见 ensure_device_page/ensure_map_loaded 的注释), 所以它们默认不阻断,
    免得把原来能跑完的流程改成"卡在弹窗上等人"。其余类型默认阻断(用户 2026-10-16 要求)。
    """
    if "soft" in item:
        return bool(item.get("soft"))
    spec = PRECONDITION_TYPES.get(item.get("type")) or {}
    return bool(spec.get("soft_default", False))


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
    # soft_default: 见 item_is_soft() —— 这两项历史上就是"确认不了也继续执行"
    "map_load": {"label": "等待地图加载", "soft_default": True, "params": [
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
    "enter_device": {"label": "进入设备页(按文本确认)", "soft_default": True, "params": [
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
                               recharge_text=item.get("recharge_text"),
                               interval=item_interval(item))
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
                              on_progress=on_progress, should_cancel=should_cancel,
                              interval=item_interval(item))
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
                    # ★ 前缀必须用 item_label(带"自定义步骤(1步)"的计数后缀) ——
                    #   完成行的 desc 就是它(_emit_pre_result), 两边对不上,
                    #   界面的「执行中」行就地改 PASS 就会失效变成重复行
                    on_step_start(f"{item_label(item)}: {desc}", attempt)
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

    on_step_start(desc, attempt): **开始执行**时回调, GUI 用它插「执行中」行
    (2026-10-13, 与用例同体验)。两处会调:
      · 每一条**非**「自定义步骤」型前置项开始跑时 —— desc 就是 `item_label(item)`
        (2026-10-16 补: 原来只有自定义步骤型会吭声, 电量门槛这种"等几十分钟"的项
        期间结果表一片空白, 看着像卡死);
      · 「自定义步骤」型前置**内部每一步**开始时 —— desc 形如 `自定义步骤(2步): 点击X`
        (父项自己那行不在此列, 由完成时的结果行给出, 避免多出一行)。

    ★ on_item_done(result): **每执行完一条立刻回调** —— GUI 用它把结果逐条落表,
      用户要求"一条一条显示结果和耗时, 而不是全部执行完再一次性刷出来"。

    ★ 第一项未通过就**停**: (用户 2026-10-16 实际事故) "等待充电"超时后, 后面的
      "电量门槛"接着跑了 6 分钟, 直到人手动按停止 —— 前置项之间是有依赖的
      (没充上电, 后面的电量门槛必然也是白等), 继续跑只会把失败拖长、把日志搅浑。
      现在: 某条未通过且**不是**"失败不阻断"(soft)的项 → 后面所有项记成"未执行"
      (中性, 与"停止后剩下的项"同一套语义)并立刻返回, 由调用方弹窗/暂停处理。
      个别项确实"失败了也无所谓"时, 在它自己的参数里勾「失败不阻断」。
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

    blocked = None                      # 已有一条未通过且不"失败不阻断" → 后面的都不跑
    ran = 0                             # 真跑过的项数(间隔只加在两项之间)
    for i, item in enumerate(items or []):
        if not item.get("enabled", True):
            continue
        desc = item_label(item)
        if (should_cancel and should_cancel()) or blocked is not None:
            _finish(i, desc, False, skipped=True)   # 未执行(停止/前面已失败), 不是失败
            continue
        # ★ 执行间隔(用户 2026-10-16): 上一项收尾后设备页还要稳定一下再查下一项。
        #   只加在**两项之间** —— 第一项前不加(那时还没有"上一项", 白等 5s 只是拖延)。
        if ran:
            gap = item_interval(item)
            if gap and not _sleep(gap, should_cancel):
                _finish(i, desc, False, skipped=True)      # 间隔中收到停止 → 未执行
                continue
        ran += 1
        # ★ 除「自定义步骤」型之外的每条前置项, **开始时就插一行「执行中」**
        #   (用户 2026-10-16: "充电检测已经通过了, 跑后面的电量检测时, 执行结果中为何
        #    没有显示电量检测的执行中状态")。原来只有自定义步骤型有开始事件(它内部每一步
        #    各发一次), 其余类型(等待充电/电量门槛/地图加载/进入设备页...)从头到尾不吭声,
        #    结果表里那一行要等整项跑完才冒出来 —— 电量门槛这种"等几十分钟"的项, 期间界面
        #    看着就像卡住了。
        #   · 不动「自定义步骤」型: 它内部的步骤行已经是"执行中"指示, 再给父项插一行,
        #     完成时的父行会另起一行 → 变成两行(界面按 _running_row 就地改写, 不留占位);
        #   · 完成时 `_emit_pre_result` 会把这一行**就地**改成 PASS/FAIL(界面不比对 desc),
        #     所以"电量门槛≥50%"→"…(当前电量 45%)"这种描述变化不会造成重复行。
        if on_step_start and item.get("type") != "steps":
            try:
                on_step_start(desc, 1)
            except Exception as e:          # 界面回调异常不能影响前置执行本身
                log.debug(f"[前置] 开始回调异常(忽略): {e}")
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
            blocked = blocked or desc
            continue
        if ok is None:
            continue                    # 该项跳过(如未配置文本)
        _finish(i, desc, ok, time.time() - t0, sub=sub)
        if not ok and not item_is_soft(item):
            blocked = desc
            log.warning(f"[前置] {desc}: 未通过 → 后续前置项不再执行, "
                        f"停下来等你处理(可在该项参数里勾「失败不阻断」改这个行为)")
    return results


def item_label(item):
    """前置项展示名(带关键参数, 便于报告里区分同类型多实例)

    ★ 公开名(结构整理 2026-10-15): 界面侧一直 `from core.session import _item_label`
      直接取下划线私有名 —— 那是跨层依赖私有实现, 改名就会静默炸在别处。
    """
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
    # ★ 「失败不阻断」要在列表里看得出来(用户 2026-10-16): 否则只有点开编辑框才知道
    #   为什么这一项失败后流程还在继续。默认不阻断的类型(见 item_is_soft)只在用户
    #   显式勾/取消时标注 —— 免得默认配置的每一项后面都挂一个尾巴。
    if "soft" in item and item.get("soft"):
        extra = f"{extra}[失败不阻断]"
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


#: 旧名兼容(下划线私有名曾被子模块/界面跨层引用) —— 新代码请用 item_label()
_item_label = item_label
