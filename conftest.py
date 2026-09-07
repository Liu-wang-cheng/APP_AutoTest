import os
import re
import subprocess
import sys
import time

import cv2
import pytest

from common import vision
from common.driver import BASE_DIR, load_config
from common.excel_report import ExcelReport
from common.logger import setup_logger


# 初始化循环日志（5MB）
log = setup_logger(os.path.join(BASE_DIR, "reports", "test.log"))


def _clean_debug_dir():
    """每次运行前清空 debug 调试目录（避免历史调试产物越积越多）"""
    debug_dir = os.path.join(BASE_DIR, "reports", "debug")
    if os.path.isdir(debug_dir):
        for f in os.listdir(debug_dir):
            try:
                os.remove(os.path.join(debug_dir, f))
            except OSError:
                pass


# 模块加载时清空 debug 目录
_clean_debug_dir()


def pytest_addoption(parser):
    parser.addoption("--device", default="auto", help="设备ID或 auto")
    parser.addoption("--mode", default="mock", choices=["mock", "real"],
                     help="mock=Mock服务, real=真实设备")
    parser.addoption("--case", default="", help="指定用例名（模糊匹配），空=全部")


def _get_battery_level(d):
    """从页面提取电量百分比，如 100% → 100，找不到返回 -1"""
    xml = d.dump_hierarchy()
    matches = re.findall(r'text="(\d+)%"', xml)
    if matches:
        return max(int(m) for m in matches)
    return -1


def _is_charging(d):
    """检查是否处于充电状态（充电中 或 充电完成）"""
    return d(textContains="充电").exists(timeout=1)


def _click_template(d, img_name, timeout=10):
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


@pytest.fixture(scope="function")
def device(request, report):
    """每个用例前后重启 APP，进入设备页，确保充电且电量 >50%"""
    import uiautomator2 as u2
    cfg = load_config()
    device_cfg = cfg["device"]
    app_cfg = cfg["app"]

    device_id = request.config.getoption("--device")
    if device_id == "auto":
        device_id = device_cfg["default"]
        if device_id == "auto":
            device_id = device_cfg["list"][0]["id"]

    d = u2.connect(device_id)
    d.implicitly_wait(10)

    # 报告填入真实设备信息（SN / App 版本），失败不影响执行
    try:
        report.set_device_info(
            sn=getattr(d, "serial", "") or "",
            app_version=d.app_version(app_cfg["package"]),
        )
    except Exception:
        pass

    # ── 1. 重启 APP ──
    d.app_stop(app_cfg["package"])
    time.sleep(2)
    d.app_start(app_cfg["package"], app_cfg["main_activity"])
    time.sleep(10)

    # ── 2. 进入设备页面 ──
    device_name = cfg.get("target_device", "")
    if device_name and d(text=device_name).exists(timeout=10):
        d(text=device_name).click()
        time.sleep(10)
        print(f"\n[前置] 已进入 {device_name} 设备页面")
    elif device_name:
        print(f"\n[前置] 未找到设备 {device_name}")

    # ── 3. 确保充电状态（超时 20 分钟） ──
    if _is_charging(d):
        print("[前置] 设备处于充电状态")
    else:
        print("[前置] 设备未充电，点击开始回充...")
        _click_template(d, "开始回充.png")
        time.sleep(2)
        # 检测确认弹窗
        if d(textContains="确认").exists(timeout=3):
            d(textContains="确认").click()
            print("[前置] 已点击确认弹窗")
        end = time.time() + 1200  # 20 分钟
        wait_start = time.time()
        while time.time() < end:
            if _is_charging(d):
                print("[前置] 设备已进入充电状态")
                break
            elapsed = int(time.time() - wait_start)
            batt = _get_battery_level(d)
            print(f"[前置] 等待充电中... 已等 {elapsed}s, 电量 {batt}%")
            time.sleep(30)
        else:
            print("[前置] 超时 20 分钟，设备仍未进入充电状态")

    # ── 4. 等待地图加载 ──
    if d(textContains="地图编辑").exists(timeout=5):
        print("[前置] 地图已加载")
    else:
        print("[前置] 等待地图加载...")
        if not d(textContains="地图编辑").wait(timeout=30):
            print("[前置] 警告: 30s 内未检测到地图加载，继续执行")

    # ── 5. 确保电量 > 50%（兜底 30 分钟，防止无限等待） ──
    battery = _get_battery_level(d)
    if battery >= 50:
        print(f"[前置] 电量 {battery}%，达标")
    elif battery > 0:
        print(f"[前置] 电量 {battery}%，不足 50%，等待充电...")
        wait_end = time.time() + 1800
        while time.time() < wait_end:
            time.sleep(30)
            battery = _get_battery_level(d)
            if battery >= 50 or battery <= 0:
                break
        print(f"[前置] 电量等待结束，当前 {battery}%")

    yield d

    d.app_stop(app_cfg["package"])


@pytest.fixture(scope="session")
def report():
    r = ExcelReport()
    yield r
    r.save()
    print(f"\n 报告已生成: {r.path}")


@pytest.fixture(scope="session", autouse=True)
def mock_server(request):
    if request.config.getoption("--mode") == "mock":
        script = os.path.join(BASE_DIR, "mock_server", "server.py")
        proc = subprocess.Popen([sys.executable, script], cwd=BASE_DIR)
        time.sleep(2)
        yield
        if proc.poll() is None:
            proc.terminate()
    else:
        yield
