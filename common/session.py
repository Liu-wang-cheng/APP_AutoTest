"""设备会话与前置条件: pytest fixture 与 GUI 执行器共用的同一套流程"""
import logging
import os
import re
import time

import cv2

from common import vision
from common.action_runner import d_serial
from common.driver import BASE_DIR, load_config

log = logging.getLogger("vacuum_test")


def get_device_id(cfg, device_option="auto"):
    """根据 --device 参数或 config.yaml 解析设备 ID"""
    if device_option and device_option != "auto":
        return device_option
    device_cfg = cfg["device"]
    if device_cfg["default"] != "auto":
        return device_cfg["default"]
    return device_cfg["list"][0]["id"]


def get_battery_level(d):
    """从页面提取电量百分比，如 100% → 100，找不到返回 -1"""
    xml = d.dump_hierarchy()
    matches = re.findall(r'text="(\d+)%"', xml)
    if matches:
        return max(int(m) for m in matches)
    return -1


def is_charging(d):
    """检查是否处于充电状态（充电中 或 充电完成）"""
    return d(textContains="充电").exists(timeout=1)


def click_template(d, img_name, timeout=10):
    """SIFT 定位并点击模板图片，成功返回坐标，失败返回 None"""
    img_path = os.path.join(vision.TEMPLATE_DIR, img_name)
    end = time.time() + timeout
    while time.time() < end:
        screen = d.screenshot(format="opencv")
        pos = vision.find_in_gray(cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY), img_path)
        if pos:
            d.click(*pos)
            return pos
        time.sleep(2)
    return None


def restart_app(d, cfg, enter_page=True):
    """重启 APP 并进入目标设备页面"""
    app_cfg = cfg["app"]
    d.app_stop(app_cfg["package"])
    time.sleep(2)
    d.app_start(app_cfg["package"], app_cfg["main_activity"])
    time.sleep(10)

    device_name = cfg.get("target_device", "")
    if enter_page and device_name:
        if d(text=device_name).exists(timeout=10):
            d(text=device_name).click()
            time.sleep(10)
            log.info(f"[前置] 已进入 {device_name} 设备页面")
        else:
            log.warning(f"[前置] 未找到设备 {device_name}")


def ensure_charging(d, timeout=1200, on_progress=None, should_cancel=None):
    """确保设备处于充电状态(未充电则回充),默认超时 20 分钟;should_cancel 可中断等待"""
    if is_charging(d):
        log.info("[前置] 设备处于充电状态")
        return True
    log.info("[前置] 设备未充电，点击开始回充...")
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
            log.info("[前置] 收到停止请求，中断充电等待")
            return False
        elapsed = int(time.time() - end) + timeout
        batt = get_battery_level(d)
        msg = f"等待充电中... 已等 {elapsed}s, 电量 {batt}%"
        log.info(f"[前置] {msg}")
        if on_progress:
            on_progress(msg)
        time.sleep(30)
    log.warning(f"[前置] 超时 {timeout // 60} 分钟，设备仍未进入充电状态")
    return False


def ensure_map_loaded(d, timeout=30):
    """等待地图加载完成(以"地图编辑"入口出现为准)"""
    if d(textContains="地图编辑").exists(timeout=5):
        log.info("[前置] 地图已加载")
        return True
    log.info("[前置] 等待地图加载...")
    ok = d(textContains="地图编辑").wait(timeout=timeout)
    if ok:
        log.info("[前置] 地图加载完成")
    else:
        log.warning(f"[前置] {timeout}s 内未检测到地图加载，继续执行")
    return ok


def ensure_battery(d, min_level=50, timeout=1800, on_progress=None, should_cancel=None):
    """确保电量达标,默认 >50%,兜底 30 分钟防止无限等待;should_cancel 可中断等待"""
    battery = get_battery_level(d)
    if battery < 0:
        log.info("[前置] 未读取到电量信息，跳过电量检查")
        return True
    if battery >= min_level:
        log.info(f"[前置] 电量 {battery}%，达标")
        return True
    log.info(f"[前置] 电量 {battery}%，不足 {min_level}%，等待充电...")
    end = time.time() + timeout
    while time.time() < end:
        if should_cancel and should_cancel():
            log.info("[前置] 收到停止请求，中断电量等待")
            return False
        time.sleep(30)
        battery = get_battery_level(d)
        if battery >= min_level or battery <= 0:
            break
    log.info(f"[前置] 电量等待结束，当前 {battery}%")
    return battery >= min_level or battery <= 0


def prepare(d, cfg, restart=True, charging=True, map_load=True, battery=True,
            on_progress=None, should_cancel=None):
    """完整前置流程,按需组合(GUI 勾选项 / pytest 全开);should_cancel 支持中途取消"""
    if restart:
        restart_app(d, cfg)
    if should_cancel and should_cancel():
        return
    if charging:
        ensure_charging(d, on_progress=on_progress, should_cancel=should_cancel)
    if map_load:
        ensure_map_loaded(d)
    if battery:
        ensure_battery(d, on_progress=on_progress, should_cancel=should_cancel)
