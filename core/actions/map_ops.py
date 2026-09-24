# -*- coding: utf-8 -*-
"""地图分区动作: room_zones(识别彩色房间分区) / room_click(点分区) /
merge_zones(合并) / split_zone(分割)。

room_zones 只识别并存储坐标,不点击 —— 点击交给 room_click,两者解耦后
同一份识别结果可以被多次使用(选区清扫逐间点选就是靠这个)。
"""
import math
import os
import time

import cv2
import numpy as np
from PIL import Image

from core import registry as reg
from core.driver import BASE_DIR, split_texts   # 状态文本多个: 半角/全角逗号、顿号等
from core.logger import get_logger

log = get_logger()


def find_hue_peaks(hue_flat, min_count=200):
    """在色调直方图里找峰值,返回峰值所在的 bin 列表。

    两个坑都是实测出来的:
    1. **扫描范围必须含 H=0**。原实现从 bin 2 起扫,而红色系房间的色调就是
       0 附近 —— 整个红/粉色系房间永远识别不到。
    2. **必须容忍平顶**。单一颜色的色块只占一个 bin,经 5 点平滑后会摊成
       几个 bin 宽的平顶;用「严格大于左右邻居」判定时,平台上每个点都有
       相等的邻居,于是一个峰都挑不出来(表现为"彩色像素很多但色调峰值=0")。
       这里把等值区间整体当一个峰,取区间中点。

    对真实地图(颜色有渐变,峰是尖的)行为与原来一致;对色块类纯色区域
    则从"完全识别不到"变成可识别。
    """
    n = len(hue_flat)
    peaks = []
    i = 0
    while i < n:
        if hue_flat[i] <= min_count:
            i += 1
            continue
        j = i
        while j + 1 < n and hue_flat[j + 1] == hue_flat[i]:
            j += 1
        peaks.append((i + j) // 2)
        i = j + 1
    return peaks


def _snap_inside(mask, x, y):
    """点击点若落在色块外(凹形房间质心偏移/名字避让位移),吸附到块内最近像素

    定点清扫点在空白处的根源:彩色区块的质心不保证在房间内部(凹形),点击
    无法到达就整场清扫作废。识别阶段直接把点击点吸附进色块。
    """
    h, w = mask.shape[:2]
    xi, yi = int(x), int(y)
    if 0 <= xi < w and 0 <= yi < h and mask[yi, xi]:
        return xi, yi
    nz = cv2.findNonZero(mask)
    if nz is None:
        return xi, yi
    pts = nz.reshape(-1, 2).astype(np.int64)   # 不同版本返回 (N,1,2) 或 (N,2)
    d2 = (pts[:, 0] - xi) ** 2 + (pts[:, 1] - yi) ** 2
    k = int(d2.argmin())
    return int(pts[k, 0]), int(pts[k, 1])


@reg.action("room_zones", priority=70, fixed_bool=True)
def do_room_zones(runner, step):
    """识别地图上的彩色房间分区并存储坐标
    bounds: [x1, y1, x2, y2] 地图区域,默认屏幕中间区域

    APP 冷启动后地图像素可能比"地图编辑"文本晚几秒才渲染出来 —— 识别不到
    时等 5s 重试,最多 3 次(每轮标注图都存 reports/debug/map_zone.png)。
    """
    bounds = step.get("room_zones")
    if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
        bounds = None
    zones, diag = [], ""
    for attempt in range(3):
        screen = runner.d.screenshot(format="opencv")
        zones, diag = _detect_room_zones(runner, screen, bounds)
        if zones:
            break
        log.warning(f"[room_zones] 第{attempt + 1}/3 次未识别到分区({diag}),"
                    f"等 5s 重试(地图可能尚未渲染)")
        runner._sleep(5)
    if not zones:
        raise RuntimeError(f"未识别到房间分区 ({diag})")
    runner._stored_zones = [(z[0], z[1]) for z in zones[:8]]
    runner._stored_zone_boxes = [z[3] for z in zones[:8]]
    runner._next_room_idx = 0  # 重置点击游标
    msg = f"识别到 {len(runner._stored_zones)} 个房间分区"
    log.info(f"[room_zones] {msg}")
    runner._last_compare_msg = msg


def _detect_room_zones(runner, screen, bounds):
    """单次分区识别:标注图存 reports/debug,返回 (分区列表, 诊断信息)"""
    h, w = screen.shape[:2]
    if bounds is None:
        x1, y1, x2, y2 = int(w * 0.01), int(h * 0.20), int(w * 0.84), int(h * 0.73)
    else:
        x1, y1, x2, y2 = bounds
    roi = screen[y1:y2, x1:x2]
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    hh, s, v = cv2.split(hsv)
    # 只取有颜色的像素(排除白/灰/黑背景)
    color_mask = (s > 10) & (v > 20) & (v < 240)
    # 对有色像素的色调做直方图,找峰值=不同颜色房间
    hue_hist = cv2.calcHist([hh], [0], color_mask.astype(np.uint8), [180], [0, 180])
    hue_flat = hue_hist.flatten().astype(np.float32)
    # 简单均值平滑
    hue_flat = np.convolve(hue_flat, np.ones(5) / 5, mode='same')
    # 找直方图峰值
    peaks = find_hue_peaks(hue_flat)
    # 每个色调峰值找一个连通区域
    zones = []
    rh, rw = roi.shape[:2]
    roi_area = rw * rh
    mask_all = np.zeros_like(hh)
    # 诊断计数:在循环外初始化,保证 zones 为空时错误信息里也有数据可看
    colored_px = int(cv2.countNonZero(color_mask.astype(np.uint8)))
    n_contours = 0
    min_area = roi_area * 0.001
    for pk in peaks:
        hue_mask = (hh > pk - 8) & (hh < pk + 8) & color_mask
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
        hue_mask = cv2.morphologyEx(hue_mask.astype(np.uint8), cv2.MORPH_CLOSE, kernel)
        contours, _ = cv2.findContours(hue_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        n_contours += len(contours)
        mask_all = mask_all | hue_mask
        for c in contours:
            area = cv2.contourArea(c)
            if area > min_area:
                M = cv2.moments(c)
                if M["m00"] > 0:
                    cx = int(M["m10"] / M["m00"])
                    cy = int(M["m01"] / M["m00"])
                    # 向下偏移避开房间名字(名字通常在上方)
                    cy += int((y2 - y1) * 0.03)
                    # ★ 质心可能落在凹形房间之外/空白处 —— 吸附到色块内部
                    cmask = np.zeros_like(hue_mask)
                    cv2.drawContours(cmask, [c], -1, 255, -1)
                    sx, sy = _snap_inside(cmask, cx, cy)
                    bx, by, bw, bh = cv2.boundingRect(c)  # 外框,合并时判相邻用
                    zones.append((sx + x1, sy + y1, area,
                                  (bx + x1, by + y1, bx + bw + x1, by + bh + y1)))
    zones.sort(key=lambda z: -z[2])
    # 去重:合并中心距离过近的分区(同一房间被拆成多块)
    merged = []
    min_dist = min((x2 - x1), (y2 - y1)) * 0.15  # 距离阈值=区域尺寸的15%
    for z in zones:
        zx, zy = z[0], z[1]
        too_close = False
        for m in merged:
            if abs(zx - m[0]) < min_dist and abs(zy - m[1]) < min_dist:
                too_close = True
                break
        if not too_close:
            merged.append(z)
    zones = merged
    debug_dir = os.path.join(BASE_DIR, "reports", "debug")
    os.makedirs(debug_dir, exist_ok=True)
    # 标注检测区域(红框)+ 分区中心(绿点)
    debug = screen.copy()
    cv2.rectangle(debug, (x1, y1), (x2, y2), (0, 0, 255), 3)
    for z in zones[:8]:
        cx, cy = z[0], z[1]
        cv2.circle(debug, (cx, cy), 10, (0, 255, 0), -1)
        bx1, by1, bx2, by2 = z[3]      # 外框:合并排序用的就是它,画出来方便核对
        cv2.rectangle(debug, (bx1, by1), (bx2, by2), (255, 128, 0), 2)
    # 用 PIL 保存,支持中文路径
    Image.fromarray(cv2.cvtColor(screen, cv2.COLOR_BGR2RGB)).save(
        os.path.join(debug_dir, "map_screen.png"))
    Image.fromarray(cv2.cvtColor(roi, cv2.COLOR_BGR2RGB)).save(
        os.path.join(debug_dir, "map_roi.png"))
    Image.fromarray(mask_all).save(os.path.join(debug_dir, "map_mask.png"))
    path = os.path.join(debug_dir, "map_zone.png")
    Image.fromarray(cv2.cvtColor(debug, cv2.COLOR_BGR2RGB)).save(path)
    runner._debug_map_path = path
    if not zones:
        # 分档提示:彩色像素=0 说明地图是灰度/深色主题/根本没加载出来;
        # 有彩色但没峰值 说明房间颜色太接近或峰值阈值过高;
        # 有轮廓但没分区 说明色块面积低于下限。
        return [], (f"彩色像素={colored_px}, 色调峰值={len(peaks)}, "
                    f"轮廓={n_contours}, 有效面积下限={min_area:.0f}px²")
    return zones, ""


@reg.action("room_click", priority=75)
def do_room_click(runner, step):
    """点击已存储的房间分区坐标
    count<=已识别数: 点击第count个分区(1=第1个,2=第2个),不消耗
    count>已识别数: 视为"点击剩余全部",从下一个未点位置开始连点
    """
    count = step.get("room_click")
    if isinstance(count, bool) or count is None:
        count = 1
    count = int(count)
    zones = getattr(runner, "_stored_zones", [])
    if not zones:
        raise RuntimeError("未识别到任何房间分区")
    next_idx = getattr(runner, "_next_room_idx", 0)
    if count <= len(zones):
        # 按序号点单个
        if next_idx >= len(zones):
            raise RuntimeError(
                f"无未点击分区可用(已点到第{next_idx}个,共{len(zones)}个),请重新 room_zones")
        cx, cy = zones[next_idx]
        runner.d.click(cx, cy)
        time.sleep(2)
        runner._next_room_idx = next_idx + 1
    else:
        # count 超出分区数:连点剩余全部(兼容旧语义)
        remaining = len(zones) - next_idx
        for cx, cy in zones[next_idx:]:
            runner.d.click(cx, cy)
            time.sleep(2)
        runner._next_room_idx = len(zones)
        msg = f"连点剩余 {remaining} 个分区(要求 {count}, 可用 {len(zones)})"
        log.info(f"[room_click] {msg}")
        runner._last_compare_msg = msg


def _wait_map_ready(runner, timeout=300):
    """等地图像素就绪:「地图正在加载中」提示消失(加载中点分区/开模式都落空)"""
    end = time.time() + timeout
    while time.time() < end:
        if not runner.d(textContains="地图正在加载").exists(timeout=1):
            return True
        runner._sleep(3)
    log.warning("[map] 120s 内地图仍在加载中")
    return False


def _click_tpl(runner, name, timeout=10):
    """模板点击(复用 session.click_template),成功返回坐标,失败返回 None"""
    from core import session
    return session.click_template(runner.d, name, timeout)


def _collapse_panel(runner, cfg):
    """收回清扫数据面板(若展开):优先收起模板(⌃),没配/匹配不到再原位点一下

    展开后箭头翻转成朝上,展开模板(⌄)匹配不上 —— 收起模板单独一张;
    原位坐标点击作为兜底(位置在展开时已记住,不随箭头方向变化)。
    """
    if not runner.d(textContains=cfg["panel_text"]).exists(timeout=1):
        return
    pos = None
    if cfg.get("collapse_btn"):
        pos = _click_tpl(runner, cfg["collapse_btn"], 5)
    if not pos:
        pos = getattr(runner, "_spot_expand_pos", None)
    if pos:
        runner.d.click(*pos)
        runner._sleep(2)
    runner._spot_expand_pos = None


@reg.action("spot_clean", priority=78, fixed_bool=True)
def do_spot_clean(runner, step):
    """定点清扫:选分区→进指哪扫哪→开始→等 60s→抓数据→判定→收面板→读状态。

    时序(用户指定):点击清扫后过 60s 才读取清扫数据;面积/时长任一 > 0 →
    收回数据面板、读取清扫状态、结束重试循环、继续往下走;全 0(点位不可达/
    未真正清扫)换下一个分区。房子刚扫干净时面积就是 0 —— 所以判据是
    面积**或**时长任一 > 0。
    点位点击前先校验在色块内(防空白);模式确认后核实真的切到了指哪扫哪
    (选单默认勾「全屋清扫」,不核实就会误开全屋清扫)。
    APP 适配三层:内置默认 < locators.yaml 的 spot_clean 段 < 用例步骤参数。
    """
    cfg = {
        "mode_btn": "切换清扫模式.png",
        "mode_text": "指哪扫哪",
        "confirm": "确认",
        "mode_dialog_title": "选择清扫模式",
        "start_btn": "开始清扫.png",
        "state_texts": "指哪扫哪中,局部清扫,清扫中",
        "dock_texts": "充电中,充电完成",
        "expand_btn": "展开清扫数据.png",
        "collapse_btn": "收起清扫数据.png",
        "panel_text": "清扫面积",
        "area_key": "面积",
        "time_key": "时间",
        "max_rooms": 3,
        "verify_timeout": 60,
        "dock_timeout": 1200,
    }
    # APP 适配三层: 内置默认 < locators.yaml 的 spot_clean 段(换 APP 改这里)
    # < 用例步骤参数(单次覆盖)
    if isinstance(getattr(runner, "_locators", None), dict):
        section = runner._locators.get("spot_clean")
        if isinstance(section, dict):
            cfg.update({k: v for k, v in section.items() if v not in (None, "")})
    over = step.get("spot_clean")
    if isinstance(over, dict):
        cfg.update({k: v for k, v in over.items() if v not in (None, "")})
    for k in ("state_texts",):
        if isinstance(cfg[k], str):
            cfg[k] = split_texts(cfg[k])
    zones = getattr(runner, "_stored_zones", [])
    if not zones:
        raise RuntimeError("未识别到任何房间分区,请先执行 room_zones")
    # 地图在加载中时,选模式/点分区全部落空(指哪扫哪还会置灰) —— 先等就绪
    _wait_map_ready(runner)
    from core.actions.data_ops import do_grab

    start_idx = getattr(runner, "_next_room_idx", 0) % len(zones)
    n_try = min(int(cfg["max_rooms"]), len(zones))
    for off in range(n_try):
        idx = (start_idx + off) % len(zones)
        # ★ 换分区重试前:确认还在设备页 —— 上一轮的返回键可能一路退到
        #   APP 首页(实测踩坑),后续选模式/点分区全会在错误页面上操作
        if not runner.d(textContains="地图编辑").exists(timeout=2):
            log.info("[spot] 不在设备页,重启 APP 重新进入")
            from core.driver import load_config
            from core import session as session_mod
            session_mod.restart_app(runner.d, load_config())
            runner._sleep(2)
            _wait_map_ready(runner)
        # ★ 点击前校验点位在色块(地板)内:转场后地图视图可能变化,识别时
        #   存好的点不保证此刻还在地板上;空白则就近吸附
        cx, cy = _ensure_point_on_floor(
            runner, zones[idx][0], zones[idx][1],
            box=(runner._stored_zone_boxes[idx]
                 if idx < len(getattr(runner, "_stored_zone_boxes", [])) else None))
        log.info(f"[spot] 第 {off + 1}/{n_try} 次尝试: 分区{idx + 1} ({cx},{cy})")
        # ★ 进模式:选「指哪扫哪」并确认后必须核实模式真的切过去了 ——
        #   选单里默认勾的是「全屋清扫」,指哪扫哪没选中就点确认,开始的就是
        #   全屋清扫(2026-09-16 实测;选项还可能因地图未就绪而置灰)。核实
        #   不过就重选,最多 3 次,仍失败换下一个分区。
        if not _click_tpl(runner, cfg["mode_btn"], 8):
            runner.d.press("back")
            runner._sleep(2)
            if not _click_tpl(runner, cfg["mode_btn"], 8):
                raise RuntimeError("打不开清扫模式选择")
        mode_ok = False
        for _ in range(3):
            try:
                runner._click_by_locator(cfg["mode_text"], 8)
                runner._sleep(1)
                runner._click_by_locator(cfg["confirm"], 8)
                runner._sleep(2)
            except AssertionError as e:
                log.info(f"[spot] 模式选单操作失败({e}),返回重试")
                runner.d.press("back")
                runner._sleep(2)
                if not _click_tpl(runner, cfg["mode_btn"], 8):
                    break
                continue
            # 确认后回到主页:底部模式卡应显示「指哪扫哪」,模式选单应已关闭
            if (runner.d(textContains=cfg["mode_text"]).exists(timeout=2)
                    and not runner.d(text=cfg["mode_dialog_title"]).exists(timeout=1)):
                mode_ok = True
                break
            log.info("[spot] 模式没有切到指哪扫哪(选项可能置灰),重选")
            runner.d.press("back")
            runner._sleep(2)
            if not _click_tpl(runner, cfg["mode_btn"], 8):
                break
        if not mode_ok:
            log.info("[spot] 模式选择失败,换下一个分区")
            continue
        # 点分区 + 开始
        runner.d.click(cx, cy)
        runner._sleep(2)
        if not _click_tpl(runner, cfg["start_btn"], 10):
            log.info("[spot] 开始按钮没出现,换下一个分区")
            continue
        # ★ 时序对齐原用例:步骤7 确认清扫状态(面板已收,状态可读) →
        #   步骤8 等待清扫完成并返回充电 → 等 60s 数据落盘 → 步骤9 抓取判定
        end = time.time() + cfg["verify_timeout"]
        entered = False
        while time.time() < end:
            if any(runner.d(textContains=s).exists(timeout=1)
                   for s in cfg["state_texts"]):
                entered = True
                break
            runner._sleep(2)
        if not entered:
            log.info("[spot] 未进入清扫状态,换下一个分区")
            continue
        # 原步骤8:双条件等回充(充电文本出现且清扫状态消失,防"未出发"误判)
        if not _wait_clean_cycle(runner, cfg["dock_texts"], cfg["state_texts"],
                                 dock_timeout=cfg["dock_timeout"]):
            log.info("[spot] 等回充超时,换下一个分区")
            continue
        # 回充(双条件成立)= 清扫已结束、数据已是终值 —— 直接展开面板抓取判定
        if not runner.d(textContains=cfg["panel_text"]).exists(timeout=2):
            pos = _click_tpl(runner, cfg["expand_btn"], 10)
            if pos:
                runner._spot_expand_pos = pos
        runner.store.pop(cfg["area_key"], None)   # 清掉上一轮残留,防旧值假阳性
        runner.store.pop(cfg["time_key"], None)
        try:
            do_grab(runner, f"{cfg['area_key']},{cfg['time_key']}")
        except RuntimeError as e:
            log.info(f"[spot] 抓取失败({e}),换下一个分区")
            continue
        area_f = _to_f(runner.store.get(cfg["area_key"]))
        time_f = _to_f(runner.store.get(cfg["time_key"]))
        area = runner.store.get(cfg["area_key"]) or ""
        t = runner.store.get(cfg["time_key"]) or ""
        log.info(f"[spot] 分区{idx + 1} 面积={area} 时长={t}")
        if area_f <= 0 and time_f <= 0:
            log.info("[spot] 面积与时长均为 0(点位未真正清扫),换下一个分区")
            continue
        # 大于 0:收回数据面板,再读取清扫状态
        _collapse_panel(runner, cfg)
        state_now = [s for s in cfg["state_texts"]
                     if runner.d(textContains=s).exists(timeout=1)]
        log.info(f"[spot] 当前清扫状态: {state_now or '无匹配状态文本'}")
        runner._next_room_idx = (idx + 1) % len(zones)
        runner._last_compare_msg = (f"定点清扫 分区{idx + 1}: 面积={area}, "
                                    f"时间={t}, 状态={state_now or '未知'}")
        return
    raise RuntimeError(
        f"尝试 {n_try} 个分区均未产生有效清扫(面积与时长均为 0 或未进入清扫状态)")


def _wait_clean_cycle(runner, dock_texts, state_texts, dock_timeout=1200):
    """等"机器人真回充":充电文本出现 **且** 清扫状态文本消失,连续 2 次满足。

    单看充电文本会把"刚点完开始、机器人还没离坞"误判成回充(2026-09-16
    实测:误判后抓到 0/0 又去重试,干扰了正在进行的清扫)。
    """
    end = time.time() + dock_timeout
    stable = 0
    while time.time() < end:
        docked_now = any(runner.d(textContains=s).exists(timeout=1)
                         for s in dock_texts)
        cleaning = any(runner.d(textContains=s).exists(timeout=1)
                       for s in state_texts)
        if docked_now and not cleaning:
            stable += 1
            if stable >= 2:
                return True
        else:
            stable = 0
        if runner.stopped:
            from core.runner import UserStopped
            raise UserStopped("用户手动停止")
        runner._sleep(5)
    return False


def _ensure_point_on_floor(runner, x, y, box=None, radius=15):
    """点击前校验点位落在色块(地板)内;空白则在外框内就近吸附。

    转场后地图视图可能变化,识别时存好的点不保证此刻还在地板上 —— 每次点击
    前用新鲜截图做色块判定(HSV 阈值与 room_zones 一致)。吸附失败(外框内
    也无色块)返回原坐标,由后续的时长判据兜底换分区。
    """
    try:
        import cv2
        screen = runner.d.screenshot(format="opencv")
        hsv = cv2.cvtColor(screen, cv2.COLOR_BGR2HSV)
        hh, s, v = cv2.split(hsv)
        mask = ((s > 10) & (v > 20) & (v < 240)).astype(np.uint8)
        h, w = mask.shape[:2]
        xi, yi = int(x), int(y)
        x0, y0 = max(0, xi - radius), max(0, yi - radius)
        x1, y1 = min(w, xi + radius + 1), min(h, yi + radius + 1)
        if mask[y0:y1, x0:x1].any():
            return xi, yi                     # 邻域内有色块,可直接点
        # 邻域空白:优先在分区外框内找最近的色块像素
        search, off_x, off_y = mask, 0, 0
        if box:
            bx1, by1, bx2, by2 = (int(v) for v in box)
            search = mask[max(0, by1):by2, max(0, bx1):bx2]
            off_x, off_y = max(0, bx1), max(0, by1)
        nz = cv2.findNonZero(search)
        if nz is None:
            log.info(f"[spot] 点位({xi},{yi})周边与外框内均无色块,按原坐标点击")
            return xi, yi
        pts = nz.reshape(-1, 2)
        pts[:, 0] += off_x
        pts[:, 1] += off_y
        d2 = (pts[:, 0] - xi) ** 2 + (pts[:, 1] - yi) ** 2
        k = int(d2.argmin())
        log.info(f"[spot] 点位({xi},{yi})落在空白,吸附到 ({pts[k, 0]},{pts[k, 1]})")
        return int(pts[k, 0]), int(pts[k, 1])
    except Exception as e:
        log.debug(f"[spot] 点位校验跳过: {e}")
        return int(x), int(y)


def _to_f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return -1.0


def zone_pair_gap(box_a, box_b):
    """两个分区外框的边到边距离;相接或重叠为 0

    先取轴向边距,再算**欧氏距离**(不是取两者的较大值)。斜对角的两个房间
    轴向边距都不为 0,用 max 会低估它们的实际距离,导致排序时被当成"相邻"
    提前尝试 —— 合并只对真正相邻的房间有效,试错一次要白花十几秒。
    """
    ax1, ay1, ax2, ay2 = box_a
    bx1, by1, bx2, by2 = box_b
    dx = max(bx1 - ax2, ax1 - bx2, 0)
    dy = max(by1 - ay2, ay1 - by2, 0)
    return math.hypot(dx, dy)


def merge_pair_order(zones, boxes):
    """合并候选对的尝试顺序:几何相邻的排前面

    只有相邻房间能合并。原来盲枚举 (0,1)(0,2)... 每撞一次不相邻就要
    白花约 10 秒(两次点击 + 全程 sleep + 重进合并模式),而且期间一直在动地图。
    没有外框信息(旧调用方/识别降级)时退回原枚举顺序,行为不变。
    """
    n = len(zones)
    pairs = [(i, j) for i in range(n) for j in range(i + 1, n)]
    if len(boxes) != n:
        return pairs
    pairs.sort(key=lambda p: zone_pair_gap(boxes[p[0]], boxes[p[1]]))
    return pairs


def _wait_loading(runner, timeout=15):
    """等房间操作后的加载提示消失(不长等,失败也不报错)"""
    from core.actions.basic import _LOADING_TEXTS
    end = time.time() + timeout
    while time.time() < end:
        if not any(runner.d(textContains=t).exists(timeout=0.5) for t in _LOADING_TEXTS):
            return
        runner._sleep(1)


@reg.action("merge_zones", priority=80, fixed_bool=True)
def do_merge_zones(runner, step):
    """合并房间: **已在合并模式**下,按几何相邻顺序尝试分区对直到有一对生效。

    只有相邻房间能合并,所以先按外框边距排序 —— 盲枚举每撞一次不相邻就要
    白花十几秒,而且期间一直在动地图。不相邻的表现是弹「提示」弹窗,
    关掉它继续试下一对;每次失败都要重新进入合并模式(点上一步会退出)。
    """
    zones = getattr(runner, "_stored_zones", [])
    boxes = getattr(runner, "_stored_zone_boxes", [])
    if len(zones) < 2:
        raise RuntimeError(f"合并需要至少 2 个分区,当前仅 {len(zones)} 个")
    log.info(f"[merge] {len(zones)} 个分区")
    for i, j in merge_pair_order(zones, boxes):
        log.info(f"[merge] 尝试对 ({i},{j})")
        runner.d.click(*zones[i])
        time.sleep(2)
        runner.d.click(*zones[j])
        time.sleep(2)
        if runner.d(text="合并").exists(timeout=2):
            runner.d(text="合并").click()
            time.sleep(3)
            _wait_loading(runner)
        if runner.d(textContains="提示").exists(timeout=2):
            runner._click_dialog_confirm()
            time.sleep(2)
            log.info(f"[merge] {i},{j} 不相邻")
            continue
        if (runner.d(textContains="请选择").exists(timeout=2)
                or runner.d(textContains="区域分割").exists(timeout=2)):
            log.info(f"[merge] {i},{j} 成功")
            runner._last_compare_msg = f"合并分区 {i + 1}+{j + 1}"
            return
        # 失败后重进合并模式
        runner.d(textContains="区域合并").click()
        time.sleep(3)
        log.info(f"[merge] {i},{j} 未生效")
    raise RuntimeError("所有分区对未成功合并")


@reg.action("split_zone", priority=85, fixed_bool=True)
def do_split_zone(runner, step):
    """分割房间: **已在分割模式**下,依次尝试各分区坐标直到有一个生效。

    不是所有分区都能分割(取决于当前地图划分),点没反应的分区要跳过试下一个;
    每次失败都要重新进入分割模式。成功判据是出现「请选择」或退回到「区域合并」。
    """
    zones = getattr(runner, "_stored_zones", [])
    if not zones:
        raise RuntimeError("当前无可用分区,请先执行 room_zones")
    log.info(f"[split] {len(zones)} 个候选分区")
    for i, (cx, cy) in enumerate(zones):
        log.info(f"[split] 尝试 {i + 1}/{len(zones)} 坐标({cx},{cy})")
        runner.d.click(cx, cy)
        time.sleep(3)
        if runner.d(text="分割").exists(timeout=3):
            runner.d(text="分割").click()
            time.sleep(3)
            _wait_loading(runner)
        if (runner.d(textContains="请选择").exists(timeout=2)
                or runner.d(textContains="区域合并").exists(timeout=2)):
            log.info(f"[split] {i + 1} 成功")
            runner._last_compare_msg = f"已分割分区 {i + 1} ({cx},{cy})"
            return
        # 失败后重进分割模式
        runner.d(textContains="区域分割").click()
        time.sleep(3)
        log.info(f"[split] {i + 1} 未生效,重试下一个")
    raise RuntimeError(f"尝试 {len(zones)} 次未成功")
