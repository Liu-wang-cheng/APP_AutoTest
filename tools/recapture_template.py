# -*- coding: utf-8 -*-
"""一次性脚本:进设备页 → 展开清扫数据面板 → 截帧 → 裁 ⌃ 收起模板 → 收回。"""
import os
import sys
import time

import cv2
import uiautomator2 as u2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.driver import load_config  # noqa: E402
from core import session  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "reports", "debug")

d = u2.connect("127.0.0.1:7555")
cfg = load_config()
session.prepare(d, cfg)

if not d(textContains="清扫面积").exists(timeout=2):
    pos = session.click_template(d, "展开清扫数据.png")
    print("展开点击:", pos)
    time.sleep(1.5)
else:
    print("面板原本就展开着")
    pos = (986, 287)

f = d.screenshot(format="opencv")
cv2.imencode(".png", f)[1].tofile(os.path.join(OUT, "live_expanded2.png"))

# 裁 ⌃ 页签:展开态下面板把页签推到 (986, 528)
crop = f[528 - 60:528 + 60, 986 - 60:986 + 60]
cv2.imencode(".png", crop)[1].tofile(
    os.path.join(BASE, "Test_img", "templates", "收起清扫数据.png"))
print("收起模板已保存:", crop.shape)

# 收回面板,恢复原状
if pos:
    d.click(*pos)
    time.sleep(1.5)
print("面板已收回")
