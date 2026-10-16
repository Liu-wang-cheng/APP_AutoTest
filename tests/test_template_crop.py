# -*- coding: utf-8 -*-
"""裁剪模板(用户 2026-10-16): 弹窗逻辑 + 当场校验。

用户要求: "在点击步骤界面做一个裁剪模板的按钮, 点击后弹出获取 APP 当前页面的截图,
然后根据点击的位置自动截取模板再让用户手动命名, 并支持手动调整模板截取大小" +
"还要自动校验模板的准确性, 直接给匹配百分比结果"。

为什么最要紧的指标是"屏上还有几处也像它"(而不是"它跟自己有多像" —— 那永远是 100%):
实测三星三个圆按钮的旧模板要放大 1.5~2 倍才命中, 图标细节被插值糊掉后**互相都过阈值**
(暂停模板 0.663 命中了"启动"按钮), 表现就是"点错按钮"。真机复核: 正确的整盘模板
(▶ / ⌂)各 100% 命中自己、次高 57.6%/58.0%(就是另一个按钮), 低于 0.62 阈值 → 合格。
"""
import os
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

cv2 = pytest.importorskip("cv2")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def _frame(dup=False):
    """仿真界面: 青蓝卡片底(带轻微纹理) + 两个同尺寸白盘, 内部图形不同(▶ / ⌂)

    ★ 底必须是**彩色卡片**: 用浅灰/纯色时白盘会"像"背景, NCC 给出假高分(实测踩到)。
    ★ dup=True 时把一个按钮**整块**复制到别处 —— 用来验证"屏上还有几处也像它"。
    """
    rng = np.random.default_rng(7)
    card = np.array([170, 180, 60], dtype="int16")          # BGR 青蓝
    img = np.clip(card + rng.integers(-8, 9, (400, 300, 3)), 0, 255).astype("uint8")
    for cx in (100, 220):
        cv2.circle(img, (cx, 200), 46, (255, 255, 255), -1)
    cv2.fillPoly(img, [np.array([[84, 182], [84, 218], [114, 200]])], (20, 20, 20))   # ▶
    cv2.rectangle(img, (206, 196), (234, 216), (20, 20, 20), -1)                      # ⌂
    cv2.fillPoly(img, [np.array([[202, 198], [238, 198], [220, 178]])], (20, 20, 20))
    if dup:
        img[150:250, 170:270] = img[150:250, 50:150]        # 精确复制(含纹理)
    return img


BUTTON = (54, 154, 92, 92)      # 左按钮整盘 (x, y, w, h)


# ── ① 裁剪框: 点击定位 / 拖动 / 缩放 / 边界夹紧 ──

def test_crop_box_clamps_inside_frame(qapp):
    from gui.dialogs import _CropView
    frame = _frame()
    view = _CropView(frame, [100, 100, 96, 96], 0.5, lambda: None)
    view.center_at(5, 5)                     # 点左上角外 → 贴边, 不能跑出画面
    assert view.box[0] == 0 and view.box[1] == 0, view.box
    view.center_at(9999, 9999)               # 点右下角外
    assert view.box[0] + view.box[2] <= frame.shape[1], view.box
    assert view.box[1] + view.box[3] <= frame.shape[0], view.box
    view.resize_box(500, 500)                # 尺寸超过画面 → 夹到画面大小
    assert view.box[2] <= frame.shape[1] and view.box[3] <= frame.shape[0], view.box


def test_crop_box_resize_keeps_center_and_is_in_place(qapp):
    """★ 必须**原地**改那个 box 列表: 弹窗持有同一个列表当裁剪依据, 重新绑定会让它
    拿到旧框(拖动一次之后裁出来的就不是框里的东西了 —— 这条测试最初就是这么抓到的)"""
    from gui.dialogs import _CropView
    frame = _frame()
    box = [100, 150, 96, 96]
    view = _CropView(frame, box, 0.5, lambda: None)
    assert view.box is box, "视图必须持有调用方那个列表对象"
    cx = box[0] + box[2] // 2
    view.resize_box(60, 40)
    assert (box[2], box[3]) == (60, 40), box           # 调用方看到的是新尺寸
    assert box[0] + box[2] // 2 == cx, box             # 中心不变
    view.center_at(3, 3)
    assert box[0] == 0 and box[1] == 0, box            # 夹紧也写回同一个列表


# ── ② 校验: 直接给匹配百分比, 并揪出"屏上还有别处也像它" ──

def test_validate_passes_for_a_full_button_disc():
    from gui.dialogs import validate_template
    frame = _frame()
    x, y, w, h = BUTTON
    txt, ok, detail = validate_template(frame, frame[y:y + h, x:x + w])
    assert ok, txt
    assert detail["原生匹配"] > 0.99 and detail["最佳尺度"] == 1.0, detail
    # ★ 判据是**区分度**(最佳与次高之差), 不是"次高有没有过阈值":
    #   真机上正确的整盘模板也有 55~67% 的次高(那是另一个按钮), 按绝对阈值会天天误报。
    assert detail["区分度"] >= 0.25, detail
    assert "匹配 100.0%" in txt and "原生尺度" in txt, txt


def test_validate_flags_a_template_that_also_looks_like_another_button():
    """★ 最要紧的一条: 屏上还有别处也像它 → 真机上会点错按钮 → 不合格"""
    from gui.dialogs import validate_template
    frame = _frame(dup=True)
    x, y, w, h = BUTTON
    txt, ok, detail = validate_template(frame, frame[y:y + h, x:x + w])
    assert not ok, txt
    assert detail["其它命中"], detail
    assert "另有" in txt and "点错按钮" in txt, txt


def test_validate_rejects_a_flat_crop():
    """纯色模板: 既没用, 又会让 NCC 在平坦区域给出假高分 → 必须拦"""
    from gui.dialogs import validate_template
    flat = np.full((200, 200, 3), 150, dtype="uint8")
    txt, ok, _d = validate_template(flat, flat[80:120, 80:120])
    assert not ok and "没有纹理" in txt, txt


# ── ③ 保存: 命名清洗 / 补 .png / 重名处理 / 落盘尺寸 ──

def _dialog(qapp, tmp_path, name="新模板"):
    from gui.dialogs import TemplateCropDialog
    dlg = TemplateCropDialog(_frame(), str(tmp_path), default_name=name)
    dlg._box[:] = list(BUTTON)
    return dlg


def test_save_writes_png_with_sanitized_name(qapp, tmp_path):
    dlg = _dialog(qapp, tmp_path, name="开始清扫 新版")
    dlg._save()
    assert dlg.saved_name == "开始清扫_新版.png", dlg.saved_name
    p = tmp_path / dlg.saved_name
    assert p.is_file()
    img = cv2.imdecode(np.fromfile(str(p), dtype=np.uint8), cv2.IMREAD_COLOR)
    assert img.shape[:2] == (92, 92), img.shape       # 存的就是框里的像素


def test_save_asks_before_overwriting(qapp, tmp_path, monkeypatch):
    (tmp_path / "已有.png").write_bytes(b"old")
    dlg = _dialog(qapp, tmp_path, name="已有.png")
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.No))
    dlg._save()
    assert dlg.saved_name == "已有_2.png", dlg.saved_name      # 不覆盖 → 自动加序号
    assert (tmp_path / "已有.png").read_bytes() == b"old"

    dlg2 = _dialog(qapp, tmp_path, name="已有.png")
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.Yes))
    dlg2._save()
    assert dlg2.saved_name == "已有.png"                        # 覆盖 → 名字不变
    assert (tmp_path / "已有.png").read_bytes() != b"old"


# ── ④ 界面接线: 模板字段旁边要有「裁剪模板」按钮 ──

def test_template_field_has_crop_button(qapp):
    """模板字段旁边必须有「裁剪模板」按钮, 且点击时把**当前模板名**传进去(界面接线)

    ★ 用轻量桩而不是真的 MainWindow: 后者会起设备扫描线程, 在测试里会挂住
      (实测: 整条用例跑不完, 得靠超时杀掉)。
    """
    from gui.main_window import MainWindow, StepCard

    class _Main:
        steps = [{"desc": "点开始清扫", "click_template": "开始清扫.png"}]
        preconditions = []
        expanded_key = (0,)          # ★ 卡片只在**展开**时才建字段控件(默认是收起的芯片),
                                     #   让第 0 张卡片处于展开态, 字段才会被创建。
                                     #   值是**元组**(和 MainWindow.expand_card 一致):
                                     #   写 0 不算展开 —— 卡片比的是 `ek == self.key`。
        # 卡片渲染要用它找图标/配色; 方法体只用 schema, 不需要真窗口(test_gui 同款用法)
        _find_action = MainWindow._find_action

        def __init__(self):
            self.called = None

        def crop_template_for_step(self, current_name="", on_saved=None):
            self.called = current_name            # 只记参数, 不真连设备

    main = _Main()
    card = StepCard(main, 0)
    try:
        btns = card.findChildren(QtWidgets.QPushButton)
        crop_btns = [b for b in btns if "裁剪模板" in b.text()]
        assert crop_btns, [b.text() for b in btns]
        crop_btns[0].click()
        assert main.called == "开始清扫.png", main.called   # 把现值当默认名传进去
        assert "click_template" in card.widgets, card.widgets.keys()
    finally:
        card.deleteLater()
