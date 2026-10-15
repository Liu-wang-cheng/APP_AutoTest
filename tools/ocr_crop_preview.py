# -*- coding: utf-8 -*-
"""OCR 顶部裁剪预览: 把"顶部 5% 不识别"的效果画在真实截图上(诊断脚本, 一次性)。

输出到 Test_img/debug/ocr_crop_preview/:
  <名>_原图标注.png  —— 全图 OCR 结果框(绿=保留 / 红=裁掉), 顶部 5% 带加暗+虚线
  <名>_对比.txt      —— 逐行列出: 全图识别到 vs 裁掉后识别到
"""
import os
import sys

import cv2
import numpy as np

# 仓库根目录进 sys.path(与其它 tools 脚本一致)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import ocr as ocr_mod      # 复用项目自己的 OCR 封装(与运行时同一套)

OUT = os.path.join("Test_img", "debug", "ocr_crop_preview")
CROP_RATIO = 0.05


def ocr_lines(img):
    """[(文本, (x1,y1,x2,y2))] —— 直接用项目的 OCR 引擎跑一张 BGR 图"""
    res, _ = ocr_mod._get()(img)
    out = []
    for item in (res or []):
        try:
            box, txt = item[0], item[1]
            xs = [int(p[0]) for p in box]
            ys = [int(p[1]) for p in box]
            out.append((str(txt), (min(xs), min(ys), max(xs), max(ys))))
        except Exception:
            continue
    return out


def annotate(img, lines, cut, path):
    vis = img.copy()
    h = vis.shape[0]
    # 顶部裁剪带: 加暗 + 虚线边界
    overlay = vis.copy()
    cv2.rectangle(overlay, (0, 0), (vis.shape[1], cut), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.55, vis, 0.45, 0, vis)
    for x in range(0, vis.shape[1], 60):
        cv2.line(vis, (x, cut), (min(x + 30, vis.shape[1]), cut), (0, 255, 255), 4)
    cv2.putText(vis, f"TOP {CROP_RATIO:.0%} cropped ({cut}px of {h})", (20, cut + 46),
                cv2.FONT_HERSHEY_SIMPLEX, 1.4, (0, 0, 0), 8, cv2.LINE_AA)
    cv2.putText(vis, f"TOP {CROP_RATIO:.0%} cropped ({cut}px of {h})", (20, cut + 46),
                cv2.FONT_HERSHEY_SIMPLEX, 1.4, (0, 255, 255), 3, cv2.LINE_AA)
    for txt, (x1, y1, x2, y2) in lines:
        dropped = y1 < cut
        color = (80, 80, 255) if dropped else (60, 220, 60)
        cv2.rectangle(vis, (x1, y1), (x2, y2), color, 4)
        label = ("✗ " if dropped else "✓ ") + txt[:18]
        ly = y2 + 34 if y2 + 34 < h - 10 else y1 - 12
        cv2.putText(vis, label, (x1, ly), cv2.FONT_HERSHEY_SIMPLEX, 1.0,
                    (0, 0, 0), 6, cv2.LINE_AA)
        cv2.putText(vis, label, (x1, ly), cv2.FONT_HERSHEY_SIMPLEX, 1.0,
                    color, 2, cv2.LINE_AA)
    cv2.imencode(".png", vis)[1].tofile(path)


def run(name, img):
    h, w = img.shape[:2]
    cut = int(h * CROP_RATIO)
    full = ocr_lines(img)
    cropped = ocr_lines(img[cut:, :])
    kept = {t for t, _b in cropped}
    dropped = [t for t, _b in full if t not in kept]
    annotate(img, full, cut, os.path.join(OUT, f"{name}_原图标注.png"))
    with open(os.path.join(OUT, f"{name}_对比.txt"), "w", encoding="utf-8") as f:
        f.write(f"图: {name}  尺寸 {w}x{h}  裁掉顶部 {cut}px ({CROP_RATIO:.0%})\n")
        f.write(f"全图识别到 {len(full)} 行; 裁掉后 {len(cropped)} 行\n\n")
        f.write("== 被裁掉的文本(这些在裁后**看不到**) ==\n")
        for t, b in full:
            if t not in kept:
                f.write(f"  y={b[1]:>5}  「{t}」\n")
        f.write("\n== 裁剪带之外的行数: %d ==\n" % len(cropped))
        f.write("== 裁剪带内原有的行(y < %d) ==\n" % cut)
        for t, b in full:
            if b[1] < cut:
                f.write(f"  y={b[1]:>5}  「{t}」\n")
    print(f"{name}: 全图 {len(full)} 行 → 裁后 {len(cropped)} 行; 丢掉 {len(dropped)} 行: {dropped}")


def main():
    os.makedirs(OUT, exist_ok=True)
    shots = []

    # ① 模拟器**当前界面**(真实图)
    try:
        import uiautomator2 as u2
        d = u2.connect("127.0.0.1:7555")
        cur = d.screenshot(format="opencv")
        p = os.path.join(OUT, "当前界面.png")
        cv2.imencode(".png", cur)[1].tofile(p)
        shots.append(("当前界面", cur))
        print("已抓模拟器当前界面:", p)
    except Exception as e:
        print("抓当前界面失败:", type(e).__name__, e)

    # ② 仓库里的真实 APP 页面样例(用户之前留下的真机截图)
    sample = "Test_cases/未分组/screenshots/真实界面样例/step01_真实界面对比范围样例.png"
    if os.path.isfile(sample):
        img = cv2.imdecode(np.fromfile(sample, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is not None:
            shots.append(("APP样例", img))
            print("载入真实 APP 页面样例:", sample)

    for name, img in shots:
        run(name, img)
    print("\n输出目录:", os.path.abspath(OUT))


if __name__ == "__main__":
    main()
