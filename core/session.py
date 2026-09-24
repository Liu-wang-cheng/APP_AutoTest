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
    warm_webview(d)
    return _any_present(d, texts)


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


def _wait_foreground(d, package, timeout=20):
    """轮询等待 APP 处于前台;超时返回 False(疑似闪退)"""
    end = time.time() + timeout
    while time.time() < end:
        try:
            if d.app_current().get("package") == package:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def _heal_if_dead(d, package):
    """APP 不在前台(疑似闪退)时补拉一次;拉不起来抛 RuntimeError"""
    if _wait_foreground(d, package, timeout=5):
        return
    log.warning("[前置] APP 中途退出(疑似闪退),补一次启动")
    d.app_start(package)
    if not _wait_foreground(d, package, timeout=20):
        raise RuntimeError("APP 补拉后仍未到前台,请检查模拟器环境")


def restart_app(d, cfg, enter_page=True):
    """重启 APP 并进入目标设备页面

    带启动自愈(2026-09-17):模拟器转译层偶发"启动即闪退",检测到 APP 没到
    前台就自动补拉(最多 3 次)。正常启动时前台轮询取代固定 sleep(10),
    几乎零额外开销;只有真闪退才多花一次重启的时间。
    """
    app_cfg = cfg["app"]
    package = app_cfg["package"]
    d.app_stop(package)
    time.sleep(2)

    started = False
    for i in range(3):
        d.app_start(package, app_cfg.get("main_activity"))
        if _wait_foreground(d, package, timeout=20):
            started = True
            break
        log.warning(f"[前置] APP 启动后未到前台(疑似闪退),第 {i + 1}/3 次自愈重启")
        d.app_stop(package)
        time.sleep(3)
    if not started:
        raise RuntimeError(f"APP {package} 反复启动失败(疑似持续闪退),请检查模拟器")

    time.sleep(8)                       # 首页数据加载
    device_name = cfg.get("target_device", "")
    if enter_page and device_name:
        _heal_if_dead(d, package)       # 进设备页前:APP 中途闪退兜底
        if d(text=device_name).exists(timeout=10):
            d(text=device_name).click()
            time.sleep(10)
            _heal_if_dead(d, package)   # 进设备页后:偶发闪退兜底
            log.info(f"[前置] 已进入 {device_name} 设备页面")
        else:
            log.warning(f"[前置] 未找到设备 {device_name}")


# 页面标志文本(2026-09-24 真机 dump 实测):
#   设备页底部是「设备控制 / 服务」; 列表页底部是「主页 / 设备 / 生活 / 日常程序 / 菜单」
DEVICE_PAGE_MARKS = ("设备控制", "服务")
LIST_PAGE_MARKS = ("主页", "日常程序", "收藏")
DEFAULT_DEVICE_READY_TEXT = "设备控制,服务"


def _wait_texts(d, texts, timeout):
    """在 timeout 内轮询: texts 任一出现 → 返回命中的那个; 超时返回 None。

    ★ 直接复用 `_any_present` —— 它已包含 WebView 预热 + **OCR 兜底**, 这里不再
      自己拼一套只读无障碍树的判断(插件页树稀疏时那套会一直判不到)。
    """
    end = time.time() + timeout
    while True:
        for t in texts:
            if _any_present(d, [t]):
                return t
        if time.time() >= end:
            return None
        time.sleep(1)


def _enter_device_page(d, device_name):
    """从**列表页**点设备名进设备页; 不在列表页就先退一层再试(最多 3 层)。

    ⚠ 只在"确认处于列表页"时才点: 设备页上设备名是 WebView 的标题(铺满全屏),
      盲点它会点到页面中央的按钮(如「暂停」)——真机踩过这种坑的风险很高。
    """
    if not device_name:
        return False
    for attempt in range(3):
        if _any_present(d, LIST_PAGE_MARKS) and d(text=device_name).exists(timeout=5):
            d(text=device_name).click()
            time.sleep(8)               # 等插件页加载
            return True
        if _any_present(d, DEVICE_PAGE_MARKS):
            return False                # 已经在设备页(只是没确认到), 别再乱点
        log.info(f"[前置] 当前不在设备列表页, 返回上一层再试(第 {attempt + 1}/3 次)")
        d.press("back")
        time.sleep(3)
    return False


def ensure_device_page(d, device_name="", ready_text=DEFAULT_DEVICE_READY_TEXT,
                       timeout=30, rounds=3):
    """确保已进入设备页: **用特定文本确认**; 确认不了就重新执行进入操作。

    ★ 用户要求(2026-09-24): 进入设备页也要是一个前置条件, 而且必须靠特定文本
      确认真的进去了; 判断失败就再进一次。
      原来的实现是"点了设备名就假定成功", 实际可能还停在列表页或页面没加载完。
    ★ 单轮确认默认 30s: 插件页文本要预热才可读(见 warm_webview)。

    返回 True 已确认进入 / False 多轮后仍确认不到(按前置语义会阻断本轮执行)。
    """
    texts = _split_texts(ready_text) or _split_texts(DEFAULT_DEVICE_READY_TEXT)
    rounds = max(1, int(rounds))
    for r in range(rounds):
        hit = _wait_texts(d, texts, timeout)
        if hit:
            log.info(f"[前置] 已确认进入设备页(命中「{hit}」)")
            return True
        log.warning(f"[前置] 第 {r + 1}/{rounds} 次未确认进入设备页, 重新执行进入操作...")
        _enter_device_page(d, device_name)
    log.warning(f"[前置] {rounds} 轮仍未确认进入设备页({ready_text}), 继续执行——后续步骤会暴露")
    return False


# 插件页所在的 Activity 片段(SmartThings 设备页 = .webplugin.WebPluginActivity)。
# 仅用于"整页文本读不出来时要不要重进"的判断 —— 别的页面不碰。
PLUGIN_ACTIVITY_HINT = "WebPlugin"


def plugin_page_unreadable(d):
    """当前停在插件页、但整页文本都读不出来(只剩标题/时间)。

    ★ 真机实测(2026-09-24): 插件页切视图后无障碍树可能整体消失, 且**不会自己恢复**
      (40s+ dump 无效), 只能重新进入设备页(实测 5s 恢复)。断言/前置遇到这种情况
      要重进一次再继续等, 否则只能干等到超时(用户实测: 断言 30s 超时失败)。
    """
    try:
        act = (d.app_current() or {}).get("activity", "") or ""
    except Exception:
        return False
    return PLUGIN_ACTIVITY_HINT in act and not device_page_readable(d)


def device_page_readable(d, min_texts=5):
    """设备页(插件 WebView)的内容此刻能不能读到。

    ★ 真机实测(2026-09-24): 插件页有时**整块内容都不在无障碍树里** —— dump 出来
      只剩标题(`扫地机器人0087`)和时间 2 条, 此时无论配什么判定文本都读不到,
      充电/地图/断言会全部"判不到"。这种情况重进一次设备页能恢复(实测多次)。
    """
    try:
        xml = d.dump_hierarchy()
    except Exception:
        return False
    texts = [t for t in re.findall(r'text="([^"]*)"', xml) if t]
    return len(texts) >= min_texts


def ensure_charging(d, timeout=1200, on_progress=None, should_cancel=None,
                    ready_text=None, device_name=""):
    """确保设备处于充电状态(未充电则回充),默认超时 20 分钟;should_cancel 可中断等待

    ready_text:  充电判定文本(可配置, 多个用逗号/顿号分隔);留空用默认「充电」
    device_name: 设备名称 —— 用于"页面内容读不出来时重进设备页"(见 device_page_readable)
    """
    charging = is_charging(d, ready_text)
    if charging:
        log.info("[前置] 设备处于充电状态")
        return True
    log.info("[前置] 设备未充电,点击开始回充...")
    click_template(d, "开始回充.png")
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


def _any_present(d, texts):
    """texts 里任一文本出现在屏幕上 → True(空列表 → False)。

    单个文本走 textContains(与旧行为完全一致)。
    多个文本: **在层级 XML 的 text 属性里做子串查找**。

    ⚠ 曾经用 `d(textMatches="a|b")` 实现多值 —— 错的! Android 的
      `UiSelector.textMatches` 是**整串匹配**(Pattern.matches), 不是子串匹配:
      文本是「已充满电 仅真空吸尘器」时, 正则 `充电|满电` 匹配不上(整条不等于
      其中任何一个), 于是"多文本反而比单文本更差"(真机实测: 单值 满电 → True,
      而 充电,满电 → False)。子串语义必须自己来。

    ★ 兜底(用户定的方向: 原生优先, OCR 兜底): 原生查不到**且页面主体没暴露**
      (插件页切换视图后的典型症状)时, 用截图 OCR 再认一次。
    """
    if not texts:
        return False
    xml = warm_webview(d)          # 一次 dump: 预热 + 供多值匹配/稀疏判断
    values = re.findall(r'text="([^"]*)"', xml)
    if len(texts) == 1:
        if d(textContains=texts[0]).exists(timeout=1):
            return True
    elif any(t in v for t in texts for v in values):
        return True
    if ocr.sparse(xml) and ocr.available():
        hit = ocr.find(d, texts)
        if hit:
            log.info(f"[OCR兜底] 无障碍树里没有, 截图识别命中「{hit}」")
            return True
    return False


def ensure_map_loaded(d, timeout=10, device_name="", rounds=6,
                      ready_text="地图编辑", loading_text="地图正在加载"):
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
            warm_webview(d)      # ★ WebView 文本预热(dump 才会让插件页文本进树)
            if _any_present(d, ready) and not _any_present(d, loading):
                log.info("[前置] 地图已加载" + (f"(第{r + 1}轮)" if r else ""))
                return True
            time.sleep(2)
        if r < rounds - 1 and device_name:
            # 退出设备页回到列表再重新进入,触发地图重新加载
            log.warning(f"[前置] 地图 {timeout}s 未就绪,退出重进设备页面(第 {r + 1} 次)")
            d.press("back")
            time.sleep(3)
            if d(text=device_name).exists(timeout=10):
                d(text=device_name).click()
                time.sleep(5)
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

    def _ok():
        if waits and not _any_present(d, waits):
            return False
        if absents and _any_present(d, absents):
            return False
        return True

    end = time.time() + timeout
    while time.time() < end:
        warm_webview(d)          # ★ 同上: WebView 文本预热
        if _ok():
            log.info(f"[前置] {label}: 条件已满足")
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
         "hint": "看到这些文本即算在充电; 多个用逗号/顿号分隔(中英文逗号均可), 任一命中即可"}]},
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


def _run_one(d, cfg, item, on_progress, should_cancel):
    """执行单个前置项(按 type 分发);返回 True/False"""
    t = item.get("type")
    if t == "restart":
        restart_app(d, cfg)
        return True
    if t == "charging":
        return ensure_charging(d, timeout=int(item.get("timeout", 1200)),
                               on_progress=on_progress, should_cancel=should_cancel,
                               ready_text=item.get("ready_text"),
                               device_name=cfg.get("target_device", ""))
    if t == "enter_device":
        return ensure_device_page(
            d,
            device_name=item.get("device_name") or cfg.get("target_device", ""),
            ready_text=item.get("ready_text") or DEFAULT_DEVICE_READY_TEXT,
            timeout=int(item.get("timeout", 30)),
            rounds=int(item.get("rounds", 3)))
    if t == "map_load":
        return ensure_map_loaded(
            d, timeout=int(item.get("timeout", 10)),
            device_name=cfg.get("target_device", ""),
            rounds=int(item.get("rounds", 6)),
            ready_text=item.get("ready_text") or "地图编辑",
            loading_text=item.get("loading_text") or "地图正在加载")
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
                              case_name=item.get("name") or "前置步骤")
        ok = runner.run_steps(steps)
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
                  on_item_done=None):
    """按前置项列表依次执行 → [{"key","desc","ok","elapsed"}]。

    key 供报告/结果去重(同类型多实例带序号), desc 是展示名(含参数摘要),
    elapsed 是该项自己的耗时(秒)。

    ★ on_item_done(result): **每执行完一条立刻回调** —— GUI 用它把结果逐条落表,
      用户要求"一条一条显示结果和耗时, 而不是全部执行完再一次性刷出来"。
    """
    results = []

    def _finish(idx, desc, ok, elapsed=0.0):
        r = {"key": f"pre{idx}", "desc": desc, "ok": bool(ok), "elapsed": elapsed}
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
            _finish(i, desc, False)
            continue
        t0 = time.time()
        ok = _run_one(d, cfg, item, on_progress, should_cancel)
        if ok is None:
            continue                    # 该项跳过(如未配置文本)
        _finish(i, desc, ok, time.time() - t0)
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
    elif t == "charging" and item.get("timeout"):
        extra = f"({int(item['timeout']) // 60}分钟)"
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
