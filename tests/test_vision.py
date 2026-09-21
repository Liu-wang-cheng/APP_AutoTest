# -*- coding: utf-8 -*-
"""find_in_gray 的定位正确性 —— 「点错地方」是 2026-09-16 真机翻车的根源。

用噪声图构造 SIFT 特征最丰富的合成场景:精确复制粘贴、缩小副本干扰、空屏。
旧算法取屏幕特征点质心 + 40px 绝对窗口,会被干扰聚类带偏;新算法用
RANSAC 单应做几何校验并投影模板中心,必须锁定同一个几何一致的簇。
"""
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import vision  # noqa: E402

pytest.importorskip("cv2")


def _noise_patch(w, h, seed):
    rng = np.random.default_rng(seed)
    return rng.integers(0, 255, (h, w), dtype=np.uint8)


def _screen(w, h):
    """平坦背景(没有特征点)的灰度屏"""
    return np.full((h, w), 200, dtype=np.uint8)


@pytest.fixture
def tpl(tmp_path):
    """64x64 噪声模板(内部会放大 3x)"""
    p = tmp_path / "tpl.png"
    cv2.imwrite(str(p), _noise_patch(64, 64, seed=7))
    return str(p)


def test_exact_paste_returns_pasted_center(tpl, tmp_path):
    screen = _screen(480, 360)
    patch = cv2.imread(tpl, cv2.IMREAD_GRAYSCALE)
    x0, y0 = 120, 90
    screen[y0:y0 + 64, x0:x0 + 64] = patch
    pos = vision.find_in_gray(screen, tpl)
    assert pos is not None
    cx, cy = x0 + 32, y0 + 32
    assert abs(pos[0] - cx) <= 3 and abs(pos[1] - cy) <= 3, \
        f"返回 {pos},应为粘贴中心 ({cx}, {cy})"


def test_scaled_decoy_does_not_hijack_result(tpl, tmp_path):
    """缩小副本构成的相似聚类不能把定位带跑 —— 全局清扫「继续清扫」的回归"""
    screen = _screen(600, 400)
    patch = cv2.imread(tpl, cv2.IMREAD_GRAYSCALE)
    x0, y0 = 100, 80
    screen[y0:y0 + 64, x0:x0 + 64] = patch            # 真目标:原尺寸
    small = cv2.resize(patch, (22, 22), interpolation=cv2.INTER_AREA)
    screen[260:282, 380:402] = small                  # 干扰:0.34x 副本
    pos = vision.find_in_gray(screen, tpl)
    assert pos is not None
    cx, cy = x0 + 32, y0 + 32
    assert abs(pos[0] - cx) <= 5 and abs(pos[1] - cy) <= 5, \
        f"定位到 ({pos[0]}, {pos[1]}),应为真目标 ({cx}, {cy})"


def test_no_match_on_blank_screen(tpl):
    assert vision.find_in_gray(_screen(480, 360), tpl) is None


def test_missing_template_file_raises(tmp_path):
    """模板文件不存在 = 配置错误,应当抛异常而不是静默返回 None"""
    with pytest.raises(FileNotFoundError):
        vision.find_in_gray(_screen(200, 200), str(tmp_path / "不存在.png"))


# ── NCC 兜底通道(小图标) ──

def _paste_noise(screen, patch, x0, y0):
    h, w = patch.shape[:2]
    screen[y0:y0 + h, x0:x0 + w] = patch


def test_ncc_fallback_native_scale(tmp_path):
    """原生比例的小图标:NCC 兜底精确命中粘贴中心"""
    patch = _noise_patch(48, 48, seed=3)
    tpl_p = tmp_path / "small.png"
    cv2.imwrite(str(tpl_p), patch)

    screen = _screen(400, 300)
    _paste_noise(screen, patch, 120, 90)
    pos = vision._find_by_ncc(screen, str(tpl_p))
    assert pos is not None
    assert abs(pos[0] - 144) <= 3 and abs(pos[1] - 114) <= 3, \
        f"返回 {pos},应为 (144, 114)"


def _glyph(size):
    """圆润的合成图标(圆+方),模拟真实 UI 小按钮 —— 缩放后结构保留"""
    img = np.full((size, size), 230, dtype=np.uint8)
    cv2.circle(img, (size // 2, size // 2), int(size * 0.32), 60, -1)
    cv2.rectangle(img, (int(size * 0.15), int(size * 0.15)),
                  (int(size * 0.35), int(size * 0.35)), 120, -1)
    return img


def test_ncc_fallback_half_scale_template(tmp_path):
    """模板是半分辨率截图(屏幕上是 2x 大小):scale 2.0 档命中"""
    base = _glyph(48)
    tpl_p = tmp_path / "half.png"
    cv2.imwrite(str(tpl_p), base)

    screen = _screen(500, 400)
    big = cv2.resize(base, (96, 96), interpolation=cv2.INTER_CUBIC)
    _paste_noise(screen, big, 150, 100)
    pos = vision._find_by_ncc(screen, str(tpl_p))
    assert pos is not None
    assert abs(pos[0] - 198) <= 4 and abs(pos[1] - 148) <= 4, \
        f"返回 {pos},应为 (198, 148)"


def test_ncc_fallback_blank_screen_returns_none(tmp_path):
    """平坦背景没有目标:NCC 兜底也必须返回 None,不得瞎报位置"""
    tpl_p = tmp_path / "small.png"
    cv2.imwrite(str(tpl_p), _noise_patch(42, 42, seed=7))
    assert vision._find_by_ncc(_screen(300, 300), str(tpl_p)) is None


def test_ncc_fallback_missing_file_returns_none(tmp_path):
    """兜底通道自身不抛异常(文件缺失在 SIFT 主通道统一报错)"""
    assert vision._find_by_ncc(_screen(200, 200),
                               str(tmp_path / "不存在.png")) is None


# ── 模板按 APP 前缀解析 ──

def test_resolve_template_prefers_prefixed_file(tmp_path, monkeypatch):
    (tmp_path / "涂鸦_开始清扫.png").write_bytes(b"x")
    (tmp_path / "开始清扫.png").write_bytes(b"y")
    monkeypatch.setattr(vision, "TEMPLATE_DIR", str(tmp_path))
    monkeypatch.setattr(vision, "_template_prefix", lambda: "涂鸦")
    assert vision.resolve_template("开始清扫.png").endswith("涂鸦_开始清扫.png")


def test_resolve_template_falls_back_to_plain_name(tmp_path, monkeypatch):
    """带前缀的文件不存在时回退原名(兼容旧命名/部分共用模板)"""
    (tmp_path / "开始清扫.png").write_bytes(b"y")
    monkeypatch.setattr(vision, "TEMPLATE_DIR", str(tmp_path))
    monkeypatch.setattr(vision, "_template_prefix", lambda: "涂鸦")
    assert vision.resolve_template("开始清扫.png").endswith("开始清扫.png")
    assert vision.resolve_template("不存在.png").endswith("不存在.png")


def test_resolve_template_without_prefix_config(tmp_path, monkeypatch):
    (tmp_path / "开始清扫.png").write_bytes(b"y")
    monkeypatch.setattr(vision, "TEMPLATE_DIR", str(tmp_path))
    monkeypatch.setattr(vision, "_template_prefix", lambda: "")
    assert vision.resolve_template("开始清扫.png").endswith("开始清扫.png")


def test_resolve_template_absolute_path_untouched(tmp_path):
    p = str(tmp_path / "x.png")
    assert vision.resolve_template(p) == p
