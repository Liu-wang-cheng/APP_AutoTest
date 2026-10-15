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
