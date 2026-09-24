# -*- coding: utf-8 -*-
"""设备前置准备: 重启APP / 等待充电 / 等待地图加载 / 电量门槛。

GUI 的四个勾选项与 pytest 入口共用这里的 prepare()。
"""
import re
import time

import cv2

from core import vision
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
    """从页面提取电量百分比,如 100% → 100,找不到返回 -1"""
    xml = d.dump_hierarchy()
    matches = re.findall(r'text="(\d+)%"', xml)
    if matches:
        return max(int(m) for m in matches)
    return -1


def is_charging(d):
    """检查是否处于充电状态(充电中 或 充电完成)"""
    return d(textContains="充电").exists(timeout=1)


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


def ensure_charging(d, timeout=1200, on_progress=None, should_cancel=None):
    """确保设备处于充电状态(未充电则回充),默认超时 20 分钟;should_cancel 可中断等待"""
    if is_charging(d):
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
        if is_charging(d):
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


def _any_present(d, texts):
    """texts 里任一文本出现在屏幕上 → True(空列表 → False)。

    单个文本走 textContains(与旧行为完全一致); 多个文本用 textMatches 正则
    **一次查询** —— 候选变多也不会线性增加等待时间。
    """
    if not texts:
        return False
    if len(texts) == 1:
        return bool(d(textContains=texts[0]).exists(timeout=1))
    return bool(d(textMatches="|".join(re.escape(t) for t in texts)).exists(timeout=1))


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
        {"key": "timeout", "label": "超时(秒)", "default": 1200, "type": "int"}]},
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
                               on_progress=on_progress, should_cancel=should_cancel)
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


def prepare_items(d, cfg, items, on_progress=None, should_cancel=None):
    """按前置项列表依次执行 → [{"key","desc","ok"}]。

    key 供报告/结果去重(同类型多实例带序号), desc 是展示名(含参数摘要)。
    """
    results = []
    for i, item in enumerate(items or []):
        if not item.get("enabled", True):
            continue
        if should_cancel and should_cancel():
            results.append({"key": f"pre{i}", "desc": _item_label(item), "ok": False})
            continue
        ok = _run_one(d, cfg, item, on_progress, should_cancel)
        if ok is None:
            continue
        results.append({"key": f"pre{i}", "desc": _item_label(item), "ok": bool(ok)})
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
              "map_load": "地图加载", "battery": "电量门槛"}
    base = labels.get(t, str(t))
    extra = ""
    if t == "battery":
        extra = f"≥{item.get('min_level', 50)}%"
    elif t == "map_load" and item.get("ready_text"):
        extra = f"({item['ready_text']})"
    elif t == "charging" and item.get("timeout"):
        extra = f"({int(item['timeout']) // 60}分钟)"
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
