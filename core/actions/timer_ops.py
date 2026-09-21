# -*- coding: utf-8 -*-
"""定时相关动作: set_time(OCR 滚轮设置时间) / add_timer(添加定时任务) /
latest_record(点最新一条清扫记录)。"""
import os
import re
import subprocess
import time

import cv2
from PIL import Image

from core import registry as reg
from core.logger import get_logger

log = get_logger()


def ensure_ocr(runner):
    """ddddocr 懒加载(加载慢且非所有用例用到)"""
    if getattr(runner, "_ocr", None) is None:
        import ddddocr
        runner._ocr = ddddocr.DdddOcr(show_ad=False)
    return runner._ocr


def find_wheel_desc(runner, name):
    """在层级里找滚轮控件(如 DatePicker_Hour / Timer_TimerPicker_Hour)

    返回该节点的 bounds 文本,找不到返回 ""。
    """
    try:
        xml = runner.d.dump_hierarchy()
    except Exception:
        return ""
    for m in re.finditer(r'content-desc="([^"]+)"[^>]*bounds="(\[[^"]+\])"', xml):
        desc, bounds = m.group(1), m.group(2)
        if name.lower() in desc.lower() and ("picker" in desc.lower() or "date" in desc.lower()):
            return bounds
    return ""


def wheel_bounds(bounds_str):
    """把 bounds 文本 "[l,t][r,b]" 解析为 dict;解析不了返回 None"""
    if not bounds_str:
        return None
    m = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds_str)
    if not m:
        return None
    l, t, r, b = map(int, m.groups())
    return {"left": l, "top": t, "right": r, "bottom": b}


@reg.action("set_time", priority=100)
def do_set_time(runner, step):
    """根据设备当前时间+offset分钟,用 OCR+滑动精确设置时间选择器
    circular: True=循环滚轮(最短路径环绕),False=单向滚轮(纯数值不环绕)
    """
    offset = int(step["set_time"])
    circular = bool(step.get("circular", False))
    # 1. 获取设备当前时间
    try:
        from core.runner import d_serial
        # CREATE_NO_WINDOW:不加的话 GUI 里执行到这一步会闪一个控制台黑框
        _no_window = 0x08000000 if os.name == "nt" else 0
        # pythonw 的 stdin/stderr 句柄都无效,不显式重定向会让 CreateProcess
        # 抛 [WinError 50](GUI 里 adb 全挂的根源)
        cur = subprocess.check_output(
            ["adb", "-s", d_serial(runner.d), "shell", "date", "+%H:%M"],
            timeout=5, stdin=subprocess.DEVNULL, stderr=subprocess.STDOUT,
            creationflags=_no_window).decode().strip()
        ch, cm = map(int, cur.split(":"))
    except Exception:
        raise RuntimeError("无法获取设备当前时间")
    # 2. 计算目标时间
    total = (ch * 60 + cm + offset) % (24 * 60)
    th, tm = total // 60, total % 60
    log.info(f"[set_time] 当前{ch:02d}:{cm:02d} +{offset}min → 目标{th:02d}:{tm:02d}")

    # 3. OCR 识别滚轮当前值(懒加载)
    ensure_ocr(runner)

    # 4. 自动识别滚轮区域(DatePicker_ 或 Timer_TimerPicker_)
    hour_b = wheel_bounds(find_wheel_desc(runner, "Hour"))
    min_b = wheel_bounds(find_wheel_desc(runner, "Minute"))
    if hour_b and min_b:
        hx = (hour_b["left"] + hour_b["right"]) // 2
        mx = (min_b["left"] + min_b["right"]) // 2
        h_w = (hour_b["right"] - hour_b["left"]) // 4
        m_w = (min_b["right"] - min_b["left"]) // 4   # 与小时轮同宽,窄裁剪 OCR 误读
    else:
        # 无滚轮控件时的兜底位置(按 1080x1920 校准的等比坐标)
        w, h = runner.d.window_size()
        hx, mx = int(w * 276 / 1080), int(w * 810 / 1080)
        h_w, m_w = int(w * 110 / 1080), int(w * 65 / 1080)
    adjust_wheel(runner, "hour", hx, th, 24, step=60, crop_w=h_w, circular=circular)
    adjust_wheel(runner, "minute", mx, tm, 60, step=35, crop_w=m_w, circular=circular)
    runner.store["预约时间"] = f"{th:02d}:{tm:02d}"
    runner._last_compare_msg = f"设置{th:02d}:{tm:02d}(当前{ch:02d}:{cm:02d}+{offset}min)"


def _ocr_to_int(raw):
    """ddddocr 数字清洗:0 常被误读成 o/O、1 误读成 l/I(实测 '00'→'0o')

    清洗后解析失败返回 None —— 调用方按 -1/None 走兜底。
    """
    s = (str(raw).strip().replace("o", "0").replace("O", "0")
         .replace("l", "1").replace("I", "1"))
    try:
        return int(s)
    except Exception:
        return None


def adjust_wheel(runner, name, cx, target, mod, step=50, crop_w=60, circular=False):
    """计数法:探测方向+每格步长→计数滑动(不反复OCR)→验证→小步补救

    2026-09-17 修复分钟滚轮划不动:
    - 滑动带 duration(慢速拖动)——无 duration 的快速轻扫 NumberPicker 不响应
    - 探测距离自适应(1×/2×/4× 步长),实测"每格像素"再按比例计数
    - 最终验证不达标直接报错(±1 容差)——静默放行会拿错误时间点确认
    """
    ensure_ocr(runner)
    b = wheel_bounds(find_wheel_desc(runner, name.capitalize()))
    # 兜底 cy 按 1080x1920 校准等比换算
    _, wh = runner.d.window_size()
    cy = (b["top"] + b["bottom"]) // 2 if b else int(wh * 1525 / 1920)
    y1, y2 = cy - 35, cy + 35

    def read_val():
        # 窄裁剪会让 ddddocr 误读(实测 '00'→'0o')—— 先常规宽,失败自动加宽重试
        screen = runner.d.screenshot(format="opencv")
        for w_try in (crop_w, crop_w * 2):
            crop = screen[max(0, y1):y2, max(0, cx - w_try):cx + w_try]
            if crop.size == 0:
                continue
            crop = cv2.resize(crop, (crop.shape[1] * 2, crop.shape[0] * 2),
                              interpolation=cv2.INTER_CUBIC)
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            val = _ocr_to_int(runner._ocr.classification(Image.fromarray(gray)))
            if val is not None:
                return val
        return -1

    def short_diff(a, b, m):
        # 带符号最短差值:边界(|d|=m/2)保持原方向(0→30增大,30→0减小)
        d = b - a
        if d > m // 2:
            d -= m
        elif d < -(m // 2):
            d += m
        return d

    def swipe_up(s):
        runner.d.swipe(cx, cy, cx, cy - s, duration=0.35)

    def swipe_down(s):
        runner.d.swipe(cx, cy - s, cx, cy, duration=0.35)

    # 1. 探测方向 + 每格像素:距离自适应(1×/2×/4×),找到能让滚轮动的最小距离
    v0 = read_val()
    up_increase, px_per_grid, use_s = True, float(step), step
    if v0 >= 0:
        determined = False
        for fn in [swipe_up, swipe_down]:
            for scale in (1, 2, 4):
                s = step * scale
                fn(s)
                time.sleep(0.8)
                v1 = read_val()
                if v1 >= 0 and v1 != v0:
                    sd = short_diff(v0, v1, mod)
                    is_up = (fn == swipe_up)
                    moved_inc = sd > 0
                    up_increase = moved_inc if is_up else (not moved_inc)
                    px_per_grid = s / max(1, abs(sd))   # 实测每格像素
                    use_s = s
                    # 恢复原位(反向滑回去)
                    recover = swipe_down if is_up else swipe_up
                    recover(s)
                    time.sleep(0.6)
                    log.info(f"[set_time] {name} 探测 {v0}→{v1}→恢复 距离{s}px "
                             f"每格{px_per_grid:.0f}px 上滑={'增' if up_increase else '减'}")
                    determined = True
                    break
            if determined:
                break
        if not determined:
            log.info(f"[set_time] {name} 探测无变化(最大 {step * 4}px)")

    # 2. 计数滑动(circular=最短路径环绕,非circular=纯数值不环绕)
    cur = read_val()
    if cur >= 0:
        diff = short_diff(cur, target, mod) if circular else (target - cur)
        need_inc = diff > 0
        grids = abs(diff)
        gps = use_s / px_per_grid if px_per_grid > 0 else grids   # 每次滑动的格数
        n = int(round(grids / gps)) if gps > 0 else grids
        fn = (swipe_up if (need_inc == up_increase) else swipe_down)
        log.info(f"[set_time] {name} 从{cur}→{target} {diff:+d} "
                 f"需滑{n}次×{use_s}px({'上' if fn == swipe_up else '下'})")
        for _ in range(n):
            fn(use_s)
            time.sleep(0.4)

    # 3. 验证 + 小步补救(±1 容差;最终不达标报错——静默放行会拿错误时间点确认)
    fix_s = max(int(px_per_grid), 20)
    for _ in range(10):
        cur = read_val()
        if cur == target:
            log.info(f"[set_time] {name}={target:02d} 达成")
            return
        if cur >= 0:
            diff = short_diff(cur, target, mod) if circular else (target - cur)
            if abs(diff) <= 1:
                log.info(f"[set_time] {name}≈{target:02d} 达成(读{cur})")
                return
            need_inc = diff > 0
            fn = swipe_up if (need_inc == up_increase) else swipe_down
            fn(fix_s)
            time.sleep(0.5)
        else:
            swipe_up(use_s)
            time.sleep(0.5)
    time.sleep(0.8)                          # 滚轮惯性落定后再读一次
    cur = read_val()
    diff = short_diff(cur, target, mod) if (circular and cur >= 0) else (
        (target - cur) if cur >= 0 else None)
    if cur == target or (diff is not None and abs(diff) <= 1):
        log.info(f"[set_time] {name}={target:02d} 达成(惯性落定后读{cur})")
        return
    raise RuntimeError(
        f"set_time {name} 滚轮未能设置到 {target:02d}(停在 {cur}),"
        f"请检查滚轮坐标或滑动参数")


@reg.action("add_timer", priority=60)
def do_add_timer(runner, step):
    """添加定时任务: 统计任务数 → 达上限先删一条 → 点添加按钮。

    step 形态: {add_text: "添加", add_tpl: "添加预约.png", max_tasks: 2}
    三个字段都可省(add_text/add_tpl 有自动回退,max_tasks 省略则不删)。

    任务数靠定时列表控件的 content-desc(`Timer_TimerCell<N>`)统计 ——
    比数界面上的时间文本可靠得多;取不到时退回数 Switch 控件。
    """
    conf = step["add_timer"]
    if isinstance(conf, str):
        conf = {"add_text": conf}
    add_text = conf.get("add_text")
    add_tpl = conf.get("add_tpl")
    max_tasks = int(conf.get("max_tasks", 0))

    if max_tasks:
        xml = runner.d.dump_hierarchy()
        cells = re.findall(r'content-desc="(Timer_TimerCell\d+)"', xml)
        switches = re.findall(r'class="[^"]*Switch[^"]*"', xml)
        cell_count = len(set(cells)) if cells else len(switches)
        log.info(f"[add_timer] 现有任务数: {cell_count}")

        if cell_count >= max_tasks:
            el = runner.d(description=f"Timer_TimerCell{max_tasks - 1}")
            if not el.exists(timeout=2):
                switch_els = [e for e in runner.d(className="android.widget.Switch")]
                if len(switch_els) >= max_tasks:
                    el = switch_els[max_tasks - 1]
            if el.exists(timeout=1):
                b = el.info["bounds"]
                runner.d.long_click((b["left"] + b["right"]) // 2,
                                    (b["top"] + b["bottom"]) // 2, duration=4)
                time.sleep(2)
                _confirm_delete_dialog(runner)
            else:
                log.info("[add_timer] 无法定位任务行,跳过删除")

    # 添加按钮: YAML 指定优先,否则自动回退
    if add_text and runner.d(text=add_text).exists(timeout=3):
        runner.d(text=add_text).click()
        log.info(f"[add_timer] 点击添加按钮(文本:{add_text})")
        return
    if add_tpl and _try_click_template(runner, add_tpl, timeout=5):
        log.info(f"[add_timer] 点击添加按钮(模板:{add_tpl})")
        return
    for t in ("添加", "新增", "添加预约"):
        if runner.d(text=t).exists(timeout=2):
            runner.d(text=t).click()
            log.info(f"[add_timer] 点击添加按钮(自动:{t})")
            return
    if _try_click_template(runner, "添加预约.png", timeout=5):
        log.info("[add_timer] 点击添加按钮(自动:添加预约.png)")
        return
    raise RuntimeError("未找到添加定时任务按钮")


def _try_click_template(runner, name, timeout=5):
    """模板图定位并点击;没匹配到返回 False(不抛异常)"""
    try:
        pos = runner._assert_template(name, timeout=timeout)
    except AssertionError:
        return False
    runner.d.click(pos[0], pos[1])
    return True


def _confirm_delete_dialog(runner):
    """删除定时任务后的确认弹窗: 先按文本按钮找,再退到 content-desc。"""
    xml = runner.d.dump_hierarchy()
    for pat in (r'text="([^"]*确认[^"]*)"', r'text="([^"]*确定[^"]*)"',
                r'text="([^"]*删除[^"]*)"', r'text="([^"]*Delete[^"]*)"',
                r'text="([^"]*OK[^"]*)"', r'text="([^"]*Yes[^"]*)"'):
        m = re.search(pat, xml)
        if m and runner.d(text=m.group(1)).exists(timeout=2):
            runner.d(text=m.group(1)).click()
            time.sleep(2)
            log.info(f"[add_timer] 已删除任务(弹窗:{m.group(1)})")
            return
    for dp in (r'content-desc="(Popup_Confirm)"', r'content-desc="(Button_Confirm)"'):
        m = re.search(dp, xml)
        if m and runner.d(description=m.group(1)).exists(timeout=2):
            runner.d(description=m.group(1)).click()
            time.sleep(2)
            log.info(f"[add_timer] 已删除任务(弹窗:{m.group(1)})")
            return
    log.info("[add_timer] 无确认弹窗,假定已直接删除")


@reg.action("latest_record", priority=20, fixed_bool=True)
def do_latest_record(runner, step):
    """点清扫记录列表里的最新一条。

    记录条目形如「2026-09-15 08:30  25㎡  18min」。分两轮定位:
    先找「面积…」开头的条目(涂鸦记录列表首条的固定文案,最稳),
    再退回按日期/时间格式匹配 —— 单靠时间格式容易被页面上的其它时钟误伤。
    """
    texts = [t for t, _ in runner._get_all_nodes()]
    for t in texts:
        s = t.strip()
        if s.startswith("面积") and re.search(r"\d", s):
            runner.d(text=s).click()
            time.sleep(2)
            log.info(f"[latest_record] 已点最新记录(面积条目): {s}")
            runner._last_compare_msg = f"最新记录: {s}"
            return
    for t in texts:
        s = t.strip()
        if re.match(r"\d{4}-\d{2}-\d{2}", s) or re.match(r"\d{2}:\d{2}", s):
            runner.d(text=s).click()
            time.sleep(2)
            log.info(f"[latest_record] 已点最新记录(日期/时间): {s}")
            runner._last_compare_msg = f"最新记录: {s}"
            return
    raise RuntimeError("未找到清扫记录条目")
