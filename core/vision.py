# -*- coding: utf-8 -*-
"""SIFT 模板匹配(默认视觉后端)。

只比图标的特征点(角点/边缘/轮廓),不比背景像素 —— 换皮肤/缩放都不怕。
"""
import os
import threading

import cv2
import numpy as np
import yaml
from PIL import Image

from core.driver import BASE_DIR
from core.logger import get_logger

log = get_logger()

TEMPLATE_DIR = os.path.join(BASE_DIR, "Test_img", "templates")

# ── 模板按 APP 前缀解析(适配不同 APP) ──
# config.yaml 的 app.template_prefix(如 涂鸦)→ 模板文件命名 <前缀>_<名称>.png;
# 用例 YAML 里仍写无前缀名称,解析时优先取带前缀文件,回退原名(兼容旧命名)。
_PREFIX_CACHE = {"mtime": None, "value": ""}


def _template_prefix():
    cfg_path = os.path.join(BASE_DIR, "config", "config.yaml")
    try:
        mtime = os.path.getmtime(cfg_path)
        if _PREFIX_CACHE["mtime"] != mtime:
            with open(cfg_path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            _PREFIX_CACHE["value"] = (data.get("app") or {}).get(
                "template_prefix") or ""
            _PREFIX_CACHE["mtime"] = mtime
    except Exception:
        _PREFIX_CACHE["value"] = ""
    return _PREFIX_CACHE["value"]


_template_app_group = ""


def current_app_group():
    """当前 APP 组名(未设置返回空串)"""
    return _template_app_group


def group_case_dir(group):
    """APP 组目录: Test_cases/<组>/(用例/模板/截图都收在这里, 用户要求)"""
    return os.path.join(BASE_DIR, "Test_cases", group or "")


def group_templates_dir(group):
    """组模板目录: Test_cases/<组>/templates/"""
    return os.path.join(group_case_dir(group), "templates")


def group_screenshots_dir(group):
    """组截图目录: Test_cases/<组>/screenshots/"""
    return os.path.join(group_case_dir(group), "screenshots")


def set_template_app_group(group):
    """设置当前 APP 组(执行开始时调用): 模板/截图都取组目录"""
    global _template_app_group
    _template_app_group = group or ""


def list_templates():
    """当前 APP 组可用的模板名列表(去前缀/扩展名, 供编辑器下拉自动获取)"""
    names = []
    search_dirs = []
    if _template_app_group:
        search_dirs.append(group_templates_dir(_template_app_group))
    search_dirs.append(TEMPLATE_DIR)
    for sd in search_dirs:
        if not os.path.isdir(sd):
            continue
        for fn in sorted(os.listdir(sd)):
            if not fn.lower().endswith((".png", ".jpg", ".jpeg", ".bmp")):
                continue
            base = os.path.splitext(fn)[0]
            prefix = _template_prefix()
            if prefix and base.startswith(f"{prefix}_"):
                base = base[len(prefix) + 1:]
            if base not in names:
                names.append(base)
    return names


def resolve_template(name, app_group=None):
    """模板名 → 实际路径:组子目录优先 → 全局 <APP前缀>_<名称> → 全局原名

    - 用例 YAML 永远写无前缀名称(换 APP 不改用例,只换模板文件+前缀配置)
    - ★ 模板按 APP 组子目录存放: templates/<APP组>/<前缀_名>.png(用户要求)
    - app_group 缺省时用模块级 set_template_app_group 设置的当前组
    - 绝对路径原样返回;带目录分隔符的相对路径按 TEMPLATE_DIR 下相对路径处理
    """
    p = str(name)
    if os.path.isabs(p):
        return p
    if os.sep in p or "/" in p:
        return os.path.join(TEMPLATE_DIR, p)
    g = _template_app_group if app_group is None else app_group
    prefix = _template_prefix()
    if g:
        gdir = group_templates_dir(g)
        if prefix:
            cand = os.path.join(gdir, f"{prefix}_{p}")
            if os.path.exists(cand):
                return cand
        cand = os.path.join(gdir, p)
        if os.path.exists(cand):
            return cand
    if prefix:
        cand = os.path.join(TEMPLATE_DIR, f"{prefix}_{p}")
        if os.path.exists(cand):
            return cand
    return os.path.join(TEMPLATE_DIR, p)

_sift = None
_lock = threading.Lock()
_tpl_cache = {}
_tpl_shape_cache = {}


def get_sift():
    """SIFT 检测器单例(创建有开销,全进程共享一个)"""
    global _sift
    if _sift is None:
        with _lock:
            if _sift is None:
                _sift = cv2.SIFT_create(
                    contrastThreshold=0.01,
                    edgeThreshold=5,
                    nOctaveLayers=5,
                )
    return _sift


def _template_image(template_path):
    """读取模板灰度图;小模板(<100px)放大 3x,保证 SIFT 有足够像素"""
    tpl = np.array(Image.open(template_path).convert("L"))
    h, w = tpl.shape
    if w < 100 or h < 100:
        tpl = cv2.resize(tpl, (w * 3, h * 3), interpolation=cv2.INTER_CUBIC)
    return tpl


def template_features(template_path):
    """读取模板并检测特征点,按 (路径, mtime) 缓存,模板更新后自动失效"""
    template_path = str(template_path)
    key = (template_path, os.path.getmtime(template_path))
    if key not in _tpl_cache:
        tpl = _template_image(template_path)
        kp, des = get_sift().detectAndCompute(tpl, None)
        # 同一路径的过期缓存清掉,避免换图后无限增长
        for k in [k for k in _tpl_cache if k[0] == template_path and k != key]:
            del _tpl_cache[k]
        _tpl_cache[key] = (kp, des)
    return _tpl_cache[key]


def template_shape(template_path):
    """模板经小图放大处理后的大小 (w, h)

    单应投影模板中心必须与特征点同一坐标系(小模板放大 3x 后检测的特征,
    坐标就在放大后的图上)。与 template_features 用同一个 mtime 缓存键。
    """
    template_path = str(template_path)
    key = (template_path, os.path.getmtime(template_path))
    if key not in _tpl_shape_cache:
        h, w = _template_image(template_path).shape
        _tpl_shape_cache[key] = (w, h)
    return _tpl_shape_cache[key]


def find_in_gray(screen_gray, template_path, min_matches=4, ratio=0.7):
    """模板定位主入口:SIFT 几何匹配优先,失败自动落全屏 NCC 兜底。

    - SIFT+RANSAC+NCC 二次验证:抗缩放/旋转,大中型模板的主力通道
    - 全屏多尺度 NCC:APP 里不少按钮本身就只有 30~50px(进入设置 34x35、
      展开清扫数据 42x42),特征点屈指可数,几何匹配天然不可靠;但设备
      分辨率固定、UI 渲染确定,原生尺度互相关极稳 —— 实测同链路帧 1.000、
      跨截图链路 1.000、负样本 ≤0.48,判据非常干净
    """
    pos = _find_by_sift(screen_gray, template_path, min_matches, ratio)
    if pos is not None:
        return pos
    return _find_by_ncc(screen_gray, template_path)


def _find_by_sift(screen_gray, template_path, min_matches, ratio):
    """SIFT 特征匹配:RANSAC 单应校验 + NCC 二次验证,返回模板中心或 None"""
    kp1, des1 = template_features(template_path)
    if des1 is None or len(des1) < 2:
        return None
    kp2, des2 = get_sift().detectAndCompute(screen_gray, None)
    if des2 is None or len(des2) < 2:
        return None

    bf = cv2.BFMatcher()
    try:
        raw_matches = bf.knnMatch(des1, des2, k=2)
    except cv2.error:
        return None

    good = [m for m, n in raw_matches if m.distance < ratio * n.distance]
    if len(good) < min_matches:
        return None

    src = np.float32([kp1[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    dst = np.float32([kp2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
    M, mask = cv2.findHomography(src, dst, cv2.RANSAC, 5.0)
    if M is None or mask is None:
        return None
    if int(mask.sum()) < min_matches:
        return None

    w, h = template_shape(template_path)
    corners = np.float32([[0, 0], [w, 0], [w, h], [0, h]]).reshape(-1, 1, 2)
    try:
        proj = cv2.perspectiveTransform(corners, M).reshape(-1, 2)
    except cv2.error:
        return None

    # 尺度合理性:投影后的模板比原模板大/小 6 倍以上,说明匹配错了对象
    pw = float(proj[:, 0].max() - proj[:, 0].min())
    ph = float(proj[:, 1].max() - proj[:, 1].min())
    if pw < 2 or ph < 2:
        return None
    scale = max(pw / w, ph / h)
    if scale > 6.0 or scale < 1.0 / 6.0:
        return None

    # ── NCC 二次验证/修正 ──
    # RANSAC 的 4 点最小集总能"完美"拟合出单应,几何校验拦不住随机共识
    # (2026-09-16 暂停清扫点到空白区即此因)。真目标处归一化互相关接近 1,
    # 空白处接近 0;窗口 ±40px 还能把 ±40px 内的假设偏差修正到按钮中心。
    tpl_img = _template_image(template_path)
    th, tw = tpl_img.shape[:2]
    cx, cy = float(proj[:, 0].mean()), float(proj[:, 1].mean())
    best_score, best_tl, best_size = -1.0, None, None
    for f in (0.8, 0.9, 1.0, 1.1, 1.25):
        rw = max(8, int(round(pw * f)))
        rh = max(8, int(round(rw * th / tw)))
        tl_x, tl_y = cx - rw / 2.0, cy - rh / 2.0
        x0 = max(0, int(tl_x) - 40)
        y0 = max(0, int(tl_y) - 40)
        x1 = min(screen_gray.shape[1], int(tl_x) + 40 + rw)
        y1 = min(screen_gray.shape[0], int(tl_y) + 40 + rh)
        if x1 - x0 < rw or y1 - y0 < rh:
            continue
        win = screen_gray[y0:y1, x0:x1]
        tpl_r = cv2.resize(tpl_img, (rw, rh), interpolation=cv2.INTER_AREA)
        _, score, _, loc = cv2.minMaxLoc(
            cv2.matchTemplate(win, tpl_r, cv2.TM_CCOEFF_NORMED))
        if score > best_score:
            best_score = score
            best_tl = (x0 + loc[0], y0 + loc[1])
            best_size = (rw, rh)
    if best_tl is None or best_score < 0.5:
        return None
    return (best_tl[0] + best_size[0] // 2, best_tl[1] + best_size[1] // 2)


# ── NCC 兜底通道(小图标专用) ──

NCC_SCALES = (0.6, 0.8, 1.0, 1.2, 1.5, 2.0)   # 1.0=原生比例, 2.0=半分辨率截图
NCC_MIN_SCORE = 0.62

_tpl_native_cache = {}


def _template_native(template_path):
    """模板原始灰度图(不经 3x 放大)—— NCC 按原生尺寸做多尺度扫描"""
    template_path = str(template_path)
    key = (template_path, os.path.getmtime(template_path))
    if key not in _tpl_native_cache:
        img = np.array(Image.open(template_path).convert("L"))
        for k in [k for k in _tpl_native_cache
                  if k[0] == template_path and k != key]:
            del _tpl_native_cache[k]
        _tpl_native_cache[key] = img
    return _tpl_native_cache[key]


def _find_by_ncc(screen_gray, template_path):
    """全屏多尺度归一化互相关:小图标兜底,只在 SIFT 失败后触发。

    与 SIFT 通道互补:SIFT 抗缩放/旋转但依赖特征点数量;NCC 不依赖特征点,
    对固定分辨率下的确定渲染最稳。阈值 0.62 之下宁可返回 None(诚实失败),
    也不会像无几何校验的匹配那样点错地方。
    """
    try:
        tpl = _template_native(template_path)
    except Exception:
        return None
    if tpl is None or tpl.size == 0:
        return None
    th, tw = tpl.shape[:2]
    H, W = screen_gray.shape[:2]
    best_score, best_loc, best_size = -1.0, None, None
    for f in NCC_SCALES:
        rw, rh = int(round(tw * f)), int(round(th * f))
        if rw < 8 or rh < 8 or rw >= W or rh >= H:
            continue
        tpl_r = cv2.resize(tpl, (rw, rh))
        _, score, _, loc = cv2.minMaxLoc(
            cv2.matchTemplate(screen_gray, tpl_r, cv2.TM_CCOEFF_NORMED))
        if score > best_score:
            best_score, best_loc, best_size = score, loc, (rw, rh)
    if best_loc is None or best_score < NCC_MIN_SCORE:
        return None
    log.info("[NCC兜底] score=%.2f scale=%.2f", best_score,
             best_size[0] / max(tw, 1))
    return (best_loc[0] + best_size[0] // 2, best_loc[1] + best_size[1] // 2)
