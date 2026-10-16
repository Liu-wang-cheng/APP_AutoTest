# -*- coding: utf-8 -*-
"""图片读写(中文/空格路径安全): 全项目统一入口(结构整理, 用户 2026-10-15)。

★ 为什么必须有这层
  `cv2.imread` / `cv2.imwrite` 在 Windows 上**不支持非 ASCII 路径** —— 本项目的目录与
  文件名大量来自用例描述、设备名、APP 组名(「未分组」「前置」「失败截图.png」…),
  直接调会静默返回 None / 写不出文件。正确做法是 `np.fromfile` + `cv2.imdecode`,
  写出走 `cv2.imencode` + `open(path, "wb")` —— 这段代码原先在 runner 里逐行重复了两份
  (`_annotate_shot` 与 `_mark_last_click_on`), 各处再各写一遍迟早漏一处。

★ 注意
  · 灰度基准图仍走 PIL(见 core/actions/asserts.py、core/vision.py) —— 换实现会改变
    灰度取整方式, 进而让"相似度"数值发生偏移, 不做无谓迁移;
  · 编码失败返回 False/None, 不抛 —— 证据图只是附带产物, 绝不能影响用例执行。
"""
import os

import cv2
import numpy as np


def imread(path, flags=cv2.IMREAD_COLOR):
    """读图(中文路径安全)。读不到返回 None。"""
    try:
        buf = np.fromfile(str(path), dtype=np.uint8)
        if buf.size == 0:
            return None
        return cv2.imdecode(buf, flags)
    except Exception:
        return None


def imwrite(path, img):
    """写图(中文路径安全)。成功返回 True, 失败 False(调用方决定是否告警)。"""
    try:
        d = os.path.dirname(str(path))
        if d:
            os.makedirs(d, exist_ok=True)
        ok, buf = cv2.imencode(".png", img)
        if not ok:
            return False
        with open(str(path), "wb") as f:
            f.write(buf.tobytes())
        return True
    except Exception:
        return False


def annotate(path, draw):
    """把标注画在**已落盘**的图上(读 → draw(img) → 写回同一路径)。

    中文路径安全的读改写一条龙, 供"截图先落盘、再补标注"的流程用(点击/滑动/对比标注)。
    返回是否成功; 失败只影响标注, 证据原图仍在。
    """
    img = imread(path)
    if img is None:
        return False
    try:
        draw(img)
    except Exception:
        return False
    return imwrite(path, img)


#: 中文字体候选(Windows 自带; 打包版也读系统字体目录)
_CJK_FONTS = ("C:/Windows/Fonts/msyh.ttc",        # 微软雅黑
              "C:/Windows/Fonts/msyhbd.ttc",
              "C:/Windows/Fonts/simhei.ttf",      # 黑体
              "C:/Windows/Fonts/simsun.ttc")      # 宋体
_font_cache = {}


def cjk_font(size):
    """中文字体(按字号缓存); 系统里一个都没有 → None(调用方退回 cv2 的 ASCII 画法)"""
    if size in _font_cache:
        return _font_cache[size]
    font = None
    try:
        from PIL import ImageFont
        for path in _CJK_FONTS:
            if os.path.isfile(path):
                font = ImageFont.truetype(path, size)
                break
    except Exception:
        font = None
    _font_cache[size] = font
    return font


def draw_text(img, text, org, color=(255, 255, 255), size=34, outline=(0, 0, 0),
              fallback=None):
    """在 BGR 图上写**中文**(用户 2026-10-16: 证据图上的标注要能读懂)。

    ★ 为什么不能用 cv2.putText: 它的 Hershey 字体只有 ASCII —— 中文会画成一串问号/方块。
      所以走 PIL + 系统字体(msyh/simhei), 带黑描边保证任何底色上都可读。
    ★ 找不到中文字体时退回 cv2 画 `fallback`(纯 ASCII 文案) —— 宁可写成英文, 也不要
      在图上画一堆方块。
    org: 文本**左上角**坐标(与 cv2.putText 的左下角不同, 这里按左上角更好排版)。
    返回实际用掉的宽高 (w, h)。
    """
    if not text:
        return 0, 0
    font = cjk_font(size)
    if font is None:
        cv2.putText(img, str(fallback or text), (int(org[0]), int(org[1] + size)),
                    cv2.FONT_HERSHEY_SIMPLEX, size / 26.0, outline, max(2, size // 8),
                    cv2.LINE_AA)
        cv2.putText(img, str(fallback or text), (int(org[0]), int(org[1] + size)),
                    cv2.FONT_HERSHEY_SIMPLEX, size / 26.0, color, max(1, size // 14),
                    cv2.LINE_AA)
        return len(str(fallback or text)) * size // 2, size
    from PIL import Image, ImageDraw
    pil = Image.fromarray(img[:, :, ::-1])          # BGR → RGB
    d = ImageDraw.Draw(pil)
    x, y = int(org[0]), int(org[1])
    stroke = max(1, size // 14)
    d.text((x, y), str(text), font=font, fill=tuple(int(c) for c in color[::-1]),
           stroke_width=stroke, stroke_fill=tuple(int(c) for c in outline[::-1]))
    img[:, :, :] = np.array(pil)[:, :, ::-1]        # 写回原图(原地)
    try:
        bbox = d.textbbox((x, y), str(text), font=font, stroke_width=stroke)
        return bbox[2] - bbox[0], bbox[3] - bbox[1]
    except Exception:
        return len(str(text)) * size, size


def text_width(text, size):
    """这段中文按字号算多宽(排版用); 没有中文字体时按半角估"""
    font = cjk_font(size)
    if font is None:
        return len(str(text)) * size // 2
    try:
        return int(font.getlength(str(text)))
    except Exception:
        return len(str(text)) * size
