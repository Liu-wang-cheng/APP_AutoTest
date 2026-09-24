# -*- coding: utf-8 -*-
"""真机渲染探针: 用真实屏幕抓屏, 量化「悬停/重绘是否稳定」。

为什么需要它
    离屏平台(QT_QPA_PLATFORM=offscreen)**不执行真实绘制** —— 悬停态、指示器被
    裁切这类纯渲染问题在那里完全测不出来(paint 计数是 0, 像素对比也全等)。
    2026-09-24 排查「测试步骤里的自动截图勾选框悬停闪动」时:
      · 离屏: 一切正常, 抓不到任何异常
      · 真机抓屏: 悬停时原生指示器只画出左边和上边(右/下边整块缺失), 移开又恢复
        ⇒ 一进一出闪两下
    修法(关 WA_Hover + 给足最小高度)同样是用本脚本做像素级验证的。

用法
    python tools/check_render.py                # 检查表单勾选框的悬停稳定性
    python tools/check_render.py --keep-images  # 保留对比图(reports/render_probe_*.png)
    退出码: 0 = 悬停前后无像素变化(稳定); 1 = 有变化(疑似闪动)。

注意
    会短暂弹出探针窗口并把鼠标移到窗口上, 结束后恢复鼠标原位置。
    隔离: 内存 QSettings + 临时 CASES_DIR/CONFIG_PATH —— 不碰真实用例与配置。
"""
import argparse
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PySide6.QtCore import QPoint, Qt                          # noqa: E402
from PySide6.QtGui import QCursor                              # noqa: E402
from PySide6.QtWidgets import QApplication                     # noqa: E402

app = QApplication.instance() or QApplication([])
import gui.main_window as mw                                   # noqa: E402


class _FakeSettings:
    """内存 QSettings 替身 —— 探针绝不能读写用户真实注册表里的历史记录"""

    store = {}

    def __init__(self, *a, **k):
        pass

    def value(self, k, d=None):
        return _FakeSettings.store.get(k, d)

    def setValue(self, k, v):
        _FakeSettings.store[k] = v

    def sync(self):
        pass


def build_window():
    """一个最小可复现窗口: 单步用例 + 展开卡片与高级参数(让勾选框可见)"""
    mw.QSettings = _FakeSettings
    tmp = tempfile.mkdtemp()
    cases = os.path.join(tmp, "Test_cases")
    os.makedirs(os.path.join(cases, "探针组"), exist_ok=True)
    case_path = os.path.join(cases, "探针组", "probe.yaml")
    with open(case_path, "w", encoding="utf-8") as f:
        f.write("module: 探针组\ncases: []\n")
    mw.CASES_DIR = cases
    mw.CONFIG_PATH = os.path.join(tmp, "config.yaml")
    mw.load_config = lambda: {"app": {}, "device": {}}
    mw.update_config = lambda d: None

    w = mw.MainWindow()
    w.data = {"module": "探针组",
              "cases": [{"name": "probe", "priority": "P1",
                         "steps": [{"desc": "探针步骤", "screenshot": True}]}]}
    w.case_idx = 0
    w.expanded_key = (0,)
    w.case_path = case_path
    w.resize(900, 560)
    w.move(120, 120)
    w.show()
    w.render_cards()
    app.processEvents()
    card = w.cards_lay.itemAt(0).widget()
    card._toggle_advanced()
    for _ in range(20):
        app.processEvents()
        time.sleep(0.02)
    w.raise_()
    w.activateWindow()
    app.processEvents()
    time.sleep(0.4)
    return w, card


def settle(n=15):
    for _ in range(n):
        app.processEvents()
        time.sleep(0.02)


def diff(a, b):
    """两张同尺寸图的不同像素数 + 差异包围盒"""
    n = 0
    x0, y0, x1, y1 = 10 ** 9, 10 ** 9, -1, -1
    for yy in range(min(a.height(), b.height())):
        for xx in range(min(a.width(), b.width())):
            if a.pixel(xx, yy) != b.pixel(xx, yy):
                n += 1
                x0, y0 = min(x0, xx), min(y0, yy)
                x1, y1 = max(x1, xx), max(y1, yy)
    return n, (x0, y0, x1, y1)


def main():
    ap = argparse.ArgumentParser(description="真机渲染探针(悬停稳定性)")
    ap.add_argument("--keep-images", action="store_true", help="保留对比图到 reports/")
    args = ap.parse_args()

    screen = app.primaryScreen()
    w, card = build_window()
    cb = card.widgets["screenshot"]
    cb_tl = cb.mapToGlobal(QPoint(0, 0))
    card_tl = card.mapToGlobal(QPoint(0, 0))
    card_geo = (card_tl.x(), card_tl.y(), card.width(), card.height())
    pad = 6
    region = (cb_tl.x() - pad, cb_tl.y() - pad, cb.width() + 2 * pad, cb.height() + 2 * pad)
    print(f"勾选框 {cb.width()}x{cb.height()} @ ({cb_tl.x()},{cb_tl.y()})  "
          f"卡片 {card_geo[2]}x{card_geo[3]}")

    def grab(tag):
        pm = screen.grabWindow(0, *region)
        if args.keep_images:
            path = os.path.join(ROOT, "reports", f"render_probe_{tag}.png")
            pm.save(path)
        return pm.toImage()

    orig = QCursor.pos()
    failed = 0
    try:
        QCursor.setPos(10, 10)                      # 移出窗口
        settle()
        base = grab("out")

        # ① 悬停卡片本身(避开勾选框) —— 卡片 :hover 改边框会重绘, 勾选框不该受影响
        QCursor.setPos(QPoint(card_geo[0] + card_geo[2] - 40, card_geo[1] + 12))
        settle()
        on_card = grab("card_hover")
        n, bb = diff(base, on_card)
        print(f"[悬停卡片]  勾选框区域不同像素 = {n}  包围盒 = {bb}")
        failed += n > 0

        # ② 悬停勾选框自身 —— 原生指示器的悬停态(曾被画残)
        QCursor.setPos(QPoint(cb_tl.x() + 7, cb_tl.y() + cb.height() // 2))
        settle()
        on_cb = grab("cb_hover")
        n, bb = diff(on_card, on_cb)
        print(f"[悬停勾选框] 不同像素 = {n}  包围盒 = {bb}")
        failed += n > 0

        # ③ 移出后应回到初始态
        QCursor.setPos(10, 10)
        settle()
        back = grab("out_back")
        n, bb = diff(base, back)
        print(f"[移出复原]  与初始态不同像素 = {n}  包围盒 = {bb}")
        failed += n > 0
    finally:
        QCursor.setPos(orig)                        # 恢复用户鼠标位置
        w.close()
        app.processEvents()

    if failed:
        print("结果: 悬停/移出有明显像素变化 → 疑似闪动, 需要检查(可加 --keep-images 看图)")
        return 1
    print("结果: 悬停前后像素完全一致 → 稳定 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
