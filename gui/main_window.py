"""用例编排器主窗口(卡片式简洁版)

布局: 顶部工具栏(文件/设备/前置/运行) + 快捷组件条 + 步骤卡片列表 + 底部结果页签
- 添加步骤: 点快捷组件或「＋添加步骤」分类菜单
- 编辑参数: 点卡片原位展开,高级参数折叠收起
- 数据模型: dict 列表,YAML 文件是唯一持久化格式
"""
import copy
import json
import os

import yaml
from PySide6.QtCore import QPoint, QRect, QSettings, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QDoubleValidator, QIntValidator, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
    QDoubleSpinBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
    QHeaderView, QLabel, QLayout, QLineEdit, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QSplitter, QTabWidget,
    QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
)

from common import app_detect
from common.driver import BASE_DIR, load_config, update_config
from gui import schema
from gui.runner_thread import RunWorker

CASES_DIR = os.path.join(BASE_DIR, "Test_cases")


def _safe_relpath(path):
    """相对路径展示;跨盘符等无法计算时退回原路径"""
    try:
        return os.path.relpath(path, BASE_DIR)
    except ValueError:
        return path

# 分类 → 组件标签颜色
CATEGORY_COLORS = {
    "操作": "#2563eb", "断言": "#ea580c", "数据": "#7c3aed", "开关": "#0d9488",
    "时间": "#0891b2", "地图编辑": "#16a34a", "定时": "#b45309", "流程控制": "#64748b",
}
# 快捷组件条(最常用的排前面)
QUICK_ACTIONS = ["click", "assert", "switch_to", "set_time", "wait_for", "__wait"]

STYLESHEET = """
* { font-family: "Microsoft YaHei UI"; font-size: 12px; }
QMainWindow, QWidget { background: #f4f6f9; }
QLabel { background: transparent; }

QPushButton {
    background: #ffffff; border: 1px solid #d9dee6; border-radius: 6px;
    padding: 4px 12px; color: #333;
}
QPushButton:hover { border-color: #2563eb; color: #2563eb; }
QPushButton:pressed { background: #eef4ff; }

QPushButton#runBtn { background: #2563eb; color: white; border: none; font-weight: bold; padding: 6px 20px; }
QPushButton#runBtn:hover { background: #1d4fd7; }
QPushButton#runBtn:disabled { background: #a9c4f2; }
QPushButton#stopBtn { background: #ffffff; color: #dc2626; border: 1px solid #f0b4b4; font-weight: bold; }
QPushButton#stopBtn:hover { border-color: #dc2626; }
QPushButton#stopBtn:disabled { color: #d8a0a0; border-color: #ecd4d4; }

QToolButton { background: transparent; border: none; color: #98a2b0; padding: 2px 5px; border-radius: 4px; }
QToolButton:hover { background: #eef1f5; color: #475569; }

QLineEdit, QComboBox {
    background: #ffffff; border: 1px solid #d9dee6; border-radius: 6px;
    padding: 4px 8px; color: #333;
}
QLineEdit:focus, QComboBox:focus { border-color: #2563eb; }
QLineEdit#caseName { font-weight: bold; }
QComboBox::drop-down { border: none; width: 18px; }

/* 步骤卡片 */
QFrame#stepCard { background: #ffffff; border: 1px solid #e4e8ee; border-radius: 8px; }
QFrame#stepCard:hover { border-color: #c6d2e6; }
QFrame#stepCardOpen { background: #ffffff; border: 1px solid #2563eb; border-radius: 8px; }
/* else 子步骤卡片(缩进、浅底、左侧描边) */
QFrame#subStepCard { background: #f8fafc; border: 1px solid #e4e8ee; border-left: 3px solid #c7d4ea; border-radius: 6px; }
QFrame#subStepCard:hover { border-color: #c6d2e6; }
QFrame#subStepCardOpen { background: #f8fafc; border: 1px solid #2563eb; border-left: 3px solid #2563eb; border-radius: 6px; }
QLabel#numLabel { color: #b0b9c6; font-weight: bold; font-size: 13px; }
QLabel#chip { color: white; border-radius: 9px; padding: 2px 9px; font-size: 11px; }
QLabel#summary { color: #1e293b; font-weight: bold; }
QLabel#fieldLabel { color: #64748b; }
QLabel#badges { color: #94a3b8; font-size: 11px; }

QFrame#chipStrip { background: #ffffff; border: 1px solid #e4e8ee; border-radius: 8px; }
QPushButton#chipBtn {
    background: #f1f5f9; border: 1px solid transparent; border-radius: 12px;
    padding: 3px 12px; color: #475569;
}
QPushButton#chipBtn:hover { border-color: #2563eb; color: #2563eb; background: #eef4ff; }

QTabWidget::pane { border: 1px solid #e4e8ee; border-radius: 6px; background: #ffffff; top: -1px; }
QTabBar::tab {
    background: transparent; color: #64748b; padding: 5px 14px;
    border: none; border-bottom: 2px solid transparent;
}
QTabBar::tab:selected { color: #2563eb; border-bottom: 2px solid #2563eb; }

QTableWidget { background: #ffffff; border: none; gridline-color: #eef1f5; }
QTableWidget::item { padding: 4px; }
QHeaderView::section {
    background: #fafbfd; border: none; border-bottom: 1px solid #e4e8ee;
    padding: 6px; color: #64748b; font-weight: bold;
}
QPlainTextEdit { background: #ffffff; border: 1px solid #e4e8ee; border-radius: 6px; }
QScrollArea { border: none; background: transparent; }
QMenu { background: #ffffff; border: 1px solid #e4e8ee; border-radius: 8px; padding: 4px; }
QMenu::item { padding: 6px 24px 6px 12px; border-radius: 5px; }
QMenu::item:selected { background: #eef4ff; color: #2563eb; }
QMenu::separator { height: 1px; background: #eef1f5; margin: 4px 8px; }
QMessageBox { background: #ffffff; }
QSplitter::handle { background: transparent; }
"""


class ClickableLabel(QLabel):
    """可点击的图片预览标签"""
    clicked = Signal()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class ClickableFrame(QFrame):
    """可点击的卡片头部行"""
    clicked = Signal()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class ImageViewDialog(QDialog):
    """截图独立查看窗口: 按钮/滚轮缩放,滚动条平移"""

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"截图查看 - {os.path.basename(path)}")
        self.resize(880, 660)
        self._pix = QPixmap(path)
        self._zoom = None  # None = 适应窗口
        self.image_label = QLabel(alignment=Qt.AlignCenter)
        self.image_label.setStyleSheet("background:#222;")
        self._scroll = QScrollArea()
        self._scroll.setWidget(self.image_label)
        self._scroll.setWidgetResizable(False)

        bar = QHBoxLayout()
        for text, fn in [("缩小", self._zoom_out), ("放大", self._zoom_in),
                         ("适应窗口", self._fit), ("1:1", self._orig)]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            bar.addWidget(b)
        tip = QLabel("滚轮缩放 · 拖动滚动条平移")
        tip.setStyleSheet("color:#94a3b8;")
        bar.addWidget(tip)
        bar.addStretch()

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.addLayout(bar)
        root.addWidget(self._scroll)
        self._fit()

    def _apply(self):
        if self._pix.isNull():
            return
        pm = self._pix
        if self._zoom is None:  # 适应窗口
            scaled = pm.scaled(self._scroll.viewport().size(),
                               Qt.KeepAspectRatio, Qt.SmoothTransformation)
        else:
            scaled = pm.scaled(max(1, int(pm.width() * self._zoom)),
                               max(1, int(pm.height() * self._zoom)),
                               Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.image_label.setPixmap(scaled)
        self.image_label.adjustSize()

    def _set_zoom(self, z):
        self._zoom = max(0.1, min(8.0, z))
        self._apply()

    def _zoom_in(self):
        self._set_zoom((self._zoom or 1.0) * 1.25)

    def _zoom_out(self):
        self._set_zoom((self._zoom or 1.0) / 1.25)

    def _fit(self):
        self._zoom = None
        self._apply()

    def _orig(self):
        self._set_zoom(1.0)

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if delta > 0:
            self._zoom_in()
        elif delta < 0:
            self._zoom_out()


class FlowLayout(QLayout):
    """流式布局: 窗口变窄时控件自动换行,顶部横条不再顶死最小宽度"""

    def __init__(self, parent=None, margin=6, spacing=6):
        super().__init__(parent)
        self.setContentsMargins(margin, margin, margin, margin)
        self.setSpacing(spacing)
        self._items = []

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations(Qt.Orientation(0))

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            wid = item.widget()
            if wid is not None and not wid.isVisible():
                continue  # 隐藏控件不占位
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _do_layout(self, rect, test_only):
        """两遍扫描: 先分行并记录行高,再把行内控件按垂直居中放置(跳过隐藏控件)"""
        m = self.contentsMargins()
        left = rect.x() + m.left()
        max_right = rect.right() - m.right()
        spacing = self.spacing()

        # 第一遍: 分行
        lines, cur, cur_h, row_x = [], [], 0, left
        for item in self._items:
            wid = item.widget()
            if wid is not None and not wid.isVisible():
                continue
            w, h = item.sizeHint().width(), item.sizeHint().height()
            if cur and row_x + w > max_right:  # 放不下 → 换行
                lines.append((cur, cur_h))
                cur, cur_h, row_x = [], 0, left
            cur.append((item, w, h))
            cur_h = max(cur_h, h)
            row_x += w + spacing
        if cur:
            lines.append((cur, cur_h))

        # 第二遍: 行内垂直居中
        y = rect.y() + m.top()
        for row, row_h in lines:
            x = left
            for item, w, h in row:
                if not test_only:
                    item.setGeometry(QRect(QPoint(x, y + (row_h - h) // 2), QSize(w, h)))
                x += w + spacing
            y += row_h + spacing
        return y - spacing + m.bottom() - rect.y()


def make_action_menu(parent, on_pick):
    """按分类构造动作选择菜单;on_pick(action_key) 在选中时回调"""
    menu = QMenu(parent)
    for cat in schema.CATEGORY_ORDER:
        menu.addSection(cat)
        for a in schema.ACTIONS:
            if a["category"] == cat:
                act = QAction(a["label"], menu)
                act.triggered.connect(lambda _, k=a["key"]: on_pick(k))
                menu.addAction(act)
    menu.addSection("其他")
    wait_act = QAction("延时等待", menu)
    wait_act.triggered.connect(lambda: on_pick("__wait"))
    menu.addAction(wait_act)
    return menu


def _make_field_widget(field, value):
    """按 schema 字段类型建控件,返回 (widget, 取值getter)"""
    t = field["type"]
    hint = field.get("hint", "")
    if t == "bool":
        w = QCheckBox()
        w.setChecked(bool(value))
        return w, w.isChecked
    if t == "int":
        w = QLineEdit("" if value is None else str(value))
        w.setValidator(QIntValidator(-100000, 100000))
        w.setPlaceholderText(hint)
        return w, _int_of(w)
    if t == "float":
        w = QLineEdit("" if value is None else str(value))
        w.setValidator(QDoubleValidator(0, 100000, 4))
        w.setPlaceholderText(hint)
        return w, _float_of(w)
    if t == "int4":
        w = QLineEdit(",".join(map(str, value)) if value else "")
        w.setPlaceholderText(hint or "x1,y1,x2,y2")
        return w, _int4_of(w)
    # text: 未编辑时保留原值(列表/数字等非字符串类型不被表单转成字符串)
    w = QLineEdit("" if value is None else str(value))
    w.setPlaceholderText(hint)
    original_value = value
    original_display = "" if value is None else str(value)

    def text_getter(w=w):
        if w.text() == original_display:
            return original_value
        return w.text()

    return w, text_getter


def _int_of(w):
    def get():
        txt = w.text().strip()
        return int(txt) if txt.lstrip("-").isdigit() else None
    return get


def _float_of(w):
    def get():
        try:
            return float(w.text().strip())
        except ValueError:
            return None
    return get


def _int4_of(w):
    def get():
        parts = [p.strip() for p in w.text().split(",") if p.strip()]
        if not parts:
            return []
        if len(parts) != 4 or not all(p.lstrip("-").isdigit() for p in parts):
            return None
        return [int(p) for p in parts]
    return get


class StepCard(QFrame):
    """单个步骤卡片: 折叠=摘要行,展开=参数表单(高级参数另收一层)

    sub_index 为 None 时是顶层步骤卡片;否则是 else 子步骤卡片(缩进显示)。
    """

    def __init__(self, main, index, parent_index=None, sub_index=None):
        super().__init__()
        self.main = main
        self.index = index
        self.parent_index = parent_index
        self.sub_index = sub_index
        self.is_sub = sub_index is not None
        if self.is_sub:
            self.step = main.steps[parent_index].setdefault("else", [])[sub_index]
        else:
            self.step = main.steps[index]
        self.getters = {}
        self.widgets = {}  # 表单控件引用(测试/程序化设值用)
        self.key = (index,) if not self.is_sub else (parent_index, sub_index)
        if self.is_sub:
            self.expanded = (main.expanded_key == self.key)
        else:
            # 顶层卡片: 本身展开,或其 else 子步骤处于展开态,都视为打开
            ek = main.expanded_key
            self.expanded = (ek == self.key or
                             (isinstance(ek, tuple) and len(ek) == 2 and ek[0] == index))
        if self.is_sub:
            self.setObjectName("subStepCardOpen" if self.expanded else "subStepCard")
        else:
            self.setObjectName("stepCardOpen" if self.expanded else "stepCard")
        self.setCursor(Qt.PointingHandCursor if not self.expanded else Qt.ArrowCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(30 if self.is_sub else 12, 5, 10, 7)
        lay.setSpacing(5)
        self._build_header(lay)
        if self.expanded:
            self._build_form(lay)

    # ── 折叠态: 编号 + 组件标签 + 摘要 + 徽标 + 操作按钮 ──
    def _build_header(self, lay):
        header = ClickableFrame()
        header.setStyleSheet("QFrame { background: transparent; }")
        header.setCursor(Qt.PointingHandCursor)
        header.clicked.connect(self._header_clicked)
        head = QHBoxLayout(header)
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        if self.is_sub:
            num = QLabel(f"{self.sub_index + 1})")
            num.setFixedWidth(18)
        else:
            num = QLabel(str(self.index + 1))
            num.setFixedWidth(18)
        num.setObjectName("numLabel")
        num.setAlignment(Qt.AlignCenter)
        head.addWidget(num)

        action = self.main._find_action(self.step)
        color = CATEGORY_COLORS.get(action["category"], "#64748b") if action else "#94a3b8"
        chip = QLabel(action["label"] if action else "未知")
        chip.setObjectName("chip")
        chip.setStyleSheet(f"background: {color};")
        head.addWidget(chip)

        self.summary_label = QLabel()
        head.addWidget(self.summary_label, 1)
        self._refresh_summary()

        if self.is_sub:
            ops = [("▲", "上移", lambda: self.main.move_sub(self.parent_index, self.sub_index, -1)),
                   ("▼", "下移", lambda: self.main.move_sub(self.parent_index, self.sub_index, 1)),
                   ("✕", "删除", lambda: self.main.del_sub(self.parent_index, self.sub_index))]
        else:
            ops = [("▲", "上移", lambda: self.main.move_step(self.index, -1)),
                   ("▼", "下移", lambda: self.main.move_step(self.index, 1)),
                   ("⧉", "复制", lambda: self.main.dup_step(self.index)),
                   ("✕", "删除", lambda: self.main.del_step(self.index))]
        for text, tip, fn in ops:
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            b.setFixedSize(22, 20)
            if text == "✕":
                b.setStyleSheet("QToolButton:hover { color:#dc2626; background:#fef2f2; }")
            b.clicked.connect(fn)
            head.addWidget(b)
        lay.addWidget(header)

    def mousePressEvent(self, event):
        """点击卡片非头部区域: 折叠态 → 展开;展开态不响应(避免编辑时误收起)"""
        if not self.expanded and event.button() == Qt.LeftButton:
            self.main.expand_card(self.key)
        super().mousePressEvent(event)

    def _header_clicked(self):
        """点击头部行: 展开/收回(toggle);子步骤收回时回到父级"""
        if self.is_sub:
            if self.main.expanded_key == self.key:
                self.main.expand_card((self.parent_index,))
            else:
                self.main.expand_card(self.key)
            return
        if self.main.expanded_key == self.key:
            self.main.expand_card(None)
        elif isinstance(self.main.expanded_key, tuple) and \
                len(self.main.expanded_key) == 2 and self.main.expanded_key[0] == self.index:
            # 父卡片因子步骤展开而打开: 点击头部切换到编辑父卡片本身
            self.main.expand_card(self.key)
        else:
            self.main.expand_card(self.key)

    def _refresh_summary(self):
        # 徽标(else/截图/超时/等待/重试)统一在 schema.step_summary 里拼装
        self.summary_label.setText(schema.step_summary(self.step))

    # ── 展开态: 参数表单 ──
    def _build_form(self, lay):
        action = self.main._find_action(self.step)
        form = QWidget()
        fv = QVBoxLayout(form)
        fv.setContentsMargins(26, 0, 4, 0)
        fv.setSpacing(5)

        # 测试步骤描述(desc)始终可编辑
        desc_grid = QGridLayout()
        desc_grid.setHorizontalSpacing(10)
        self._add_field(desc_grid, 0, {"key": "desc", "label": "测试步骤描述",
                                       "type": "text", "hint": "显示在报告和结果面板"})
        fv.addLayout(desc_grid)

        if action:
            grid = QGridLayout()
            grid.setHorizontalSpacing(10)
            grid.setVerticalSpacing(5)
            r = 0
            for f in action["fields"]:
                if f["type"] == "group":
                    r = self._add_group_field(grid, r, f)
                else:
                    r = self._add_field(grid, r, f)
            if action["fields"]:
                fv.addLayout(grid)
            if action.get("tip"):
                tip = QLabel("提示: " + action["tip"])
                tip.setStyleSheet("color:#b45309; font-size:11px;")
                fv.addWidget(tip)

        # 高级参数(通用字段,默认收起)
        self.adv_btn = QToolButton()
        self.adv_btn.setText("▸ 高级参数(截图/延时/超时/重试)")
        self.adv_btn.setStyleSheet("color:#2563eb;")
        self.adv_btn.clicked.connect(self._toggle_advanced)
        fv.addWidget(self.adv_btn)
        self.adv_grid_host = QWidget()
        adv_grid = QGridLayout(self.adv_grid_host)
        adv_grid.setContentsMargins(0, 0, 0, 0)
        adv_grid.setHorizontalSpacing(10)
        adv_grid.setVerticalSpacing(5)
        adv_fields = [f for f in schema.GENERIC_FIELDS if f["key"] != "desc"]
        for r, f in enumerate(adv_fields):
            self._add_field(adv_grid, r, f)
        self.adv_grid_host.setVisible(False)
        fv.addWidget(self.adv_grid_host)

        # if / if not: else 子步骤管理区(子卡片在 render_cards 中紧随其后渲染)
        if action and action["key"] in ("if", "if not"):
            self._build_else_section(fv)

        lay.addWidget(form)

    def _build_else_section(self, fv):
        row = QHBoxLayout()
        lbl = QLabel("else 分支(条件不满足时执行,子步骤见下方缩进卡片)")
        lbl.setObjectName("fieldLabel")
        row.addWidget(lbl)
        add_btn = QPushButton("＋ 子步骤")
        add_btn.setObjectName("chipBtn")
        add_btn.setMenu(make_action_menu(add_btn, lambda k: self.main.add_sub(self.index, k)))
        row.addWidget(add_btn)
        row.addStretch()
        fv.addLayout(row)

    def _add_field(self, grid, row, field):
        lbl = QLabel(field["label"])
        lbl.setObjectName("fieldLabel")
        grid.addWidget(lbl, row, 0)
        w, getter = _make_field_widget(field, self.step.get(field["key"]))
        w.setMinimumWidth(240)
        w.setMaximumWidth(430)
        grid.addWidget(w, row, 1)
        self.widgets[field["key"]] = w
        self.getters[field["key"]] = getter
        signal = w.toggled if isinstance(w, QCheckBox) else w.editingFinished
        signal.connect(self._write_back)
        return row + 1

    def _add_group_field(self, grid, row, field):
        lbl = QLabel(field["label"])
        lbl.setObjectName("fieldLabel")
        grid.addWidget(lbl, row, 0, Qt.AlignTop)
        host = QWidget()
        hv = QVBoxLayout(host)
        hv.setContentsMargins(0, 0, 0, 0)
        hv.setSpacing(4)
        cur = self.step.get(field["key"])
        cur = cur if isinstance(cur, dict) else {}
        sub_getters = {}
        for sub in field["fields"]:
            row_h = QHBoxLayout()
            sl = QLabel(sub["label"])
            sl.setObjectName("fieldLabel")
            sl.setFixedWidth(90)
            row_h.addWidget(sl)
            w, getter = _make_field_widget(sub, cur.get(sub["key"]))
            w.setMinimumWidth(200)
            row_h.addWidget(w)
            hv.addLayout(row_h)
            sub_getters[sub["key"]] = getter
            signal = w.toggled if isinstance(w, QCheckBox) else w.editingFinished
            signal.connect(self._write_back)
        grid.addWidget(host, row, 1)
        self.getters[field["key"]] = ("__group__", sub_getters)
        return row + 1

    def _toggle_advanced(self):
        vis = not self.adv_grid_host.isVisible()
        self.adv_grid_host.setVisible(vis)
        self.adv_btn.setText("▾ 高级参数(截图/延时/超时/重试)" if vis
                             else "▸ 高级参数(截图/延时/超时/重试)")

    def _write_back(self, *args):
        """表单编辑结束 → 写回步骤,刷新摘要与 YAML"""
        step = self.step
        for key, getter in self.getters.items():
            if isinstance(getter, tuple) and getter[0] == "__group__":
                vals = {k: g() for k, g in getter[1].items()}
                vals = {k: v for k, v in vals.items() if v not in (None, "")}
                step[key] = vals if vals else True
            else:
                v = getter()
                if v is not None:
                    step[key] = v
        self._refresh_summary()
        self.main.on_card_edited()


class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("扫地机用例编排器 - vacuum_app_test")
        self.case_path = None
        self.data = self._empty_data()
        self.case_idx = 0
        self.worker = None
        self.expanded_key = None  # 展开的卡片: (顶层序号,) 或 (父序号, 子序号)
        self._preview_path = None  # 当前预览的截图路径

        root = QWidget()
        v = QVBoxLayout(root)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(5)
        v.addWidget(self._build_toolbar())
        v.addWidget(self._build_env_strip())
        v.addWidget(self._build_chip_strip())
        v.addWidget(self._make_cards_area(), 3)
        v.addWidget(self._make_bottom(), 2)
        self.setCentralWidget(root)
        self.setStyleSheet(STYLESHEET)

        # 快捷键: Ctrl+S 保存
        from PySide6.QtGui import QKeySequence, QShortcut
        QShortcut(QKeySequence("Ctrl+S"), self, self.on_save)

        # 记住上次窗口大小/位置(笔记本外接屏切换也友好)
        self._settings = QSettings("vacuum_test", "case_studio")
        geo = self._settings.value("win/geometry")
        if geo is not None:
            self.restoreGeometry(geo)

        self._load_case_into_ui()
        self.render_cards()
        self._fill_env_from_config()

    # ── 顶部工具栏 ──
    def _build_toolbar(self):
        bar = QWidget()
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        for text, fn, attr in [("新建", self.on_new, "new_btn"),
                               ("打开", self.on_open, "open_btn"),
                               ("保存", self.on_save, "save_btn")]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            setattr(self, attr, b)
            lay.addWidget(b)
        self.file_label = QLabel("未打开文件")
        self.file_label.setStyleSheet("color:#94a3b8;")
        lay.addWidget(self.file_label)
        lay.addSpacing(8)

        self.add_btn = QPushButton("＋ 添加步骤")
        self.add_btn.setStyleSheet("color:#2563eb; border-color:#b9cff5;")
        self.add_btn.setMenu(make_action_menu(self.add_btn, self.add_step))
        lay.addWidget(self.add_btn)

        lay.addStretch()
        pre_btn = QPushButton("前置条件")
        pre_menu = QMenu(pre_btn)
        self.pre_actions = {}
        for key, label in [("restart", "重启 APP"), ("charging", "等待充电"),
                           ("map_load", "等待地图加载"), ("battery", "电量≥50%")]:
            a = QAction(label, pre_menu)
            a.setCheckable(True)
            a.setChecked(True)
            self.pre_actions[key] = a
            pre_menu.addAction(a)
        pre_btn.setMenu(pre_menu)
        lay.addWidget(pre_btn)

        self.run_btn = QPushButton("▶ 运行")
        self.run_btn.setObjectName("runBtn")
        self.run_btn.clicked.connect(self.on_run)
        lay.addWidget(self.run_btn)
        self.stop_btn = QPushButton("■ 停止")
        self.stop_btn.setObjectName("stopBtn")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.on_stop)
        lay.addWidget(self.stop_btn)

        self.status_label = QLabel("就绪")
        self.status_label.setStyleSheet("color:#64748b;")
        lay.addWidget(self.status_label)
        return bar

    def _build_env_strip(self):
        """环境配置条(流式布局,窗口窄时自动换行): 设备优先,其次测试APP"""
        strip = QFrame()
        strip.setObjectName("chipStrip")
        lay = FlowLayout(strip, margin=5, spacing=6)
        self.env_strip = strip
        self.env_strip_lay = lay

        # ── 设备(最前) ──
        lay.addWidget(QLabel("设备"))
        self.device_combo = QComboBox()
        self.device_combo.setToolTip("自动检测 adb 在线的真机/模拟器,选择执行设备")
        lay.addWidget(self.device_combo)

        refresh_btn = QPushButton("↻")
        refresh_btn.setToolTip("重新检测在线设备")
        refresh_btn.clicked.connect(self._refresh_devices)
        lay.addWidget(refresh_btn)

        sep = QLabel("|")
        sep.setStyleSheet("color:#e2e8f0;")
        lay.addWidget(sep)

        # ── 测试APP ──
        lay.addWidget(QLabel("测试APP"))
        self.app_name_edit = QLineEdit()
        self.app_name_edit.setFixedWidth(88)
        self.app_name_edit.setToolTip("测试APP名称(中文/英文均可),作为包名自动检测依据")
        self.app_name_edit.setProperty("cfg_key", "app.name")
        self.app_name_edit.editingFinished.connect(self._save_env_field)
        lay.addWidget(self.app_name_edit)

        self.detect_btn = QPushButton("🔍 检测")
        self.detect_btn.setToolTip("按APP名称在设备已装应用中匹配包名,并自动解析启动页")
        self.detect_btn.clicked.connect(self.on_detect_app)
        lay.addWidget(self.detect_btn)

        # 包名/启动页不常驻界面: 点「检测」后结果直接显示在右侧状态文字,并写入配置

        lay.addWidget(QLabel("设备名称"))
        self.device_name_edit = QLineEdit()
        self.device_name_edit.setFixedWidth(60)
        self.device_name_edit.setToolTip("APP 内的扫地机设备名称(如 SE3L),前置阶段自动点击进入该设备页")
        self.device_name_edit.setProperty("cfg_key", "target_device")
        self.device_name_edit.editingFinished.connect(self._save_env_field)
        lay.addWidget(self.device_name_edit)

        self.env_status = QLabel("")
        self.env_status.setStyleSheet("color:#94a3b8;")
        lay.addWidget(self.env_status)
        return strip

    def _fill_env_from_config(self):
        cfg = {}
        try:
            cfg = load_config()
        except Exception:
            pass
        app = cfg.get("app", {})
        self.app_name_edit.setText(app.get("name", ""))
        self.device_name_edit.setText(cfg.get("target_device", ""))
        self._refresh_devices()

    # ── 设备检测 ──
    def _refresh_devices(self):
        """adb devices 自动检测真机/模拟器,配置里有备注名的一并显示"""
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        names = {}
        try:
            cfg = load_config()
            names = {d["id"]: d.get("name", "")
                     for d in cfg.get("device", {}).get("list", [])}
        except Exception:
            pass
        try:
            devices = app_detect.list_devices()
        except Exception as e:
            devices = []
            self.env_status.setText(f"adb 检测失败: {e}")
        if not devices:
            self.device_combo.addItem("未检测到设备(检查 adb)", None)
        for dev in devices:
            did = dev["id"]
            label = f"{names[did]} ({did})" if names.get(did) else did
            if dev["state"] != "device":
                label += f" [{dev['state']}]"
            self.device_combo.addItem(label, did)
        self.device_combo.blockSignals(False)

    def _current_device_id(self):
        return self.device_combo.currentData()

    # ── APP/设备配置 ──
    def _save_env_field(self):
        """测试APP/包名/启动页/设备名称 编辑 → 写回 config.yaml"""
        self._save_cfg_field(self.sender())

    def _save_cfg_field(self, w):
        if w is None:
            return
        key = w.property("cfg_key")
        value = w.text().strip()
        if not key or not value:
            return
        try:
            update_config({key: value})
            self.env_status.setText(f"配置已保存: {key} = {value}")
        except Exception as e:
            QMessageBox.critical(self, "保存配置失败", str(e))

    def on_detect_app(self):
        """按 APP 名称在设备已装应用中匹配包名,并解析启动页"""
        app_name = self.app_name_edit.text().strip()
        if not app_name:
            QMessageBox.warning(self, "提示", "请先填写被测APP名称")
            return
        device_id = self._current_device_id()
        if not device_id:
            QMessageBox.warning(self, "提示", "未检测到在线设备,无法检测包名")
            return
        self.env_status.setText("检测中...")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            packages = app_detect.list_packages(device_id)
            matched = app_detect.match_packages(app_name, packages)
            if not matched:
                QMessageBox.warning(
                    self, "未匹配到APP",
                    f"按名称「{app_name}」未匹配到已装应用。\n"
                    "可换个别名(如英文名)重试,或直接手动填写包名。")
                self.env_status.setText("")
                return
            if len(matched) == 1 or matched[0][1] > matched[1][1]:
                package = matched[0][0]
            else:
                from PySide6.QtWidgets import QInputDialog
                options = [p for p, _ in matched[:8]]
                sel, ok = QInputDialog.getItem(
                    self, "选择APP", "匹配到多个应用,请选择:", options, 0, False)
                if not ok:
                    self.env_status.setText("")
                    return
                package = sel
            activity = app_detect.detect_main_activity(device_id, package) or ""
            update_config({"app.package": package, "app.main_activity": activity})
            self.env_status.setText(
                f"检测结果: {package} → {activity or '启动页未识别'}(已写入配置)")
        except Exception as e:
            QMessageBox.critical(self, "检测失败", f"{type(e).__name__}: {e}")
            self.env_status.setText("")
        finally:
            QApplication.restoreOverrideCursor()

    def _build_chip_strip(self):
        """用例信息行 + 常用组件快捷条(流式布局)"""
        strip = QFrame()
        strip.setObjectName("chipStrip")
        lay = FlowLayout(strip, margin=5, spacing=6)

        lay.addWidget(QLabel("组"))
        self.module_edit = QLineEdit()
        self.module_edit.setFixedWidth(96)
        self.module_edit.setToolTip("用例组名,也是 Excel 报告的 sheet 名")
        self.module_edit.editingFinished.connect(self._sync_header)
        lay.addWidget(self.module_edit)
        lay.addWidget(QLabel("用例"))
        self.case_name_edit = QLineEdit()
        self.case_name_edit.setObjectName("caseName")
        self.case_name_edit.setFixedWidth(120)
        self.case_name_edit.editingFinished.connect(self._sync_header)
        lay.addWidget(self.case_name_edit)

        # 多 case 文件的用例切换器(单 case 文件自动隐藏)
        self.case_combo = QComboBox()
        self.case_combo.setToolTip("该文件包含多个用例,选择当前编辑的用例")
        self.case_combo.currentIndexChanged.connect(self._on_case_switched)
        self.case_combo.setVisible(False)
        lay.addWidget(self.case_combo)
        lay.addWidget(QLabel("优先级"))
        self.priority_combo = QComboBox()
        self.priority_combo.addItems(["P0", "P1", "P2"])
        self.priority_combo.setFixedWidth(64)
        self.priority_combo.currentTextChanged.connect(self._sync_header)
        lay.addWidget(self.priority_combo)
        lay.addWidget(QLabel("间隔s"))
        self.case_wait_edit = QLineEdit()
        self.case_wait_edit.setFixedWidth(46)
        self.case_wait_edit.setValidator(QIntValidator(0, 9999))
        self.case_wait_edit.editingFinished.connect(self._sync_header)
        lay.addWidget(self.case_wait_edit)

        line = QLabel("|")
        line.setStyleSheet("color:#e2e8f0;")
        lay.addWidget(line)

        self._quick_btns = []
        for key in QUICK_ACTIONS:
            if key == "__wait":
                label, cat = "延时", "流程控制"
            else:
                a = schema.ACTION_BY_KEY[key]
                label, cat = a["label"], a["category"]
            b = QPushButton(label)
            b.setObjectName("chipBtn")
            b.setToolTip(f"添加「{label}」步骤(分类: {cat})")
            b.clicked.connect(lambda _, k=key: self.add_step(k))
            lay.addWidget(b)
            self._quick_btns.append(b)
        tip = QLabel("更多动作见右上「＋添加步骤」;点卡片展开编辑参数")
        tip.setStyleSheet("color:#b6c0cd;")
        lay.addWidget(tip)
        self.step_count_label = QLabel("")
        self.step_count_label.setStyleSheet("color:#2563eb; font-weight:bold;")
        lay.addWidget(self.step_count_label)
        collapse_btn = QPushButton("收起全部")
        collapse_btn.setObjectName("chipBtn")
        collapse_btn.setToolTip("折叠全部展开中的步骤卡片")
        collapse_btn.clicked.connect(self._collapse_all)
        lay.addWidget(collapse_btn)
        return strip

    def _collapse_all(self):
        self.expanded_key = None
        self.render_cards()

    # ── 步骤卡片列表 ──
    def _make_cards_area(self):
        self.cards_scroll = QScrollArea()
        self.cards_scroll.setWidgetResizable(True)
        host = QWidget()
        self.cards_lay = QVBoxLayout(host)
        self.cards_lay.setContentsMargins(2, 2, 6, 2)
        self.cards_lay.setSpacing(6)
        self.cards_lay.addStretch()
        self.cards_scroll.setWidget(host)
        return self.cards_scroll

    def render_cards(self):
        while self.cards_lay.count() > 1:  # 末尾 stretch 保留
            item = self.cards_lay.takeAt(0)
            if item.widget():
                item.widget().hide()  # 先隐藏再延迟销毁,杜绝重渲染瞬间残留
                item.widget().deleteLater()
        for i in range(len(self.steps)):
            step = self.steps[i]
            self.cards_lay.insertWidget(self.cards_lay.count() - 1, StepCard(self, i))
            # 父卡片(或其子步骤)展开时,else 子步骤以缩进子卡片紧随其后
            ek = self.expanded_key
            if ek and ek[0] == i and isinstance(step.get("else"), list):
                for j in range(len(step["else"])):
                    self.cards_lay.insertWidget(
                        self.cards_lay.count() - 1, StepCard(self, i, parent_index=i, sub_index=j))
        self.step_count_label.setText(f"共 {len(self.steps)} 步")
        self._refresh_yaml_text()

    def expand_card(self, key):
        # 收起焦点控件,触发 editingFinished 把未提交的编辑写回步骤
        focused = self.focusWidget()
        if focused is not None:
            focused.clearFocus()
        self.expanded_key = key
        self.render_cards()

    def on_card_edited(self):
        self._refresh_yaml_text()

    # ── 步骤操作 ──
    def add_step(self, key):
        if self.worker:
            return
        if key == "__wait":
            step = {"desc": "延时等待", "wait": 10}
        else:
            step = schema.new_step(key)
        self.steps.append(step)
        self.expanded_key = (len(self.steps) - 1,)
        self.render_cards()
        self._scroll_to_end()

    def _scroll_to_end(self):
        bar = self.cards_scroll.verticalScrollBar()
        QTimer.singleShot(0, lambda: bar.setValue(bar.maximum()))

    def move_step(self, index, delta):
        if self.worker:
            return
        new = index + delta
        if not (0 <= new < len(self.steps)):
            return
        self.steps[index], self.steps[new] = self.steps[new], self.steps[index]
        if self.expanded_key == (index,):
            self.expanded_key = (new,)
        self.render_cards()

    def dup_step(self, index):
        if self.worker:
            return
        self.steps.insert(index + 1, copy.deepcopy(self.steps[index]))
        self.expanded_key = (index + 1,)
        self.render_cards()

    def del_step(self, index):
        if self.worker:
            return
        del self.steps[index]
        if self.expanded_key and self.expanded_key[0] >= len(self.steps):
            self.expanded_key = (len(self.steps) - 1,) if self.steps else None
        self.render_cards()

    # ── else 子步骤操作 ──
    def _else_list(self, parent_index):
        return self.steps[parent_index].setdefault("else", [])

    def add_sub(self, parent_index, action_key):
        if self.worker:
            return
        if action_key == "__wait":
            step = {"desc": "延时等待", "wait": 10}
        else:
            step = schema.new_step(action_key)
        else_list = self._else_list(parent_index)
        else_list.append(step)
        self.expanded_key = (parent_index, len(else_list) - 1)
        self.render_cards()
        self._scroll_to_end()

    def move_sub(self, parent_index, sub_index, delta):
        if self.worker:
            return
        else_list = self.steps[parent_index].get("else") or []
        new = sub_index + delta
        if not (0 <= new < len(else_list)):
            return
        else_list[sub_index], else_list[new] = else_list[new], else_list[sub_index]
        if self.expanded_key == (parent_index, sub_index):
            self.expanded_key = (parent_index, new)
        self.render_cards()

    def dup_sub(self, parent_index, sub_index):
        if self.worker:
            return
        else_list = self._else_list(parent_index)
        else_list.insert(sub_index + 1, copy.deepcopy(else_list[sub_index]))
        self.expanded_key = (parent_index, sub_index + 1)
        self.render_cards()

    def del_sub(self, parent_index, sub_index):
        if self.worker:
            return
        else_list = self.steps[parent_index].get("else") or []
        if 0 <= sub_index < len(else_list):
            del else_list[sub_index]
        if self.expanded_key and len(self.expanded_key) == 2 and self.expanded_key[0] == parent_index:
            self.expanded_key = (parent_index,)  # 收回到父卡片
        self.render_cards()

    # ── 数据模型 ──
    def _empty_data(self):
        return {"module": "新用例组", "cases": [{"name": "新用例", "priority": "P1", "steps": []}]}

    @property
    def current_case(self):
        cases = self.data.setdefault("cases", [])
        if not cases:
            cases.append({"name": "新用例", "priority": "P1", "steps": []})
        self.case_idx = min(self.case_idx, len(cases) - 1)
        return cases[self.case_idx]

    @property
    def steps(self):
        return self.current_case.setdefault("steps", [])

    def _load_case_into_ui(self):
        c = self.current_case
        self.module_edit.setText(self.data.get("module", ""))
        self.case_name_edit.setText(c.get("name", ""))
        pri = str(c.get("priority", "P1"))
        self.priority_combo.setCurrentIndex(max(0, self.priority_combo.findText(pri)))
        self.case_wait_edit.setText("" if c.get("wait") is None else str(c["wait"]))

    def _sync_header(self):
        self.data["module"] = self.module_edit.text().strip() or "未命名"
        c = self.current_case
        c["name"] = self.case_name_edit.text().strip() or "未命名用例"
        c["priority"] = self.priority_combo.currentText()
        txt = self.case_wait_edit.text().strip()
        c["wait"] = int(txt) if txt.isdigit() else None
        self._refresh_case_item_text()
        self._refresh_yaml_text()

    # ── 多 case 切换器 ──
    def _refresh_case_selector(self):
        cases = self.data.get("cases", [])
        self.case_combo.blockSignals(True)
        self.case_combo.clear()
        for i, c in enumerate(cases):
            self.case_combo.addItem(f"{i + 1}. {c.get('name', '未命名')}")
        self.case_combo.setCurrentIndex(self.case_idx)
        self.case_combo.blockSignals(False)
        self.case_combo.setVisible(len(cases) > 1)

    def _refresh_case_item_text(self):
        if self.case_combo.count() > self.case_idx:
            self.case_combo.blockSignals(True)
            self.case_combo.setItemText(
                self.case_idx, f"{self.case_idx + 1}. {self.current_case.get('name', '未命名')}")
            self.case_combo.blockSignals(False)

    def _on_case_switched(self, index):
        if index < 0 or index == self.case_idx:
            return
        self.case_idx = index
        self.expanded_key = None
        self._load_case_into_ui()
        self.render_cards()

    # ── 执行期间锁定编排区 ──
    def _set_locked(self, locked):
        self._locked = locked
        widgets = [self.new_btn, self.open_btn, self.save_btn, self.add_btn,
                   self.case_name_edit, self.module_edit, self.priority_combo,
                   self.case_wait_edit, self.case_combo,
                   self.yaml_refresh_btn, self.yaml_apply_btn]
        widgets += self._quick_btns
        for w in widgets:
            w.setEnabled(not locked)
        self.cards_scroll.widget().setEnabled(not locked)  # 卡片区只读(仍可滚动)

    def _find_action(self, step):
        for key in step:
            if key in schema.ACTION_BY_KEY:
                return schema.ACTION_BY_KEY[key]
        return None

    # ── 底部标签页 ──
    def _make_bottom(self):
        splitter = QSplitter(Qt.Vertical)
        self.tabs = QTabWidget()

        # 执行结果
        result_split = QSplitter(Qt.Horizontal)
        self.result_table = QTableWidget(0, 5)
        self.result_table.setHorizontalHeaderLabels(["#", "结果", "步骤", "耗时", "错误信息"])
        self.result_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.result_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.result_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.result_table.verticalHeader().setVisible(False)
        self.result_table.cellDoubleClicked.connect(self.on_result_row)
        self.result_table.itemSelectionChanged.connect(self._on_result_selection)
        result_split.addWidget(self.result_table)
        self.preview = ClickableLabel("单击结果行显示对应截图\n点击图片可放大查看")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumWidth(240)
        self.preview.clicked.connect(self._zoom_preview)
        self.preview.setCursor(Qt.PointingHandCursor)
        result_split.addWidget(self.preview)
        result_split.setSizes([620, 300])
        self.tabs.addTab(result_split, "执行结果")

        # YAML 源码
        src = QWidget()
        sv = QVBoxLayout(src)
        self.yaml_edit = QPlainTextEdit()
        sv.addWidget(self.yaml_edit)
        srow = QHBoxLayout()
        for text, fn, attr in [("← 从卡片刷新", self._refresh_yaml_text, "yaml_refresh_btn"),
                               ("应用到卡片 →", self._apply_yaml_text, "yaml_apply_btn")]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            setattr(self, attr, b)
            srow.addWidget(b)
        srow.addStretch()
        sv.addLayout(srow)
        self.tabs.addTab(src, "YAML 源码")

        # 运行日志
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.tabs.addTab(self.log_view, "运行日志")

        splitter.addWidget(self.tabs)
        splitter.setSizes([300])
        return splitter

    # ── 文件操作 ──
    def on_new(self):
        if self.worker:
            return
        self.case_path = None
        self.data = self._empty_data()
        self.case_idx = 0
        self.expanded_key = None
        self.file_label.setText("未打开文件")
        self._load_case_into_ui()
        self.render_cards()
        self._refresh_case_selector()

    def on_open(self):
        if self.worker:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "打开用例", CASES_DIR, "YAML 用例 (*.yaml *.yml)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception as e:
            QMessageBox.critical(self, "打开失败", f"YAML 解析失败:\n{e}")
            return
        if not isinstance(data, dict) or not data.get("cases"):
            QMessageBox.warning(self, "格式不符", "文件需包含 module 与 cases 字段")
            return
        self.case_path = path
        self.data = data
        self.case_idx = 0
        self.expanded_key = None
        self.file_label.setText(_safe_relpath(path))
        self._load_case_into_ui()
        self.render_cards()
        self._refresh_case_selector()

    def _dump_data(self):
        return {"module": self.data.get("module", "未命名"),
                "cases": [dict(c, steps=[schema.serialize_step(s) for s in c.get("steps", [])])
                          for c in self.data.get("cases", [])]}

    def on_save(self):
        if self.worker:
            return
        if not self.case_path:
            os.makedirs(CASES_DIR, exist_ok=True)
            path, _ = QFileDialog.getSaveFileName(
                self, "保存用例", os.path.join(CASES_DIR, "新用例.yaml"), "YAML 用例 (*.yaml)")
            if not path:
                return
            self.case_path = path
        self._sync_header()
        # 校验全部用例(多 case 文件逐一检查)
        problems = []
        for ci, case in enumerate(self.data.get("cases", [])):
            label = case.get("name") or f"第{ci + 1}个用例"
            for i, s in enumerate(case.get("steps", [])):
                for err in schema.validate_step(s):
                    problems.append(f"用例[{label}] 步骤{i + 1}: {err}")
        if problems and QMessageBox.question(
                self, "存在参数问题,仍要保存?", "\n".join(problems[:10])) != QMessageBox.Yes:
            return
        with open(self.case_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(self._dump_data(), f, allow_unicode=True, sort_keys=False)
        self.file_label.setText(_safe_relpath(self.case_path))
        self.status_label.setText(f"已保存: {os.path.basename(self.case_path)}")
        self.log_view.appendPlainText(f"[保存] {self.case_path}")

    # ── YAML 源码同步 ──
    def _refresh_yaml_text(self):
        if not hasattr(self, "yaml_edit"):
            return
        self.yaml_edit.setPlainText(
            yaml.safe_dump(self._dump_data(), allow_unicode=True, sort_keys=False))

    def _apply_yaml_text(self):
        if self.worker:
            return
        try:
            data = yaml.safe_load(self.yaml_edit.toPlainText()) or {}
            if not isinstance(data, dict):
                raise ValueError("顶层必须是键值结构")
        except Exception as e:
            QMessageBox.critical(self, "解析失败", f"YAML 有误:\n{e}")
            return
        self.data = data
        self.case_idx = 0
        self.expanded_key = None
        self._load_case_into_ui()
        self.render_cards()
        self._refresh_case_selector()

    # ── 执行 ──
    def on_run(self):
        if self.worker:
            return
        self.on_save()
        if not self.case_path:
            return
        device_id = self.device_combo.currentData()
        pre = {k: a.isChecked() for k, a in self.pre_actions.items()}
        self.result_table.setRowCount(0)
        self._set_preview_placeholder("单击结果行显示对应截图\n点击图片可放大查看")

        self.worker = RunWorker(device_id, self.case_path, pre)
        self.worker.step_done.connect(self.on_step_done)
        self.worker.log_line.connect(self.log_view.appendPlainText)
        self.worker.status.connect(lambda s: self.status_label.setText(s))
        self.worker.finished_run.connect(self.on_run_finished)
        self.worker.start()
        self._set_locked(True)  # 执行期间锁定编排区
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.tabs.setCurrentIndex(0)
        self.log_view.appendPlainText(f"[运行] {self.case_path} 设备={device_id}")

    def on_stop(self):
        if self.worker:
            self.status_label.setText("停止中(当前步骤结束后退出)...")
            self.worker.request_stop()

    def on_step_done(self, result):
        row = self.result_table.rowCount()
        self.result_table.insertRow(row)
        passed = result.get("passed")
        elapsed = result.get("elapsed")
        values = [str(row + 1), "PASS" if passed else "FAIL",
                  result.get("desc", ""),
                  f"{elapsed:.1f}s" if isinstance(elapsed, (int, float)) else "",
                  result.get("error", "")]
        for col, val in enumerate(values):
            item = QTableWidgetItem(val)
            if col == 1:
                item.setForeground(QColor("#16a34a") if passed else QColor("#dc2626"))
            item.setData(Qt.UserRole, result.get("screenshot", ""))
            self.result_table.setItem(row, col, item)
        self.result_table.scrollToBottom()
        shot = result.get("screenshot", "")
        if shot and os.path.exists(shot):
            self._show_screenshot(shot)

    def _on_result_selection(self):
        """单击/键盘选中结果行 → 预览该步骤截图(无截图显示占位)"""
        row = self.result_table.currentRow()
        if row < 0:
            return
        item = self.result_table.item(row, 0)
        shot = item.data(Qt.UserRole) if item else ""
        if shot and os.path.exists(shot):
            self._show_screenshot(shot)
        else:
            self._set_preview_placeholder("该步骤无截图")

    def on_result_row(self, row, _col):
        shot = self.result_table.item(row, 0).data(Qt.UserRole)
        if shot and os.path.exists(shot):
            self._show_screenshot(shot)
        else:
            self._set_preview_placeholder("该步骤无截图")

    def _set_preview_placeholder(self, text):
        self._preview_path = None
        self.preview.setPixmap(QPixmap())
        self.preview.setText(text)

    def _show_screenshot(self, path):
        self._preview_path = path
        pix = QPixmap(path)
        if not pix.isNull():
            self.preview.setPixmap(
                pix.scaled(self.preview.width(), self.preview.height(),
                           Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _zoom_preview(self):
        """点击预览图 → 独立窗口放大查看"""
        if self._preview_path and os.path.exists(self._preview_path):
            dlg = ImageViewDialog(self._preview_path, self)
            dlg.exec()

    def on_run_finished(self, passed, message):
        self.status_label.setText(("✔ " if passed else "✘ ") + message)
        self.log_view.appendPlainText(f"[结束] {message}")
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._set_locked(False)
        self.worker = None

    def closeEvent(self, event):
        self._settings.setValue("win/geometry", self.saveGeometry())
        if self.worker:
            if QMessageBox.question(self, "正在执行", "用例正在执行,停止并退出?") == QMessageBox.Yes:
                self.worker.request_stop()
                self.worker.wait(5000)
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()
