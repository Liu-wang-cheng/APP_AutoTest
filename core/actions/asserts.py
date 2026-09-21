# -*- coding: utf-8 -*-
"""断言类动作: assert / assert_switch / switch_to / compare / diff"""
import os
import time

import cv2
import numpy as np
from PIL import Image

from core import registry as reg
from core.driver import BASE_DIR
from core.logger import get_logger

log = get_logger()

SWITCH_SATURATION = 30       # HSV 均值饱和度超过=有色=打开
SWITCH_BLUE_RATIO = 0.34     # 蓝色通道占比超过=打开,灰色≈0.33
SWITCH_TMPL_THRESHOLD = 0.7  # SIFT 失败时回退 matchTemplate 的相似度门槛


def check_threshold(threshold, inverse):
    """阈值越界直接拒绝: 断言恒真/恒假而报告仍绿是最危险的静默失败。"""
    if inverse and threshold >= 1:
        raise ValueError(
            f"diff 的 threshold={threshold} 非法: 相似度值域为 [0,1], "
            f"similarity > {threshold} 永不成立, 断言会恒为通过。"
            f"请改用小于 1 的值(如 0.99)")
    if not inverse and threshold > 1:
        raise ValueError(
            f"compare 的 threshold={threshold} 非法: 相似度值域为 [0,1], "
            f"similarity < {threshold} 恒成立, 断言会恒为失败。请改用不超过 1 的值")


def compare_screen(runner, baseline, threshold=0.6, inverse=False):
    """对比当前屏幕与基准图,差异>阈值则断言失败。
    只比较中间区域(地图区),忽略状态栏和底部控制区。

    threshold 是相似度门槛,值域为 [0,1]:
      inverse=False (compare): similarity < threshold 判失败
      inverse=True  (diff)   : similarity > threshold 判失败
    """
    check_threshold(threshold, inverse)
    screen = runner.d.screenshot(format="opencv")
    cur = cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY)
    # 基准图与 screenshot 步骤的保存规则一致: screenshots/ 前缀补 Test_img/
    if not os.path.isabs(baseline):
        prefix = "Test_img" if baseline.startswith("screenshots/") else ""
        baseline = os.path.join(BASE_DIR, prefix, baseline)
    try:
        ref = np.array(Image.open(baseline).convert("L"))
    except Exception as e:
        raise FileNotFoundError(f"基准图读取失败: {baseline}, {e}")
    if cur.shape != ref.shape:
        ref = cv2.resize(ref, (cur.shape[1], cur.shape[0]))
    h, w = cur.shape
    # 只比较中间区域
    r1, r2 = int(h * 0.2), int(h * 0.8)
    c1, c2 = int(w * 0.1), int(w * 0.9)
    cur, ref = cur[r1:r2, c1:c2], ref[r1:r2, c1:c2]
    diff = cv2.absdiff(cur, ref)
    similarity = 1 - np.count_nonzero(diff > 25) / diff.size
    msg = f"相似度 {similarity:.1%}"
    if inverse:
        # diff 模式:相似度 < 阈值(即有差异)则通过;默认 0.99 = 变化超 1% 算通过
        if similarity > threshold:
            raise AssertionError(f"{msg},几乎无变化")
    else:
        if similarity < threshold:
            raise AssertionError(f"{msg},低于阈值 {threshold:.0%}")
    runner._last_compare_msg = msg


@reg.action("assert", priority=10)
def do_assert(runner, step):
    val = step["assert"]
    timeout = step.get("timeout", runner.timeout)
    # 兼容旧格式: assert: {value: xxx, timeout: 0}
    if isinstance(val, dict):
        val = val.get("value", str(val))
        timeout = val.get("timeout", timeout) if isinstance(val, dict) else timeout
    if runner._is_image(val):
        runner._assert_template(val, timeout)
    else:
        runner._assert_locator(val, timeout)


@reg.action("compare", priority=95)
def do_compare(runner, step):
    compare_screen(runner, step["compare"], float(step.get("threshold", 0.6)), inverse=False)


@reg.action("diff", priority=115)
def do_diff(runner, step):
    """断言屏幕「有」变化: 默认 0.99 = 只要变化超过 1% 就算通过"""
    compare_screen(runner, step["diff"], float(step.get("threshold", 0.99)), inverse=True)


def get_switch_state(runner, area=None, switch_tpl=None, switch_label=None):
    """获取开关状态及位置,返回 (is_on, pos): 优先颜色检测

    switch_tpl: 用于定位开关的模板图片名(默认 勿扰打开.png)
    area: 可选 [x1,y1,x2,y2] 直接指定开关区域
    switch_label: 可选,通过同行文本标签(如"定制模式")自动定位开关
    """
    screen = runner.d.screenshot(format="opencv")
    if area:
        x1, y1, x2, y2 = area
        pos = ((x1 + x2) // 2, (y1 + y2) // 2)
        crop = screen[y1:y2, x1:x2]
    elif switch_label:
        el = runner.d(textContains=switch_label)
        if not el.exists(timeout=3):
            el = runner.d(description=switch_label)
        if not el.exists(timeout=1):
            raise AssertionError(f"未找到开关标签: {switch_label}")
        b = el.info["bounds"]
        scr_w = screen.shape[1]
        # 开关在标签同行最右侧,只取右端窄区域
        x1 = scr_w - 200
        y1 = max(0, b["top"] - 15)
        x2 = scr_w - 30
        y2 = min(screen.shape[0], b["bottom"] + 15)
        pos = ((x1 + x2) // 2, (y1 + y2) // 2)
        crop = screen[y1:y2, x1:x2]
    else:
        from core import vision
        tpl_name = switch_tpl or "勿扰打开.png"
        tpl_path = vision.resolve_template(tpl_name)
        h, w = np.array(Image.open(tpl_path).convert("L")).shape[:2]
        pos = None
        right_gray = None
        # 只在右半屏搜索(开关始终在右侧,避免左侧UI误匹配)
        half = screen.shape[1] // 2
        for _ in range(2):
            right_gray = cv2.cvtColor(screen[:, half:, :], cv2.COLOR_BGR2GRAY)
            found = vision.find_in_gray(right_gray, tpl_path, min_matches=3)
            if found:
                pos = (found[0] + half, found[1])
                break
            time.sleep(0.3)
            screen = runner.d.screenshot(format="opencv")
        if not pos:
            # SIFT 失败时回退归一化模板匹配
            tpl = np.array(Image.open(tpl_path).convert("L"))
            r = cv2.matchTemplate(right_gray, tpl, cv2.TM_CCORR_NORMED)
            _, v, _, loc = cv2.minMaxLoc(r)
            if v >= SWITCH_TMPL_THRESHOLD:
                pos = (loc[0] + half + w // 2, loc[1] + h // 2)
        if not pos:
            raise AssertionError(f"未定位到开关模板: {tpl_name}")
        # 以匹配位为中心扩散 2x 采样颜色(3x会稀释蓝色信号导致ON误判为OFF)
        x1, y1 = max(0, pos[0] - w), max(0, pos[1] - h)
        x2, y2 = min(screen.shape[1], pos[0] + w), min(screen.shape[0], pos[1] + h)
        crop = screen[y1:y2, x1:x2]
    # 蓝色通道占比:打开=蓝色>阈值,关闭=灰色≈0.33
    bgr_mean = np.mean(crop, axis=(0, 1))
    blue_ratio = bgr_mean[0] / (bgr_mean[0] + bgr_mean[1] + bgr_mean[2] + 1)
    return blue_ratio > SWITCH_BLUE_RATIO, pos


@reg.action("switch_to", priority=50)
def do_switch_to(runner, step):
    """切换开关到指定状态: 颜色判断当前→不对则点击→验证"""
    state = step["switch_to"]
    area = step.get("switch_area")
    switch_tpl = step.get("switch_tpl")
    switch_label = step.get("switch_label")
    expect_on = state in ("on", "打开", "开", True)
    is_on, pos = get_switch_state(runner, area, switch_tpl, switch_label)
    if is_on == expect_on:
        log.info(f"[switch] 已是{'打开' if expect_on else '关闭'}状态,跳过")
        runner._last_compare_msg = f"开关={'打开' if expect_on else '关闭'}(无需切换)"
        return
    if pos is None:
        raise RuntimeError("无法定位开关位置")
    runner.d.click(pos[0], pos[1])
    log.info(f"[switch] 点击开关({pos[0]},{pos[1]})")
    time.sleep(3)
    # 验证
    is_on, _ = get_switch_state(runner, area, switch_tpl, switch_label)
    if is_on == expect_on:
        log.info(f"[switch] 切换成功→{'打开' if expect_on else '关闭'}")
        runner._last_compare_msg = f"开关={'打开' if expect_on else '关闭'}"
    else:
        raise AssertionError(f"开关切换失败,当前{'打开' if is_on else '关闭'}")


@reg.action("assert_switch", priority=55)
def do_assert_switch(runner, step):
    """判断开关状态: 打开=高饱和度颜色(蓝/绿),关闭=灰色(低饱和度)"""
    state = step["assert_switch"]
    area = step.get("switch_area")
    screen = runner.d.screenshot(format="opencv")
    if area:
        x1, y1, x2, y2 = area
        crop = screen[y1:y2, x1:x2]
    else:
        # 自动定位:找开关文字右侧
        el = runner.d(textContains="勿扰开关")
        if not el.exists(timeout=3):
            el = runner.d(textContains="开关")
        if not el.exists(timeout=3):
            raise AssertionError("未找到开关标签")
        b = el.info["bounds"]
        x1 = min(b["right"] + 5, screen.shape[1] - 180)
        y1 = max(0, b["top"] - 20)
        x2 = min(x1 + 180, screen.shape[1])
        y2 = min(b["bottom"] + 70, screen.shape[0])
        crop = screen[y1:y2, x1:x2]
    # HSV 饱和度判断: 开关有色=打开,灰色=关闭
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    mean_sat = hsv[:, :, 1].mean()
    is_on = mean_sat > SWITCH_SATURATION
    expect_on = state in ("on", "打开", "开", True)
    expect_off = state in ("off", "关闭", "关", False)
    if expect_on and is_on:
        log.info(f"[switch] 开关已打开(饱和度{mean_sat:.0f})")
        runner._last_compare_msg = f"开关=打开(饱和度{mean_sat:.0f})"
    elif expect_off and not is_on:
        log.info(f"[switch] 开关已关闭(饱和度{mean_sat:.0f})")
        runner._last_compare_msg = f"开关=关闭(饱和度{mean_sat:.0f})"
    elif expect_on and not is_on:
        raise AssertionError(f"开关未打开(饱和度{mean_sat:.0f})")
    elif expect_off and is_on:
        raise AssertionError(f"开关未关闭(饱和度{mean_sat:.0f})")
    else:
        raise AssertionError(f"无法识别期望状态: {state},用 on/off 或 打开/关闭")
