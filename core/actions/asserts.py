# -*- coding: utf-8 -*-
"""断言类动作: assert / assert_switch / switch_to / compare / diff"""
import os
import re
import time

import cv2
import numpy as np
from PIL import Image

from core import registry as reg
from core.driver import BASE_DIR, split_texts
from core.actions.basic import parse_xy
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


def _parse_region(text):
    """解析 compare_area 的 4 个数字 → (l, t, r, b, is_percent)。

    ★ 两种单位都支持、自动识别(用户 2026-10-14: "要写明是百分比还是坐标值,
      并且两个都需要兼容"):
      · 数字带 **%** → 百分比(相对画面宽/高): "10%,20% 90%,80%"
      · 数字不带 %   → **像素**: "100,200 900,1500"
      · 可选前缀 pct: / px: 显式声明(增强可读性), 前缀与后缀混用报错
    分隔: 空格/逗号/全角逗号; 返回整数(已剥 % 与前缀)。
    """
    raw = str(text or "").strip()
    prefix = ""
    for p in ("pct:", "px:"):
        if raw.startswith(p):
            prefix, raw = p, raw[len(p):].strip()
            break
    if not raw:
        raise ValueError(f"对比范围(compare_area)为空: {text!r}")
    toks = [t for t in re.split(r"[\s,，]+", raw) if t]
    if len(toks) != 4:
        raise ValueError(
            f"对比范围(compare_area)要 4 个数字(左,上,右,下), 收到: {text!r}")
    is_pct = prefix == "pct:"
    vals = []
    pct_flags = []
    for t in toks:
        pct = t.endswith("%")
        if pct:
            if prefix == "px:":
                raise ValueError(f"对比范围单位混用(px: 前缀不能配 %): {text!r}")
            is_pct = True
            t = t[:-1].strip()
        elif prefix == "pct:":
            raise ValueError(f"对比范围单位混用(pct: 前缀要每个数字都带 %): {text!r}")
        pct_flags.append(pct)
        try:
            vals.append(int(float(t)))
        except ValueError:
            raise ValueError(
                f"对比范围(compare_area)的数字看不懂: {t!r}(整串: {text!r})")
    if any(pct_flags) and not all(pct_flags):
        raise ValueError(f"对比范围单位混用(% 与像素不能混填): {text!r}")
    return vals, is_pct


def _resolve_region(runner, region, shape):
    """对比范围(用户 2026-10-14): 左上角(左,上) — 右下角(右,下)。

    ★ 两种单位都支持、自动识别(用户 2026-10-14):
      · **百分比**: 数字带 %(相对画面宽/高) —— 分辨率无关, 换设备不用改;
      · **像素**: 不带 %(或 px: 前缀) —— 精确落点。
    None/空 → 默认中部(10%,30%-90%,80%, 避开状态栏与底栏);
    越界自动夹到画面内; 零面积/左右颠倒直接报错。
    返回 (x1, y1, x2, y2, 范围描述文本)。
    """
    h, w = shape
    if not region:
        return (int(w * 0.1), int(h * 0.3), int(w * 0.9), int(h * 0.8),
                "10%,30%-90%,80%(默认)")
    vals, is_pct = _parse_region(region)
    if is_pct:
        l, t, r, b = (max(0, min(v, 100)) for v in vals)
        x1, x2 = int(w * l / 100), int(w * r / 100)
        y1, y2 = int(h * t / 100), int(h * b / 100)
        if l >= r or t >= b:
            raise ValueError(
                f"对比范围的左/上要**小于**右/下(左上角→右下角): "
                f"{l}%,{t}% - {r}%,{b}%")
        if x2 - x1 < 2 or y2 - y1 < 2:
            raise ValueError(
                f"对比范围太小或为空: {l}%,{t}% - {r}%,{b}% (画面 {w}x{h})")
        return x1, y1, x2, y2, f"{l}%,{t}%-{r}%,{b}%(百分比)"
    # 像素: 越界夹到画面内
    x1, y1, x2, y2 = (max(0, min(v, m)) for v, m in
                      zip(vals, (w, h, w, h)))
    x1, x2 = sorted((x1, x2))
    y1, y2 = sorted((y1, y2))
    if x2 - x1 < 2 or y2 - y1 < 2:
        raise ValueError(
            f"对比范围太小或为空: {vals} (画面 {w}x{h})")
    return x1, y1, x2, y2, f"{x1},{y1}-{x2},{y2}(像素)"


def _draw_region_annotation(img, x1, y1, x2, y2, pct_txt, sim_line=None):
    """对比范围标注(共用): 范围矩形 + 两行大字(相似度/阈值 + 范围百分比)。

    半透明深色底衬 + 黑描边亮黄字 —— 任何底色(绿色横幅/浅色页面)都可读。
    sim_line: "sim 97.2% / thr 80%" 这类; None = 只画范围(预览模式)。
    """
    cv2.rectangle(img, (x1, y1), (x2 - 1, y2 - 1), (0, 200, 255), 3)
    line1 = sim_line or ""
    line2 = f"area {pct_txt}"
    ty1 = y1 - 60 if y1 >= 110 else y1 + 56
    ty2 = y1 - 20 if y1 >= 110 else y1 + 100
    tx = x1 + 10
    sx1, sy1 = max(0, x1), max(0, ty1 - 46)
    sx2 = min(img.shape[1], x1 + max(len(line1), len(line2)) * 26 + 24)
    sy2 = min(img.shape[0], ty2 + 14)
    if sx2 > sx1 and sy2 > sy1:
        overlay = img.copy()
        cv2.rectangle(overlay, (sx1, sy1), (sx2, sy2), (25, 25, 25), -1)
        cv2.addWeighted(overlay, 0.45, img, 0.55, 0, img)
    if line1:
        cv2.putText(img, line1, (tx, ty1), cv2.FONT_HERSHEY_SIMPLEX, 1.4,
                    (0, 0, 0), 6, cv2.LINE_AA)
        cv2.putText(img, line1, (tx, ty1), cv2.FONT_HERSHEY_SIMPLEX, 1.4,
                    (0, 220, 255), 3, cv2.LINE_AA)
    cv2.putText(img, line2, (tx, ty2), cv2.FONT_HERSHEY_SIMPLEX, 1.4,
                (0, 0, 0), 6, cv2.LINE_AA)
    cv2.putText(img, line2, (tx, ty2), cv2.FONT_HERSHEY_SIMPLEX, 1.4,
                (0, 220, 255), 3, cv2.LINE_AA)


def preview_region(frame, region):
    """预览对比范围(用户 2026-10-14): 在**当前设备画面**上画范围标注 → 存临时文件。

    返回 (标注图路径, 范围描述)。不算相似度(没有基准), 只画范围 —— 用户在编辑
    步骤时点「预览范围」确认矩形盖得对不对。范围非法抛 ValueError(界面转警告框)。
    """
    h, w = frame.shape[:2]
    x1, y1, x2, y2, pct_txt = _resolve_region(None, region, (h, w))
    vis = frame.copy()
    _draw_region_annotation(vis, x1, y1, x2, y2, pct_txt, sim_line=None)
    d = os.path.join(BASE_DIR, "Test_img", "debug", "compare_preview")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, time.strftime("%H%M%S") + "_预览.png")
    ok, buf = cv2.imencode(".png", vis)
    if ok:
        with open(path, "wb") as f:
            f.write(buf.tobytes())
    return path, f"{pct_txt} → 像素 {x1},{y1}-{x2},{y2}"


def compare_screen(runner, baseline, threshold=0.6, inverse=False, region=None):
    """对比当前屏幕与基准图,差异>阈值则断言失败。

    threshold 是相似度门槛,值域为 [0,1]:
      inverse=False (compare): similarity < threshold 判失败
      inverse=True  (diff)   : similarity > threshold 判失败
    region: 对比范围(用户 2026-10-14) —— 左上角(x1,y1)-右下角(x2,y2) 绝对像素;
      缺省 = 画面中部(原行为)。

    ★ 标注(2026-10-14): 比对帧上画出**对比范围矩形** + 相似度/阈值文字, 存为本步
      证据截图(经 runner 的证据槽) —— 报告里直接看出"比的是哪块、阈值多少、结果多少"。
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
    x1, y1, x2, y2, pct_txt = _resolve_region(runner, region, (h, w))
    cur_r, ref_r = cur[y1:y2, x1:x2], ref[y1:y2, x1:x2]
    diff = cv2.absdiff(cur_r, ref_r)
    similarity = 1 - np.count_nonzero(diff > 25) / diff.size
    rng_txt = f"(范围 {pct_txt} → 像素 {x1},{y1}-{x2},{y2})" if region else ""
    msg = f"相似度 {similarity:.1%} {rng_txt}".strip()
    if inverse:
        # diff 模式:相似度 < 阈值(即有差异)则通过;默认 0.99 = 变化超 1% 算通过
        if similarity > threshold:
            raise AssertionError(f"{msg},几乎无变化")
    else:
        if similarity < threshold:
            raise AssertionError(f"{msg},低于阈值 {threshold:.0%}")
    # ★ 通过也要留痕: 只记失败的话, 报告里看不出"比对到底比了多少、阈值多少"
    log.info(f"[截图比对] {msg},阈值 {threshold:.0%} → 通过")
    runner._last_compare_msg = msg
    # ★ 标注 + 证据落盘(2026-10-14): 范围矩形 + 相似度/阈值写在比对帧上,
    #   作为本步的截图证据(经证据槽, _execute 后置块让路)。不在步骤上下文
    #   (单元测试直调/显式 screenshot 键)时跳过落盘, 只算数。
    try:
        if runner._cur_step is not None and "screenshot" not in (runner._cur_step or {}):
            vis = screen.copy()         # 标注底图用**彩色**BGR(灰度图上矩形/文字没有颜色)
            path = runner._step_shot_path(getattr(runner, "_cur_step_desc", "") or "compare")
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            ok, out = cv2.imencode(".png", vis)
            if ok:
                with open(path, "wb") as f:
                    f.write(out.tobytes())

                def _draw(img):
                    # ★ 两行大字(2026-10-14 用户: 字体大一些 + 要有范围百分比),
                    #   半透明深色底衬 + 黑描边 —— 见 _draw_region_annotation
                    _draw_region_annotation(img, x1, y1, x2, y2, pct_txt,
                                            sim_line=f"sim {similarity:.1%} / thr {threshold:.0%}")
                runner._annotate_shot(path, _draw)
                runner._pre_click_shot = path
    except Exception as e:
        log.debug(f"[截图比对] 证据标注失败(不影响比对结果): {e}")


@reg.action("assert", priority=10)
def do_assert(runner, step):
    val = step["assert"]
    timeout = step.get("timeout", runner.timeout)
    # 兼容旧格式: assert: {value: xxx, timeout: 0}
    if isinstance(val, dict):
        val = val.get("value", str(val))
        timeout = val.get("timeout", timeout) if isinstance(val, dict) else timeout
    # ★ 先**拆分**再判断图片(顺序很重要): `断言: 干燥拖布,完成.png` 整串以 .png 结尾,
    #   先判图片的话会被当成**一个**模板文件名 → 找不到文件直接 FileNotFoundError。
    values = split_texts(val) or [str(val)]
    if len(values) == 1:
        # 单个值: 图片走模板断言、其余走文本断言(保持原有语义与报错文案)
        if runner._is_image(values[0]):
            runner._assert_template(values[0], timeout)
        else:
            runner._assert_locator(val, timeout)
        return
    # ★ 多值里**混了模板图**(2026-10-11 实测出来的一类坑): 其中的图片走模板匹配,
    #   其余的走文本, **任一命中即通过**; 以前要么崩(图片在最后)、要么静默忽略(图片在前)。
    imgs = [v for v in values if runner._is_image(v)]
    if imgs:
        return _assert_any(runner, [v for v in values if not runner._is_image(v)],
                           imgs, timeout)
    runner._assert_locator(val, timeout)


def _assert_any(runner, texts, images, timeout):
    """文本候选 与 模板候选 **任一命中即通过**; 都没有则超时报错(报出全部候选)。"""
    import cv2
    from core import vision
    end = time.time() + timeout if timeout > 0 else None
    n = 0
    while True:
        if texts and runner._text_present(*texts, round_no=n + 1):
            log.info(f"[断言] 命中文本(任一): {texts}")
            return
        for name in images:
            screen = runner.d.screenshot(format="opencv")
            if vision.find_in_gray(cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY),
                                   vision.resolve_template(name), min_matches=4):
                log.info(f"[断言] 命中模板: {name}")
                return
        if end and time.time() >= end:
            raise AssertionError(f"超时({timeout}s)未找到(文本或模板任一): {texts + images}")
        n += 1
        runner._poll_sleep(n)


def _resolve_baseline(runner, value):
    """基准图值解析: step:N 引用 → 第 N 步执行结果的截图路径;
    其他值原样返回(旧用例的手动路径)"""
    if isinstance(value, str) and value.startswith("step:"):
        n = int(value[5:])
        res = runner.results[n - 1] if 0 < n <= len(runner.results) else None
        if not res or not res.get("screenshot"):
            raise RuntimeError(
                f"对比基准指向步骤{n}, 但该步没有截图(未开启截图或尚未执行)")
        return res["screenshot"]
    return value


@reg.action("compare", priority=95)
def do_compare(runner, step):
    baseline = _resolve_baseline(runner, step["compare"])
    compare_screen(runner, baseline, float(step.get("threshold", 0.6)),
                   inverse=False, region=step.get("compare_area"))


@reg.action("diff", priority=115)
def do_diff(runner, step):
    """断言屏幕「有」变化: 默认 0.99 = 只要变化超过 1% 就算通过"""
    compare_screen(runner, step["diff"], float(step.get("threshold", 0.99)),
                   inverse=True, region=step.get("compare_area"))


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
