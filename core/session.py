# -*- coding: utf-8 -*-
"""设备前置准备: 重启APP / 等待充电 / 等待地图加载 / 电量门槛。

GUI 的四个勾选项与 pytest 入口共用这里的 prepare()。
"""
import re
import time

import cv2

from core import vision
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


def ensure_map_loaded(d, timeout=10, device_name="", rounds=6):
    """等待地图加载:正常 10s 内就能加载出来;超时自动退出重进设备页面

    每轮等 timeout 秒,没就绪就 back 退出设备页、重新点进设备页触发地图
    重新加载,最多 rounds 轮。全轮失败才放行告警(后续步骤会给出明确失败)。
    """
    for r in range(rounds):
        end = time.time() + timeout
        while time.time() < end:
            if (d(textContains="地图编辑").exists(timeout=1)
                    and not d(textContains="地图正在加载").exists(timeout=1)):
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
