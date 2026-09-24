"""用例编排器主窗口(卡片式简洁版)

布局: 顶部工具栏(文件/设备/前置/运行) + 快捷组件条 + 步骤卡片列表 + 底部结果页签
- 添加步骤: 点快捷组件或「＋添加步骤」分类菜单
- 编辑参数: 点卡片原位展开,高级参数折叠收起
- 数据模型: dict 列表,YAML 文件是唯一持久化格式
"""
import copy
import os
import shutil
import re
import time

import yaml
from PySide6.QtCore import (QEvent, QObject, QPoint, QRect, QSettings, QSize, Qt,
                            QThread, QTimer, Signal)
from PySide6.QtGui import (QAction, QColor, QDoubleValidator, QFontMetrics,
                           QIcon, QIntValidator, QPainter, QPixmap)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
    QFileDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout,
    QHeaderView, QLabel, QLayout, QLineEdit, QListView, QListWidget, QListWidgetItem,
    QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton,
    QScrollArea, QSizePolicy, QSpinBox, QSplitter, QStyle, QStyledItemDelegate,
    QStyleOptionViewItem,
    QTabWidget, QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
)

from core import app_detect
from core.driver import BASE_DIR, load_config, update_config
from core.logger import get_logger

log = get_logger()
from gui import schema
from gui.runner_thread import RunWorker

CASES_DIR = os.path.join(BASE_DIR, "Test_cases")

APP_VERSION = "1.0"   # 与 git tag v1.0 对应(2026-09-21 首个正式版)

# 用例执行状态灯(delegate 绘制在用例名后面;勾选框保持原生)
# idle=未执行置灰 / running=执行中黄 / passed=通过绿 / failed=失败红
CASE_STATE_COLORS = {"idle": "#cbd5e1", "running": "#f5b301",
                     "passed": "#22c55e", "failed": "#ef4444"}
CASE_STATE_ROLE = int(Qt.UserRole) + 1     # item 存状态字符串
_LAMP_SIZE = 12                            # 状态灯直径
_ARROW_W = 44                              # 右侧箭头命中区宽(↑22 + ↓22)
_FIELD_LABEL_W = 96                        # 步骤字段标签列固定宽(对齐整齐)
# ★ 表单里的勾选框(自动截图等)的最小高度。真机抓屏发现: 被压到 13px 高时指示器
#   画不全(悬停态尤其明显)。给足高度 + 关掉 hover 态, 见 _FormCheckBox 注释。
_CHECK_MIN_H = 20


def make_lamp_pixmap(color, size=_LAMP_SIZE):
    """状态灯:抗锯齿实心圆 + 高光"""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(color))
    p.drawEllipse(0, 0, size - 1, size - 1)
    p.setBrush(QColor(255, 255, 255, 150))
    p.drawEllipse(size * 0.22, size * 0.22, size * 0.3, size * 0.3)
    p.end()
    return pm


_LAMP_PIX = {}


def lamp_pixmap(state):
    if state not in _LAMP_PIX:
        _LAMP_PIX[state] = make_lamp_pixmap(CASE_STATE_COLORS[state])
    return _LAMP_PIX[state]


def lamp_icon(state):
    """状态灯 icon(item 的 icon 位 = 勾选框后、用例名前,原生布局不重叠)"""
    key = ("icon", state)
    if key not in _LAMP_PIX:
        _LAMP_PIX[key] = QIcon(lamp_pixmap(state))
    return _LAMP_PIX[key]


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
/* ★ QSS 设置 background 后 disabled 的自动变灰会失效, 必须显式写禁用态 */
QPushButton:disabled { background: #f1f5f9; color: #a8b3c2; border-color: #e4e8ee; }

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
QLineEdit:disabled, QComboBox:disabled { background: #f1f5f9; color: #a0aec0; }   /* 执行锁定态视觉 */
QLineEdit#caseName { font-weight: bold; }
QComboBox::drop-down { border: none; width: 18px; }
QComboBox::down-arrow { image: url(@CHEV_DOWN@); width: 10px; height: 6px; }   /* 下拉箭头标识 */

/* 次数输入框:增减按钮内嵌框内右侧上下两半,细线 chevron 箭头(抗锯齿 PNG) */
QSpinBox {
    background: #ffffff; border: 1px solid #d9dee6; border-radius: 6px;
    padding: 4px 22px 4px 8px; color: #333;
}
QSpinBox:focus { border-color: #2563eb; }
QSpinBox::up-button {
    subcontrol-origin: border; subcontrol-position: top right;
    width: 16px; height: 12px; margin: 3px;   /* margin 收缩:箭头不贴框边圆角 */
    border: none; background: transparent; border-radius: 3px;
}
QSpinBox::down-button {
    subcontrol-origin: border; subcontrol-position: bottom right;
    width: 16px; height: 12px; margin: 3px;
    border: none; background: transparent; border-radius: 3px;
}
QSpinBox::up-button:hover, QSpinBox::down-button:hover,
QSpinBox::up-button:pressed, QSpinBox::down-button:pressed { background: #eef4ff; }
QSpinBox::up-arrow { image: url(@CHEV_UP@); width: 10px; height: 6px; }
QSpinBox::up-arrow:hover, QSpinBox::up-arrow:pressed { image: url(@CHEV_UP_HOVER@); }
QSpinBox::down-arrow { image: url(@CHEV_DOWN@); width: 10px; height: 6px; }
QSpinBox::down-arrow:hover, QSpinBox::down-arrow:pressed { image: url(@CHEV_DOWN_HOVER@); }

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
/* 步骤卡片内勾选框: 固定透明背景, 防止写盘重绘时闪烁 */
QFrame#stepCard QCheckBox, QFrame#stepCardOpen QCheckBox,
QFrame#subStepCard QCheckBox, QFrame#subStepCardOpen QCheckBox { background: transparent; border: none; }
/* ⚠ 不要自定义勾选标记(指示器)的样式 —— 一旦自定义, Qt 就不再绘制原生勾,
   勾会直接看不见(实测踩过两次)。只让容器透明, 勾完全交给 Qt 原生绘制。 */
QPushButton#chipBtn {
    /* ★ 常驻边框: 原来是 transparent(只有 hover 才出现), 按钮不明显(用户反馈) */
    background: #f8fafc; border: 1px solid #d9dee6; border-radius: 12px;
    padding: 3px 12px; color: #475569;
}
QPushButton#chipBtn:hover { border-color: #2563eb; color: #2563eb; background: #eef4ff; }

/* 用例列表:行控件必须透明 —— 全局 QWidget 背景规则若盖到行内控件,
   选中高亮层与控件自绘背景交替重绘,复选框会闪(2026-09-21 实测) */
QListWidget#caseList { background: #ffffff; border: 1px solid #e4e8ee; border-radius: 8px; }
QListWidget#caseList::item { background: transparent; border-radius: 4px; margin: 1px 2px; outline: none; }
QListWidget#caseList::item:focus { outline: none; }   /* 去掉键盘焦点虚线框(用户要求) */
QListWidget#caseList::item:hover { background: #f0f4fa; }
QListWidget#caseList::item:selected { background: #dbe7fb; color: #1e293b; }
/* ★ 勾选框统一透明背景: 全局 QWidget 背景规则会让点击时"高亮层 ↔ 控件自绘背景"
   交替重绘 → 勾选时闪动(用例列表踩过一次, 前置条件对话框同样中招) */
QCheckBox { background: transparent; border: none; }
/* ⚠ 不要自定义勾选标记(指示器)的样式 —— 一旦自定义, Qt 就不再绘制原生勾,
   勾会直接看不见(实测踩过两次)。只让容器透明, 勾完全交给 Qt 原生绘制。 */
/* 勾选框用 Qt 原生样式(用户要求;闪动根源是此前的行内控件叠层,已移除) */

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
/* ★ 左侧必须留够勾选标记的空间: 原来只有 12px, 勾被挤压变形发虚(用户实测).
   26px 是「勾选标记 + 间距」的常规宽度; 不自定义 indicator, 交给 Qt 原生绘制 */
QMenu::item { padding: 6px 24px 6px 12px; border-radius: 5px; }
QMenu::item:selected { background: #eef4ff; color: #2563eb; }
/* 勾选标记交给 Qt 原生绘制 —— 不要自定义指示器样式(尺寸/图片),
   也不要改 item 的左侧 padding, 否则会让原生勾标变形或发虚 */
QMenu::separator { height: 1px; background: #eef1f5; margin: 4px 8px; }

/* 滚动条:细圆角悬浮式,与浅色主题协调 */
QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }
QScrollBar::handle:vertical { background: #ccd4e0; border-radius: 4px; min-height: 32px; }
QScrollBar::handle:vertical:hover { background: #a3b3c9; }
QScrollBar::handle:vertical:pressed { background: #2563eb; }
QScrollBar:horizontal { background: transparent; height: 8px; margin: 2px; }
QScrollBar::handle:horizontal { background: #ccd4e0; border-radius: 4px; min-width: 32px; }
QScrollBar::handle:horizontal:hover { background: #a3b3c9; }
QScrollBar::handle:horizontal:pressed { background: #2563eb; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QMessageBox { background: #ffffff; }
QSplitter::handle { background: transparent; }
"""

# 用例列表行内箭头等小图标所在目录;QSS 的图标占位符替换为绝对路径
_ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
for _name, _file in [("CHEV_UP", "spin_chevron_up.png"),
                     ("CHEV_UP_HOVER", "spin_chevron_up_hover.png"),
                     ("CHEV_DOWN", "spin_chevron_down.png"),
                     ("CHEV_DOWN_HOVER", "spin_chevron_down_hover.png")]:
    STYLESHEET = STYLESHEET.replace(f"@{_name}@",
                                    os.path.join(_ASSETS, _file).replace("\\", "/"))


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
        # ★ 滚轮独占给缩放(在 viewport 层拦截):否则滚轮先滚滚动条,
        #   滚到头才透传给对话框缩放 —— 滚动+缩放同时发生(用户实测)
        self._scroll.viewport().installEventFilter(self)
        self._drag_last = None       # 鼠标拖拽平移的上一位置
        self._scroll.viewport().setCursor(Qt.OpenHandCursor)

    def showEvent(self, event):
        super().showEvent(event)
        # __init__ 里 _fit 时视口尚未布局(尺寸不对),真正显示后重算一次
        self._apply()

    def eventFilter(self, obj, event):
        if obj is self._scroll.viewport():
            t = event.type()
            if t == QEvent.Wheel:
                delta = event.angleDelta().y()
                if delta > 0:
                    self._zoom_in()
                elif delta < 0:
                    self._zoom_out()
                return True        # 吃掉滚轮:滚动条不再响应
            if t == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                self._drag_last = event.pos()
                self._scroll.viewport().setCursor(Qt.ClosedHandCursor)
                return True
            if t == QEvent.MouseMove and self._drag_last is not None:
                d = event.pos() - self._drag_last
                self._drag_last = event.pos()
                # 拖拽平移:内容跟随鼠标(滚动条反向滚动)
                self._scroll.horizontalScrollBar().setValue(
                    self._scroll.horizontalScrollBar().value() - d.x())
                self._scroll.verticalScrollBar().setValue(
                    self._scroll.verticalScrollBar().value() - d.y())
                return True
            if t == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
                self._drag_last = None
                self._scroll.viewport().setCursor(Qt.OpenHandCursor)
                return True
        return super().eventFilter(obj, event)

    def _apply(self):
        if self._pix.isNull():
            return
        pm = self._pix
        if self._zoom is None:  # 适应窗口
            scaled = pm.scaled(self._scroll.viewport().size(),
                               Qt.KeepAspectRatio, Qt.SmoothTransformation)
            # 记录 fit 缩放比(缩小的下限 = 默认大小)
            if not pm.isNull() and pm.width() > 0:
                self._fit_scale = scaled.width() / pm.width()
        else:
            scaled = pm.scaled(max(1, int(pm.width() * self._zoom)),
                               max(1, int(pm.height() * self._zoom)),
                               Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.image_label.setPixmap(scaled)
        # ★ 图比视口小时把 label 撑到视口大小(配合 AlignCenter 居中显示);
        #   否则 QScrollArea 把 widget 放左上角,小图不居中。
        #   ⚠ 用 resize 不用 adjustSize —— adjustSize 会无视 minimumSize 缩回去
        vp = self._scroll.viewport().size()
        self.image_label.setMinimumSize(max(scaled.width(), vp.width()),
                                        max(scaled.height(), vp.height()))
        self.image_label.resize(max(scaled.width(), vp.width()),
                                max(scaled.height(), vp.height()))

    def _set_zoom(self, z):
        # ★ 缩小不能低于「适应窗口」的默认大小(用户要求);fit_scale 在
        #   _apply 的 fit 分支里记录(相对原图的缩放比)
        z = max(getattr(self, "_fit_scale", 0.1), min(8.0, z))
        self._zoom = z
        self._apply()

    def _zoom_in(self):
        self._set_zoom((self._zoom or getattr(self, "_fit_scale", 1.0)) * 1.25)

    def _zoom_out(self):
        if self._zoom is None:
            return        # 已是适应窗口(默认大小),不能再小 —— 此前把 None
                          # 当 1.0 算出更小 zoom,fit 显示反而被"放大"(bug)
        nxt = self._zoom / 1.25
        if nxt <= getattr(self, "_fit_scale", 0.1):
            self._fit()   # 缩到底 = 回到适应窗口
        else:
            self._set_zoom(nxt)

    def _fit(self):
        self._zoom = None
        self._apply()

    def _orig(self):
        self._set_zoom(1.0)


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
    """按分类构造动作选择菜单:一级=分类,二级=该分类下的动作。

    不做平铺长列表 —— 25+ 项一次展开会超出屏幕(用户反馈)。
    on_pick(action_key) 在选中动作时回调。
    """
    menu = QMenu(parent)
    for cat in schema.CATEGORY_ORDER:
        acts = [a for a in schema.ACTIONS if a["category"] == cat]
        if not acts:
            continue
        sub = QMenu(cat, menu)
        for a in acts:
            act = QAction(a["label"], sub)
            act.triggered.connect(lambda _, k=a["key"]: on_pick(k))
            sub.addAction(act)
        menu.addMenu(sub)
    sub = QMenu("其他", menu)
    wait_act = QAction("延时等待", sub)
    wait_act.triggered.connect(lambda: on_pick("__wait"))
    sub.addAction(wait_act)
    menu.addMenu(sub)
    return menu



def _make_field_widget(field, value, steps=None, exclude_index=None):
    """按 schema 字段类型建控件,返回 (widget, 取值getter)。
    steps/exclude_index: stepshot 类型(基准图选步骤下拉)用的上下文"""
    t = field["type"]
    hint = field.get("hint", "")
    if t == "template":
        from core import vision
        combo = _FitCombo()                 # 宽度自适应 + popup 尺寸按最长模板名
        combo.setEditable(True)
        combo.addItems(vision.list_templates())
        if value:
            combo.setCurrentText(str(value))
        combo.lineEdit().setPlaceholderText(hint or "选择模板")
        combo.currentTextChanged.connect(lambda _t, c=combo: c.fit_width_to_items())
        combo.fit_width_to_items()
        return combo, combo.currentText
    if t == "stepshot":
        # 基准图下拉: 选项 = 当前用例中开启了自动截图的其他步骤(用户要求)
        combo = _FitCombo()                 # 宽度自适应: "步骤N: 描述" 可能较长
        options = []
        for i, st in enumerate(steps or []):
            if i == exclude_index or not st.get("screenshot"):
                continue
            desc = st.get("desc") or "(无描述)"
            options.append((f"step:{i + 1}", f"步骤{i + 1}: {desc}"))
        for v, label in options:
            combo.addItem(label, v)
        if value and value not in [v for v, _ in options]:
            combo.addItem(f"(手动路径) {value}", value)   # 旧路径/自定义值保留显示
        if combo.count() == 0:
            combo.addItem("(前面没有开启截图的步骤)", "")
            combo.setEnabled(False)
        if value:
            k = combo.findData(value)
            if k >= 0:
                combo.setCurrentIndex(k)
        combo.currentTextChanged.connect(lambda _t, c=combo: c.fit_width_to_items())
        combo.fit_width_to_items()
        return combo, combo.currentData
    if t == "bool":
        w = _FormCheckBox()          # 整块可点 + 高度够, 见类注释(真机闪动/点不动)
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


class _FormCheckBox(QCheckBox):
    """步骤表单里的勾选框(自动截图 / 条件 等)。

    ⚠ 两个坑(真机抓屏实测):
    1. **不能被拉宽**: 表单曾把它拉成 200px 宽, 而 Qt 只认指示器那一小块命中 →
       右边一大片是死区(点不动); 若改成"整块都可点", 又会变成"点旁边空白也选中"
       (用户实测反馈)。所以**保持自然大小**(_add_field 对它跳过宽度限制):
       看到多少就能点多少, 两个毛病都没有。
    2. **悬停时"闪动"**: 真机抓屏(1.5x DPI)看到 —— 未悬停时方框四条边完整, 鼠标
       一悬停就只剩左边和上边, 右/下边整块缺失, 移开又恢复 ⇒ 一进一出闪两下。
       修法: 关掉该控件的 hover 属性, 让它不进入悬停态(勾选框没有需要 hover 的
       行为), 悬停前后像素完全一致(实测 0 差异), 方框两态都完整。
       ⚠ 离屏平台不执行真实绘制, **测不出**这类裁切/悬停态问题, 必须真机抓屏。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setMinimumHeight(_CHECK_MIN_H)
        self.setCursor(Qt.PointingHandCursor)
        self._no_hover()

    def _no_hover(self):
        self.setAttribute(Qt.WA_Hover, False)

    def event(self, ev):
        # ⚠ 构造时关掉 WA_Hover 不够: QSS 在 polish 阶段会把它重新打开, 悬停态就
        #   又画出来了(守护测试抓到)。所以在 polish / 样式变更之后再关一次。
        res = super().event(ev)
        if ev.type() in (QEvent.Polish, QEvent.StyleChange):
            self._no_hover()
        return res

    # 不重写 hitButton: 控件本体就是指示器大小(见 _add_field 不给它拉宽),
    # 于是"看到多少就能点多少", 不会出现点旁边空白也选中(用户明确要求)。


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
        lbl.setFixedWidth(_FIELD_LABEL_W)   # ★ 标签列固定宽(否则被 grid 拉伸)
        lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        grid.addWidget(lbl, row, 0)
        grid.setColumnStretch(1, 1)         # 控件列吃剩余空间(控件靠左, 不居中)
        w, getter = _make_field_widget(field, self.step.get(field["key"]),
                                       steps=self.main.steps,
                                       exclude_index=self.index)
        if isinstance(w, _FormCheckBox):
            pass                     # ★ 勾选框保持自然大小: 拉宽会产生"点不动/点哪儿都算"
        elif isinstance(w, _FitCombo):
            w.fit_width_to_items()   # 选择型下拉: 按最长项(内容驱动, 上限 420)
        else:
            w.setMinimumWidth(200)
            w.setMaximumWidth(300)   # 收紧(原 430 右侧大片留白)
        grid.addWidget(w, row, 1, Qt.AlignLeft)   # 靠左紧贴标签(拉伸列内不居中)
        self.widgets[field["key"]] = w
        self.getters[field["key"]] = getter
        if hasattr(w, "changed"):             # 组合控件(点击模板勾选): 自带 changed 信号
            w.changed.connect(self._write_back)
        elif isinstance(w, QComboBox):        # stepshot 下拉: 选中即写回
            w.currentIndexChanged.connect(self._write_back)
        else:
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
            w, getter = _make_field_widget(sub, cur.get(sub["key"]),
                                           steps=self.main.steps,
                                           exclude_index=self.index)
            if not isinstance(w, _FormCheckBox):
                w.setMinimumWidth(200)     # 勾选框保持自然大小(见 _FormCheckBox)
            row_h.addWidget(w)
            hv.addLayout(row_h)
            sub_getters[sub["key"]] = getter
            if hasattr(w, "changed"):         # 组合控件(点击模板勾选)
                w.changed.connect(self._write_back)
            elif isinstance(w, QComboBox):
                w.currentIndexChanged.connect(self._write_back)
            else:
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


class DeviceScanThread(QThread):
    """设备扫描放后台线程:adb 补连/去重要跑多个子进程(最长达 30s),
    放 GUI 线程会把整个窗口卡成"无响应"(实测)。"""
    done = Signal(list, str)

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self._cfg = cfg

    def run(self):
        try:
            devices = app_detect.resolve_devices(self._cfg)
        except Exception:
            time.sleep(1.5)              # adb server 冷启动等瞬态:重试一次
            try:
                devices = app_detect.resolve_devices(self._cfg)
            except Exception as e2:
                self.done.emit([], str(e2))
                return
        self.done.emit(devices, "")


class _StepsHost(QObject):
    """前置条件里「步骤编辑」的迷你宿主 —— 让 StepCard 等用例编辑组件原样复用。

    StepCard 只依赖宿主这组接口(steps / expanded_key / _find_action /
    expand_card / move_* / dup_* / del_* / add_sub / on_card_edited),
    这里全部实现; 渲染由 _StepsEditor 负责(逻辑与 MainWindow.render_cards 一致,
    含 else 子步骤), 所以前置条件的步骤编辑与用例编辑功能完全相同。
    """

    changed = Signal()

    def __init__(self, steps=None):
        super().__init__()
        self.steps = [dict(x) for x in (steps or [])]
        self.expanded_key = None

    @staticmethod
    def _find_action(step):
        for key in step:
            if key in schema.ACTION_BY_KEY:
                return schema.ACTION_BY_KEY[key]
        return None

    def expand_card(self, key):
        self.expanded_key = key
        self.changed.emit()

    def on_card_edited(self):
        self.changed.emit()

    # ── 与 MainWindow 同名同义的操作 ──
    def move_step(self, index, delta):
        j = index + delta
        if 0 <= index < len(self.steps) and 0 <= j < len(self.steps):
            self.steps[index], self.steps[j] = self.steps[j], self.steps[index]
            self.expanded_key = (j,)
            self.changed.emit()

    def dup_step(self, index):
        if 0 <= index < len(self.steps):
            self.steps.insert(index + 1, copy.deepcopy(self.steps[index]))
            self.expanded_key = (index + 1,)
            self.changed.emit()

    def del_step(self, index):
        if 0 <= index < len(self.steps):
            self.steps.pop(index)
            self.expanded_key = None
            self.changed.emit()

    def add_sub(self, parent_index, action_key):
        if not (0 <= parent_index < len(self.steps)):
            return
        if action_key == "__wait":
            sub = {"desc": "延时等待", "wait": 10}
        else:
            sub = schema.new_step(action_key)
        subs = self.steps[parent_index].setdefault("else", [])
        subs.append(sub)
        self.expanded_key = (parent_index, len(subs) - 1)
        self.changed.emit()

    def move_sub(self, parent_index, sub_index, delta):
        subs = self.steps[parent_index].get("else") or []
        j = sub_index + delta
        if 0 <= sub_index < len(subs) and 0 <= j < len(subs):
            subs[sub_index], subs[j] = subs[j], subs[sub_index]
            self.expanded_key = (parent_index, j)
            self.changed.emit()

    def dup_sub(self, parent_index, sub_index):
        subs = self.steps[parent_index].get("else") or []
        if 0 <= sub_index < len(subs):
            subs.insert(sub_index + 1, copy.deepcopy(subs[sub_index]))
            self.expanded_key = (parent_index, sub_index + 1)
            self.changed.emit()

    def del_sub(self, parent_index, sub_index):
        subs = self.steps[parent_index].get("else") or []
        if 0 <= sub_index < len(subs):
            subs.pop(sub_index)
            self.expanded_key = ((parent_index,) if subs else None)
            self.changed.emit()


class _StepsEditor(QWidget):
    """卡片式步骤编辑(前置条件用): 「＋添加步骤」分类菜单 + 步骤卡片(可展开改参数)。

    与用例编辑区共用 StepCard / make_action_menu / _make_field_widget,
    因此动作、参数、else 子步骤等行为完全一致。
    """

    def __init__(self, steps=None, parent=None, app_group=""):
        super().__init__(parent)
        from core import vision
        self.host = _StepsHost(steps)
        self.host.changed.connect(self.render_cards)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)

        # ★ APP 组选择: 模板/基准图下拉按所选组取模板(用户要求)
        grow = QHBoxLayout()
        grow.addWidget(QLabel("APP 组"))
        self.group_combo = QComboBox()
        self.group_combo.addItems(self._list_groups())
        # 优先用传入的组, 其次当前上下文, 最后**默认第一个组** ——
        # 否则首次打开时组为空 → 模板上下文为空 → 模板下拉读不到模板,
        # 要手动切一次组才有(用户实测)
        want = app_group or vision.current_app_group()
        k = self.group_combo.findText(want) if want else -1
        if k >= 0:
            self.group_combo.setCurrentIndex(k)
        elif self.group_combo.count() > 0:
            self.group_combo.setCurrentIndex(0)
        self.group_combo.setToolTip("选择 APP 组后, 模板下拉会列出该组的模板")
        self.group_combo.currentTextChanged.connect(self._on_group_changed)
        # ★ setCurrentIndex 不触发信号 → 这里显式同步一次模板上下文,
        #   否则首次打开时模板下拉仍为空(要手动切组才有)
        vision.set_template_app_group(self.group_combo.currentText() or "")
        grow.addWidget(self.group_combo)
        grow.addStretch(1)
        v.addLayout(grow)

        bar = QHBoxLayout()
        add_btn = QPushButton("＋ 添加步骤")
        add_btn.setObjectName("chipBtn")
        add_btn.setMenu(make_action_menu(add_btn, self.add_step))
        bar.addWidget(add_btn)
        bar.addStretch(1)
        self.count_label = QLabel("")
        self.count_label.setStyleSheet("color:#94a3b8;")
        bar.addWidget(self.count_label)
        v.addLayout(bar)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        self.cards_lay = QVBoxLayout(inner)
        self.cards_lay.setContentsMargins(2, 2, 2, 2)
        self.cards_lay.setSpacing(4)
        self.cards_lay.addStretch(1)
        scroll.setWidget(inner)
        v.addWidget(scroll, 1)
        self.render_cards()

    @staticmethod
    def _list_groups():
        """Test_cases/ 下的 APP 组目录列表"""
        try:
            root = os.path.join(BASE_DIR, "Test_cases")
            return sorted(d for d in os.listdir(root)
                          if os.path.isdir(os.path.join(root, d))
                          and not d.startswith((".", "_")))
        except Exception:
            return []

    def _on_group_changed(self, group):
        """切换 APP 组 → 重设模板上下文并重建卡片(模板下拉随之刷新)"""
        from core import vision
        vision.set_template_app_group(group or "")
        self.render_cards()

    def add_step(self, key):
        if key == "__wait":
            self.host.steps.append({"desc": "延时等待", "wait": 10})
        else:
            self.host.steps.append(schema.new_step(key))
        self.host.expanded_key = (len(self.host.steps) - 1,)
        self.render_cards()

    def steps(self):
        return self.host.steps

    def render_cards(self):
        """与 MainWindow.render_cards 同逻辑(含 else 子步骤缩进卡片)"""
        while self.cards_lay.count() > 1:      # 末尾 stretch 保留
            item = self.cards_lay.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        for i in range(len(self.host.steps)):
            self.cards_lay.insertWidget(self.cards_lay.count() - 1,
                                        StepCard(self.host, i))
            ek = self.host.expanded_key
            step = self.host.steps[i]
            if ek and ek[0] == i and isinstance(step.get("else"), list):
                for j in range(len(step["else"])):
                    self.cards_lay.insertWidget(
                        self.cards_lay.count() - 1,
                        StepCard(self.host, i, parent_index=i, sub_index=j))
        self.count_label.setText(f"共 {len(self.host.steps)} 步")


class PreconditionEditDialog(QDialog):
    """新增/编辑单个前置条件: 选类型 + 填参数(按类型动态生成表单)"""

    def __init__(self, item=None, parent=None, app_group=""):
        super().__init__(parent)
        from core.session import PRECONDITION_TYPES
        self._types = PRECONDITION_TYPES
        self._app_group = app_group
        self._item = dict(item) if item else None
        self.setWindowTitle("编辑前置条件" if item else "添加前置条件")
        self.setStyleSheet(STYLESHEET)
        v = QVBoxLayout(self)

        v.addWidget(QLabel("类型"))
        self.type_combo = QComboBox()
        for t, spec in self._types.items():
            self.type_combo.addItem(spec["label"], t)
        v.addWidget(self.type_combo)

        self.form_host = QWidget()
        self.form = QFormLayout(self.form_host)
        # ★ 字段列要能拉伸: 否则步骤编辑区被标签列挤窄, 卡片比对话框还宽
        self.form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.form.setRowWrapPolicy(QFormLayout.DontWrapRows)
        v.addWidget(self.form_host)
        self._edits = {}

        btns = QHBoxLayout()
        cancel = QPushButton("取消")
        ok = QPushButton("确定")
        ok.setObjectName("runBtn")
        for b in (cancel, ok):        # 同上: 防回车/焦点变化误关窗
            b.setAutoDefault(False)
            b.setDefault(False)
        cancel.clicked.connect(self.reject)
        ok.clicked.connect(self.accept)
        btns.addStretch(1)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        v.addLayout(btns)

        if item:
            k = self.type_combo.findData(item.get("type"))
            if k >= 0:
                self.type_combo.setCurrentIndex(k)
            self.type_combo.setEnabled(False)
        else:
            # 新增: 默认「自定义(检测文本→执行操作)」—— 最常用(用户要求填名称+操作)
            k = self.type_combo.findData("text_check")
            if k >= 0:
                self.type_combo.setCurrentIndex(k)
        self.type_combo.currentIndexChanged.connect(self._build_form)
        self._build_form()

    def _fit_dialog_size(self):
        """按内容调整对话框大小: 步骤编辑需要更大的编辑区(用户实测: 卡片被压)"""
        big = self.type_combo.currentData() == "steps"
        self.resize(760 if big else 520, 660 if big else 420)

    def accept(self):
        """校验后关闭: 名称必填 / 步骤 YAML 必须能解析"""
        try:
            v = self.values()
        except ValueError as e:
            QMessageBox.warning(self, "格式错误", str(e))
            return
        if v.get("type") == "text_check":
            if not v.get("name"):
                QMessageBox.warning(self, "提示", "请填写前置条件名称")
                return
            if not v.get("wait_text") and not v.get("absent_text"):
                QMessageBox.warning(self, "提示",
                                    "请至少填写一项操作内容(等待文本出现/消失)")
                return
        super().accept()

    def _build_form(self):
        while self.form.count():
            it = self.form.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
        spec = self._types.get(self.type_combo.currentData()) or {}
        self._edits = {}
        for prm in spec.get("params", []):
            cur = (self._item or {}).get(prm["key"], prm.get("default"))
            if prm["type"] == "text_area":
                # ★ 卡片式步骤编辑(与用例编辑区同一套组件: 动作菜单/卡片/
                #   参数表单/else 子步骤), 不再是 YAML 文本框(用户要求)
                steps = (self._item or {}).get("steps") or []
                e = _StepsEditor(steps, app_group=self._app_group)
                e.setMinimumHeight(260)
                self.form.addRow(prm["label"], e)
                self._edits[prm["key"]] = (e, prm)
                continue
            e = QLineEdit()
            e.setText("" if cur is None else str(cur))
            if prm.get("hint"):
                e.setPlaceholderText(prm["hint"])
            self.form.addRow(prm["label"], e)
            self._edits[prm["key"]] = (e, prm)
        self._fit_dialog_size()      # 步骤编辑需要更大的编辑区(卡片不被压)

    def values(self):
        t = self.type_combo.currentData()
        item = {"type": t, "enabled": True}
        if self._item:
            item["enabled"] = bool(self._item.get("enabled", True))
        for k, (e, prm) in self._edits.items():
            if prm["type"] == "text_area":
                parsed = e.steps() if hasattr(e, "steps") else []
                if parsed:
                    item["steps"] = [dict(x) for x in parsed]
                continue
            txt = e.text().strip()
            if prm["type"] == "int":
                try:
                    item[k] = int(txt) if txt else prm.get("default", 0)
                except ValueError:
                    item[k] = prm.get("default", 0)
            elif txt:
                item[k] = txt
        return item


class PreconditionsDialog(QDialog):
    """前置条件设置: 勾选启用 / 编辑 / 删除 / 新增(用户要求可编辑可新增)"""

    def __init__(self, items, parent=None, app_group=""):
        super().__init__(parent)
        from core.session import _item_label
        self._app_group = app_group
        self.setWindowTitle("前置条件设置")
        self.resize(560, 380)
        # ★ 对话框不会自动继承主窗口样式表 → 字体/字号不一致, 勾选框发虚
        self.setStyleSheet(STYLESHEET)
        self._label = _item_label
        self.items = [dict(x) for x in items]
        v = QVBoxLayout(self)
        self._hint_label = QLabel("按顺序依次执行; 取消勾选则不执行; 参数用「编辑」调整")
        v.addWidget(self._hint_label)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["启用 / 前置条件", "编辑", "删除"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setColumnWidth(1, 60)
        self.table.setColumnWidth(2, 60)
        self.table.verticalHeader().setVisible(False)
        # ★ 禁止选中: 点表格空白/行会高亮成蓝框(用户实测), 这里只用行内控件交互
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setFocusPolicy(Qt.NoFocus)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.verticalHeader().setDefaultSectionSize(30)   # 与控件高度匹配
        v.addWidget(self.table)

        row = QHBoxLayout()
        add_btn = QPushButton("＋ 添加前置条件")
        add_btn.setObjectName("chipBtn")
        add_btn.setMenu(self._make_add_menu(add_btn))
        row.addWidget(add_btn)
        row.addStretch(1)
        cancel = QPushButton("取消")
        ok = QPushButton("确定")
        ok.setObjectName("runBtn")
        # ★ 关掉 autoDefault: 否则对话框里任何回车/焦点变化都会触发确定或取消,
        #   表现为"点了某项窗口就关了"(用户实测)
        for b in (cancel, ok):
            b.setAutoDefault(False)
            b.setDefault(False)
        cancel.clicked.connect(self.reject)
        ok.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(ok)
        v.addLayout(row)
        self._render()

    def _render(self):
        # ★ 第一列用「表格原生 checkState」而非 setCellWidget(QCheckBox):
        #   cellWidget 会随单元格重绘被反复绘制 → 勾选时闪烁(用户实测多次)。
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for i, item in enumerate(self.items):
            self.table.insertRow(i)
            it = QTableWidgetItem(self._label(item))
            it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if item.get("enabled", True) else Qt.Unchecked)
            self.table.setItem(i, 0, it)
            eb = QPushButton("编辑")
            eb.setAutoDefault(False)      # ★ 否则回车/焦点变化会误触发
            eb.clicked.connect(lambda _=False, k=i: self._edit(k))
            self.table.setCellWidget(i, 1, eb)
            db = QPushButton("删除")
            db.setAutoDefault(False)
            db.clicked.connect(lambda _=False, k=i: self._del(k))
            self.table.setCellWidget(i, 2, db)
        self.table.blockSignals(False)

    def _on_item_changed(self, item):
        """原生勾选变化 → 同步到 items"""
        if item is None or item.column() != 0:
            return
        r = item.row()
        if 0 <= r < len(self.items):
            self.items[r]["enabled"] = (item.checkState() == Qt.Checked)

    def _toggle(self, idx, on):
        """(兼容入口)设置某项启用状态, 并同步表格原生勾选"""
        if 0 <= idx < len(self.items):
            self.items[idx]["enabled"] = bool(on)
            it = self.table.item(idx, 0)
            if it is not None:
                self.table.blockSignals(True)
                it.setCheckState(Qt.Checked if on else Qt.Unchecked)
                self.table.blockSignals(False)

    def _edit(self, idx):
        dlg = PreconditionEditDialog(self.items[idx], self,
                                     app_group=self._app_group)
        if dlg.exec() == QDialog.Accepted:
            self.items[idx] = dlg.values()
            self._render()

    def _del(self, idx):
        if 0 <= idx < len(self.items):
            self.items.pop(idx)
            self._render()

    def _make_add_menu(self, parent):
        """添加菜单: 按类型分组列出(与「添加步骤」同风格), 选中即加入列表"""
        from core.session import PRECONDITION_TYPES
        menu = QMenu(parent)
        for t, spec in PRECONDITION_TYPES.items():
            a = QAction(spec["label"], menu)
            a.triggered.connect(lambda _=False, k=t: self._add_of_type(k))
            menu.addAction(a)
        return menu

    def _add_of_type(self, type_key):
        """新增前置条件: 弹出编辑框填写名称与参数(用户要求, 不直接默认增加)"""
        dlg = PreconditionEditDialog({"type": type_key, "enabled": True}, self,
                                     app_group=getattr(self, "_app_group", ""))
        if dlg.exec() == QDialog.Accepted:
            self.items.append(dlg.values())
            self._render()
            self.table.scrollToBottom()

    def values(self):
        return self.items


class NewCaseDialog(QDialog):
    """新建用例对话框: 用例名称 + APP 用例组(可选现有组或输入新组)"""

    def __init__(self, groups, parent=None, default_group=None):
        super().__init__(parent)
        self.setWindowTitle("新建用例")
        v = QVBoxLayout(self)
        v.addWidget(QLabel("用例名称"))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("输入新用例的名称")
        v.addWidget(self.name_edit)
        v.addWidget(QLabel("APP 用例组"))
        self.group_combo = _FitCombo()      # 组名长时也能看全
        self.group_combo.setEditable(True)
        self.group_combo.lineEdit().setPlaceholderText("选择现有组或输入新组名")
        if groups:
            # ⚠ 只在此处 addItems 一次 —— 上面再来一次会导致下拉里组名重复
            self.group_combo.addItems(groups)
            self.group_combo.currentTextChanged.connect(
                lambda _t, c=self.group_combo: c.fit_width_to_items())
            self.group_combo.fit_width_to_items()
            # 默认组: 有指定则选中它(用户反馈: 别默认进错组), 否则第一项
            if default_group and default_group in groups:
                self.group_combo.setCurrentIndex(groups.index(default_group))
            else:
                self.group_combo.setCurrentIndex(0)
        v.addWidget(self.group_combo)
        btns = QHBoxLayout()
        cancel = QPushButton("取消")
        ok = QPushButton("创建")
        ok.setObjectName("runBtn")
        cancel.clicked.connect(self.reject)
        ok.clicked.connect(self.accept)
        btns.addStretch(1)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        v.addLayout(btns)
        self.name_edit.setFocus()

    def values(self):
        return (self.name_edit.text().strip(),
                self.group_combo.currentText().strip())


class _HistComboDelegate(QStyledItemDelegate):
    """历史下拉项 delegate: 每项右侧绘制 ✕, 点击即删除该条历史(用户要求)。"""

    _W = 20                    # ✕ 命中区宽度(与 _HistCombo 共用命中判定)

    def paint(self, painter, option, index):
        super().paint(painter, option, index)   # 先画原生文本
        rect = option.rect
        painter.save()
        f = painter.font()
        f.setPixelSize(12)
        painter.setFont(f)
        hovered = bool(option.state & QStyle.State_MouseOver)
        painter.setPen(QColor("#dc2626" if hovered else "#b6c0cc"))
        painter.drawText(QRect(rect.right() - self._W, rect.top(),
                               self._W - 4, rect.height()),
                         Qt.AlignCenter, "\u2715")
        painter.restore()

class _FitCombo(QComboBox):
    """内容自适应的下拉框基类(带/不带历史删除功能的下拉都用它)。

    ★ 解决三个 Qt 坑(2026-09-23 逐个实测):
      1. editable combo 的 AdjustToContents 不随输入变化 → fit_width() 自算
      2. popup 宽度不采用 view.sizeHint/maxWidth → showPopup 后 setFixedSize
      3. sizeHintForRow 在 clear+addItems 后返回 -1 → 高度用首行高×行数
    """

    _POPUP_PAD = 48        # ✕/内边距/安全余量

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setView(_FitListView(self))

    def _font_metrics(self):
        """取用于测量的字体: 可输入下拉用 lineEdit, 只读下拉用自身"""
        le = self.lineEdit()
        return (le or self).fontMetrics()

    def longest_item_width(self):
        fm = self._font_metrics()
        return max([fm.horizontalAdvance(self.itemText(i))
                    for i in range(self.count())] or [0])

    def fit_width(self):
        """宽度 = max(当前值, 最长项) —— 输入型下拉(测试APP/设备名称)。

        ★ 取最长项: 这样输入框与弹出的历史列表**等宽**, 且长条目也能显示完整
        (用户要求: 两者长度一致; 之前按当前值算, 删除后输入框缩短而列表仍宽,
        出现"一个长一个短")
        """
        fm = self._font_metrics()
        w = max(fm.horizontalAdvance(self.currentText()),
                self.longest_item_width()) + 60
        self.setFixedWidth(max(70, min(w, 420)))

    def fit_width_to_items(self):
        """宽度按「最长项」自适应(选择型下拉: 模板/基准图/APP组)"""
        w = self.longest_item_width() + 60
        self.setFixedWidth(max(70, min(w, 420)))

    def _row_height(self):
        """列表行高(px)。

        ★ 不能用 fontMetrics().height()+8 估算: 真机行高由 delegate 的
        sizeHint 决定(约 24px, 受 QSS 字体与 DPI 影响), 比字体高多 4~8px;
        低估会让 popup 高度不足 → 出现滚动条(用户实测: 需要滑动才能看完)。
        """
        view = self.view()
        model = view.model()
        if model is None or model.rowCount() == 0:
            return view.fontMetrics().height() + 8
        # ① 首选「已布局后的真实行高」—— 含 QSS 内边距/DPI 缩放, 最准
        rect = view.visualRect(model.index(0, 0))
        if rect.height() > 0:
            return rect.height()
        # ② 退而求其次: Qt 按实际渲染算的行高
        h = view.sizeHintForRow(0)
        if h > 0:
            return h
        # ③ 最后: delegate.sizeHint(可能不含 QSS padding, 故只是兜底)
        opt = QStyleOptionViewItem()
        opt.font = view.font()
        opt.fontMetrics = QFontMetrics(opt.font)
        size = view.itemDelegate().sizeHint(opt, model.index(0, 0))
        return size.height() if size.height() > 0 else view.fontMetrics().height() + 8

    def fit_popup_now(self):
        """按内容调整 popup 宽度(仅当它正打开时)。

        ★ 只干预**宽度**, 高度交给 Qt —— QComboBoxPrivateContainer 自己会按
        view 内容算高度; 之前用 setFixedSize 连高度一起硬设, 因数不准导致
        「内容多了不变大 / 底部多一行空白」(用户真机实测)。最小干预更稳。
        """
        popup = self.view().window()
        if popup is None or not popup.isVisible():
            return
        # 高度: 先让 view 精确等于内容高, 再让 popup 自己算(含它自身的边框)。
        # ★ 直接给 popup 设"内容高"会少算 2px 边框 → view 差 2px → 出滚动条,
        #   视觉上就是底部多一条空白(用户实测)
        rows = self.count()
        if rows:
            self.view().setFixedHeight(self._row_height() * rows)
            popup.adjustSize()
        else:
            popup.setFixedHeight(30)
        # ★ 宽度必须放在 adjustSize **之后** —— adjustSize 会把宽度也算掉
        if popup.width() != self.width():
            popup.setFixedWidth(self.width())   # 与输入框等宽(用户要求)
        # 诊断日志(真机排查用): 项数 / 实际尺寸 / 行高
        log.info(f"[下拉] 项数={self.count()} 输入框宽={self.width()} "
                 f"popup={popup.width()}x{popup.height()} "
                 f"view={self.view().width()}x{self.view().height()} "
                 f"行高={self._row_height()} "
                 f"滚动条={self.view().verticalScrollBar().maximum()} "
                 f"可见={popup.isVisible()}")

    def showPopup(self):
        super().showPopup()
        self.fit_popup_now()


class _FitListView(QListView):
    """弹出列表: sizeHint 宽度按「最长项」计算。

    ★ combo 显示 popup 时是按 `view.sizeHint()` 决定宽度的, 设 minimumWidth
    无效(所以删除项后宽度不会缩) —— 必须重写 sizeHint 才真正跟随内容。
    """

    _PAD = 48      # ✕ 命中区(20) + 内边距 + 安全余量

    def sizeHint(self):
        s = super().sizeHint()
        model = self.model()
        if model is None:
            return s
        fm = self.fontMetrics()
        longest = 0
        for i in range(model.rowCount()):
            txt = model.data(model.index(i, 0)) or ""
            longest = max(longest, fm.horizontalAdvance(str(txt)))
        return QSize(max(s.width(), longest + self._PAD), s.height())


class _HistCombo(_FitCombo):
    """历史下拉框: 点击项右侧 ✕ 删除该条历史(继承 _FitCombo 的自适应尺寸)。

    ★ 必须用事件过滤器而非 delegate.editorEvent —— QAbstractItemView 处理
    普通点击时**不调用** editorEvent(那只在编辑场景触发), 所以点击会走
    Qt 默认行为把该项填进输入框(用户实测: 点 ✕ 反而填入该记录)。
    在 viewport 上消费 MouseButtonRelease 才能既删除又阻止选中。
    """

    removed = Signal(int)      # 被删项行号

    def __init__(self, parent=None):
        super().__init__(parent)
        self.view().viewport().installEventFilter(self)

    def eventFilter(self, obj, ev):
        if obj is self.view().viewport() and ev.type() == QEvent.MouseButtonRelease:
            idx = self.view().indexAt(ev.pos())
            if idx.isValid():
                rect = self.view().visualRect(idx)
                if ev.pos().x() >= rect.right() - _HistComboDelegate._W:
                    self.removed.emit(idx.row())
                    return True            # 消费事件: 不选中该项
        return super().eventFilter(obj, ev)


class CaseListWidget(QListWidget):
    """用例列表:行内 ↑↓ 箭头 = delegate 绘制 + 命中计算。

    ★ item 保持完全原生(勾选/选中/拖拽/拖拽快照全走 Qt 管线)。
    ⚠ 历史教训:setItemWidget(行内真控件)与原生管线冲突 —— 盖 indicator、
    拦截选中/拖拽、按钮点不到,四连坑(2026-09-21 实测);自定义 QDrag
    重写 startDrag 也会断 InternalMove 管线,都别再碰。
    """

    moved = Signal()

    def __init__(self):
        super().__init__()
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setDefaultDropAction(Qt.MoveAction)
        self._arrow_w = 44          # 右侧箭头命中区宽(↑22 + ↓22)
        self.setItemDelegate(CaseItemDelegate(self))

    def set_owner(self, win):
        self._owner = win

    def mousePressEvent(self, event):
        # 箭头命中:直接执行移动,不进基类(避免同时改选中)
        if event.button() == Qt.LeftButton and getattr(self, "_owner", None):
            row, delta = self.itemDelegate().arrow_hit(event.pos())
            if row is not None:
                item = self.item(row)
                if item is not None:
                    self.setCurrentRow(row)
                    self._owner._move_case(item.data(Qt.UserRole), delta)
                    event.accept()
                    return
        super().mousePressEvent(event)

    def dropEvent(self, event):
        if event.source() is not self:
            return super().dropEvent(event)
        super().dropEvent(event)
        event.accept()
        # ★ 必须延迟到下一拍:dropEvent 内同步 clear() 重建列表会弄坏 Qt
        #   拖拽循环的内部状态(表现为只能拖一次,之后再拖无反应)
        QTimer.singleShot(0, self.moved.emit)


class CaseItemDelegate(QStyledItemDelegate):
    """原生 item 之上补绘右侧 ↑↓ 箭头(纯绘制,不干扰原生列表管线)。
    状态灯不在 delegate 里 —— 画在名字后面需要字体度量,中文字体下会和
    文字重叠(实测);改用 item 的 icon 位:原生布局排在勾选框后、用例名前。"""

    def __init__(self, view):
        super().__init__(view)
        self._view = view

    def paint(self, painter, option, index):
        super().paint(painter, option, index)   # 原生:勾选框 + icon(灯) + 用例名
        rect = option.rect
        painter.save()
        f = painter.font()
        f.setBold(True)
        f.setPixelSize(14)
        painter.setFont(f)
        painter.setPen(QColor("#2563eb" if option.state & QStyle.State_Selected else "#64748b"))
        w = _ARROW_W // 2
        right = rect.right() - 2
        painter.drawText(QRect(right - 2 * w, rect.top(), w, rect.height()),
                         Qt.AlignCenter, "↑")
        painter.drawText(QRect(right - w, rect.top(), w, rect.height()),
                         Qt.AlignCenter, "↓")
        painter.restore()

    def arrow_hit(self, pos):
        """pos(视口坐标)落在哪个箭头上 → (row, ±1);未命中 → (None, 0)"""
        view = self._view
        idx = view.indexAt(pos)
        row = idx.row() if idx.isValid() else -1
        if row < 0:
            return None, 0
        rect = view.visualItemRect(view.item(row))
        if pos.x() < rect.right() - _ARROW_W:
            return None, 0
        mid = rect.right() - _ARROW_W // 2
        return row, (-1 if pos.x() < mid else +1)


class MainWindow(QMainWindow):
    log_signal = Signal(str)      # 后台线程/核心模块的日志 → 运行日志页签

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"APP 自动化测试平台 v{APP_VERSION}")
        # 应用图标(自绘 乐动品牌融合): ico 含 16~256 多尺寸(高分屏不发虚)
        _icon_path = os.path.join(_ASSETS, "app_icon.ico")
        if not os.path.isfile(_icon_path):
            _icon_path = os.path.join(_ASSETS, "app_icon.png")
        if os.path.isfile(_icon_path):
            self.setWindowIcon(QIcon(_icon_path))
        self.case_path = None
        self.data = self._empty_data()
        self.case_idx = 0
        self.worker = None
        self.expanded_key = None  # 展开的卡片: (顶层序号,) 或 (父序号, 子序号)
        self._preview_path = None  # 当前预览的截图路径

        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(5)
        v.addWidget(self._build_toolbar())
        v.addWidget(self._build_status_line())      # ★ 运行状态独立一行(用户要求)
        v.addWidget(self._build_env_strip())
        v.addWidget(self._build_env_status_line())  # ★ 配置提示独立一行(用户要求)
        # ★ 用例列表(左,固定宽 200) | 右列 = 组/用例信息条(上)+ 步骤详情(下)
        #   同一垂直列 —— 步骤详情与组信息同列,不横跨在列表/信息条下方
        #   (2026-09-21 二次调整;列表仍在左侧,右侧是「组信息+步骤详情」)
        case_panel = self._build_case_list()
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(5)
        rv.addWidget(self._build_chip_strip())
        rv.addWidget(self._make_cards_area(), 1)
        mid = QWidget()
        h = QHBoxLayout(mid)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        h.addWidget(case_panel)
        h.addWidget(right, 1)
        v.addWidget(mid, 3)
        v.addWidget(self._make_bottom(), 2)

        self.setCentralWidget(page)
        self.setStyleSheet(STYLESHEET)

        # 快捷键: Ctrl+S 保存
        from PySide6.QtGui import QKeySequence, QShortcut
        QShortcut(QKeySequence("Ctrl+S"), self, self.on_save)

        # 默认窗口大小:固定屏幕可用区域的 80%,居中显示。
        # 不保存/恢复上次窗口几何 —— 保证每次启动尺寸一致(用户要求)
        avail = QApplication.primaryScreen().availableGeometry()
        w, h = int(avail.width() * 0.8), int(avail.height() * 0.8)
        self.resize(w, h)
        self.move(avail.x() + (avail.width() - w) // 2,
                  avail.y() + (avail.height() - h) // 2)

        self._load_case_into_ui()
        self.render_cards()
        self._fill_env_from_config()
        self._attach_log_handler()      # core 日志 → 运行日志页签

    # ── 顶部工具栏 ──
    def _set_status(self, msg, label=None):
        """同时更新界面提示并写运行日志。

        ★ 用户反馈: 保存配置 / APP 检测这类提示原来只 setText 到界面, 不走
        logging → 运行日志里看不到。统一走这里, 界面与日志都有。
        """
        (label or self.env_status).setText(msg)
        if msg:
            log.info(msg)

    def _attach_log_handler(self):
        """把 core.logger 的日志转到 GUI「运行日志」页签。

        用户要求: GUI 的运行(设备检测/前置等待)与保存配置等动作也要在界面可见,
        不能只写进 reports/test.log。
        """
        import logging
        if getattr(self, "_log_handler", None) is not None:
            return
        outer = self

        class _GuiLogHandler(logging.Handler):
            def __init__(self):
                super().__init__(logging.INFO)
                self.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                                     "%H:%M:%S"))

            def emit(self, record):
                try:
                    outer.log_signal.emit(self.format(record))
                except Exception:
                    pass

        self._log_handler = _GuiLogHandler()
        logging.getLogger("vacuum_test").addHandler(self._log_handler)
        self.log_signal.connect(self.log_view.appendPlainText)

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
        self.save_btn.setEnabled(False)   # 初始无修改,保存置灰(有修改才亮起)
        self.file_label = QLabel("未打开文件")
        self.file_label.setStyleSheet("color:#94a3b8;")
        lay.addWidget(self.file_label)
        lay.addStretch()
        pre_btn = QPushButton("前置条件")
        self.pre_menu = QMenu(pre_btn)
        pre_btn.setMenu(self.pre_menu)
        lay.addWidget(pre_btn)
        # 从配置加载前置项(无则用默认 4 项) → 动态生成菜单
        self.preconditions = self._load_preconditions()
        self._build_pre_menu()

        # ── 执行次数 ──
        lay.addWidget(QLabel("次数"))
        self.repeat_spin = QSpinBox()
        self.repeat_spin.setRange(1, 99)
        self.repeat_spin.setValue(1)
        self.repeat_spin.setToolTip("勾选用例队列重复的轮数")
        lay.addWidget(self.repeat_spin)

        self.run_btn = QPushButton("▶ 运行")
        self.run_btn.setObjectName("runBtn")
        self.run_btn.clicked.connect(self.on_run)
        lay.addWidget(self.run_btn)
        self.stop_btn = QPushButton("■ 停止")
        self.stop_btn.setObjectName("stopBtn")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.on_stop)
        lay.addWidget(self.stop_btn)

        return bar

    def _hint_line(self, attr, text, color, align=Qt.AlignLeft):
        """提示行: 独占一整行(**组件下方**, 不挂在组件右边)。

        ★ 用户两次反馈: 「报告已生成(部分执行): ...」「配置已保存: ...」这类长文本
        挂在工具栏/环境条右侧时, 会把那一行整体撑宽 → 窗口跟着变大。所以所有提示都
        做成整行显示, 并且**不让它参与宽度决策**(水平策略 Ignored + 自动换行):
        文本再长也只在自己这行里排布(与截图预览 `preview.setSizePolicy` 同一套路)。
        align: 文本在整行内的对齐(运行状态行居中, 配置提示行左对齐)。
        """
        bar = QWidget()
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(0, 0, 0, 0)
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color:{color};")
        lbl.setWordWrap(True)
        lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        lbl.setAlignment(align)
        lay.addWidget(lbl)
        setattr(self, attr, lbl)
        return bar

    def _build_status_line(self):
        """运行状态行(工具栏下方): 执行中/报告已生成 等。★ 文本居中(用户要求)"""
        return self._hint_line("status_label", "就绪", "#64748b", Qt.AlignCenter)

    def _build_env_status_line(self):
        """环境配置提示行(环境条下方): 配置已保存/已删除历史记录/检测结果 等"""
        return self._hint_line("env_status", "", "#94a3b8")

    # ── 名称输入(自适应 + 历史下拉 + ×清除) ──

    def _make_name_combo(self, tip, cfg_key, hist_key, record_on_save=True):
        """名称输入框: 宽度随内容自适应, 历史值可下拉选择, 条目右侧 ✕ 删除。

        record_on_save=False(测试APP): 编辑不记历史, 只有「检测成功」才记
        (用户要求: 检测不到的 APP 不该进历史)
        """
        combo = _HistCombo()
        combo.setEditable(True)
        combo.setToolTip(tip + " · 可从历史记录下拉选择, 点条目右侧 ✕ 删除该条")
        combo.setProperty("cfg_key", cfg_key)
        combo.setProperty("hist_key", hist_key)
        combo.setProperty("base_tip", tip)
        combo.setProperty("record_on_save", record_on_save)
        combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        combo.setMinimumContentsLength(6)
        combo.addItems(self._load_name_history(hist_key))
        # ★ 下拉每项右侧 ✕ 删除该条历史(用户要求: 删的是历史记忆, 不是输入框)
        combo.setItemDelegate(_HistComboDelegate(combo.view()))
        combo.removed.connect(lambda row, c=combo: self._remove_name_history(c, row))
        # ★ 宽度随内容自适应(editable combo 的 AdjustToContents 不随输入变化)
        combo.currentTextChanged.connect(lambda _t, c=combo: self._refresh_name_tip(c))
        self._refresh_name_tip(combo)
        combo.activated.connect(lambda _i, c=combo: self._save_env_field_of(c))
        combo.lineEdit().editingFinished.connect(lambda c=combo: self._save_env_field_of(c))
        return combo

    @staticmethod
    def _refresh_name_tip(combo):
        """名称框: 宽度自适应(基类 fit_width) + 悬停显示完整值 + popup 尺寸同步"""
        combo.fit_width()
        txt = combo.currentText()
        combo.setToolTip((combo.property("base_tip") or "") +
                         (("当前值: " + txt) if txt else ""))
        combo.fit_popup_now()

    def _remove_name_history(self, combo, row):
        """删除下拉中第 row 条历史(同步 QSettings + 刷新列表, 当前值被删则清空)"""
        hist_key = combo.property("hist_key")
        if not hist_key or row < 0 or row >= combo.count():
            return
        removed_text = combo.itemText(row)
        hist = self._load_name_history(hist_key)
        hist = [v for v in hist if v != removed_text]
        st = QSettings("vacuum_test", "case_studio")
        st.setValue(hist_key, hist)
        keep = combo.currentText()
        removed_current = bool(keep) and keep == removed_text
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(hist)
        if removed_current:
            combo.setCurrentText("")     # 删掉的正是当前值 → 清空
        elif keep:
            combo.setCurrentText(keep)
        combo.blockSignals(False)
        # ★ 信号屏蔽期间宽度不重算, 这里补一次(删除后内容可能变化)
        self._refresh_name_tip(combo)
        self._set_status(f"已删除历史记录: {removed_text}")
        if removed_current:
            # ★ 用户要求: 删的若是「当前生效的值」, 就把它连 config 一起清掉。
            #   否则输入框清空了、配置里还在 → 重启按配置回填回来, 用户看到
            #   的却是"删了下次启动又出现"(真机实测)。
            self._clear_env_field(combo)

    def _reload_name_combo(self, combo):
        """按最新历史重载下拉项(保留当前输入), 并同步宽度"""
        hist_key = combo.property("hist_key")
        if not hist_key:
            return
        keep = combo.currentText()
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(self._load_name_history(hist_key))
        combo.setCurrentText(keep)
        combo.blockSignals(False)
        # ★ 信号被屏蔽期间宽度不会自动重算, 这里显式补一次
        self._refresh_name_tip(combo)

    def _purge_name_history(self, hist_key, value):
        """把某个值从历史中移除(用于「检测不到的 APP 不该记历史」, 用户要求)"""
        if not hist_key or not value:
            return
        hist = self._load_name_history(hist_key)
        if value not in hist:
            return
        hist = [v for v in hist if v != value]
        st = QSettings("vacuum_test", "case_studio")
        st.setValue(hist_key, hist)
        combo = getattr(self, "app_name_edit", None)
        if combo is not None and combo.property("hist_key") == hist_key:
            keep = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(hist)
            combo.setCurrentText(keep)     # 输入框保留用户所写, 只是不再进历史
            combo.blockSignals(False)

    @staticmethod
    def _load_name_history(hist_key):
        st = QSettings("vacuum_test", "case_studio")
        val = st.value(hist_key, [])
        if isinstance(val, str):
            val = [val] if val else []
        return list(val or [])[:10]

    @staticmethod
    def _push_name_history(hist_key, value):
        """把值记入历史(去重, 最近在前, 最多 10 条)"""
        if not value:
            return
        st = QSettings("vacuum_test", "case_studio")
        cur = st.value(hist_key, [])
        if isinstance(cur, str):
            cur = [cur] if cur else []
        cur = [v for v in (cur or []) if v != value]
        cur.insert(0, value)
        st.setValue(hist_key, cur[:10])

    def _save_env_field_of(self, combo):
        """combo 版环境字段保存: 写 config + 记历史 + 刷新下拉项"""
        from PySide6.QtWidgets import QComboBox as _C
        w = combo if isinstance(combo, _C) else self.sender()
        if w is None:
            return
        key = w.property("cfg_key")
        value = w.currentText().strip()
        if not key or not value:
            return
        # ★ 同值不重复保存: 从下拉点选一项时 activated 与 editingFinished 会各发一次
        #   (见 _make_name_combo 的接线), 不去重会写两次盘、运行日志出现两行相同提示
        #   (用户真机实测: 同一秒两条「配置已保存: target_device = ...」)
        if w.property("last_saved_value") == value:
            return
        try:
            update_config({key: value})
            self._set_status(f"配置已保存: {key} = {value}")
        except Exception as e:
            QMessageBox.critical(self, "保存配置失败", str(e))
            return
        w.setProperty("last_saved_value", value)
        hist_key = w.property("hist_key")
        if hist_key and w.property("record_on_save") is not False:
            self._push_name_history(hist_key, value)
            self._reload_name_combo(w)

    def _clear_env_field(self, combo):
        """把该字段对应的配置值清空(值写为 "")

        ★ 用户要求: 点 ✕ 删掉的若是「当前生效的值」, 连 config 一起清 ——
        否则输入框清空了、配置里还留着, 重启又回填回来。

        注意: 这只由「✕ 删除当前值」这个明确动作触发; 输入框手动清空后失焦
        不会清配置(_save_env_field_of 对空值直接 return), 避免误清。
        """
        key = combo.property("cfg_key")
        if not key:
            return
        try:
            update_config({key: ""})
        except Exception as e:
            QMessageBox.critical(self, "清空配置失败", str(e))
            return
        combo.setProperty("last_saved_value", "")   # 空值如今也是"已写盘"
        self._set_status(f"已清空配置: {key}")

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
        # 按最长项自适应宽度:设备标签「MuMu模拟器 (127.0.0.1:7555)」较长,
        # 默认策略会截断显示不全
        self.device_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.device_combo.setMinimumContentsLength(18)
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
        self.app_name_edit = self._make_name_combo(
            "测试APP名称(中文/英文均可),作为包名自动检测依据", "app.name",
            "hist/app_name", record_on_save=False)   # 仅检测成功才记历史
        lay.addWidget(self.app_name_edit)

        self.detect_btn = QPushButton("🔍 检测")
        self.detect_btn.setToolTip("按APP名称在设备已装应用中匹配包名,并自动解析启动页")
        self.detect_btn.clicked.connect(self.on_detect_app)
        lay.addWidget(self.detect_btn)

        # 包名/启动页不常驻界面: 点「检测」后结果直接显示在右侧状态文字,并写入配置

        lay.addWidget(QLabel("设备名称"))
        self.device_name_edit = self._make_name_combo(
            "APP 内的扫地机设备名称(如 SE3L),前置阶段自动点击进入该设备页",
            "target_device", "hist/device_name")
        lay.addWidget(self.device_name_edit)
        # ★ env_status 不放在这条流式布局里(会被长文本撑宽) —— 见 _build_env_status_line
        return strip

    def _fill_env_from_config(self):
        cfg = {}
        try:
            cfg = load_config()
        except Exception:
            pass
        app = cfg.get("app", {})
        # ★ YAML 会把纯数字值解析成 int(如 target_device: 111), 必须转字符串
        #   —— 否则 setCurrentText(int) 抛 TypeError, 窗口构造直接失败(启动"卡住")
        self.app_name_edit.setCurrentText(str(app.get("name") or ""))
        self.device_name_edit.setCurrentText(str(cfg.get("target_device") or ""))
        # ★ 登记"当前已写盘的值": 启动回填的值本来就是 config 里的值, 不算一次修改。
        #   否则用户只是点一下输入框再点到别处(editingFinished, 值没变)就会触发一次
        #   "保存" —— 不但重复写盘, 还会把这个值重新记回历史, 等于把用户刚用 ✕
        #   删掉的历史项又撤销回来(用户真机实测: 删了下次启动又出现)。
        for c in (self.app_name_edit, self.device_name_edit):
            c.setProperty("last_saved_value", c.currentText().strip())
        self._refresh_devices()

    # ── 设备检测 ──
    def _refresh_devices(self):
        """后台线程扫描设备并回填下拉框(检测在 GUI 线程跑会卡死界面)"""
        if getattr(self, "_scan_thread", None) and self._scan_thread.isRunning():
            return                       # 上一次扫描还没完,避免堆积
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        self.device_combo.addItem("正在检测设备...", None)
        self.device_combo.blockSignals(False)
        self._set_status("正在检测设备...")
        try:
            cfg = load_config()
        except Exception:
            cfg = {}
        self._scan_thread = DeviceScanThread(cfg, self)
        self._scan_thread.done.connect(self._fill_devices)
        self._scan_thread.start()

    def _fill_devices(self, devices, err):
        """设备扫描结果回填(GUI 线程,由 DeviceScanThread.done 触发)"""
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        if err:
            self._set_status(f"adb 检测失败: {err}")
        elif devices:
            self.env_status.setText("")
        if not devices:
            self.device_combo.addItem("未检测到设备(检查 adb)", None)
        for dev in devices:
            self.device_combo.addItem(dev["label"], dev["id"])
        self.device_combo.blockSignals(False)

    def _build_case_list(self):
        """用例列表面板:Test_cases/ 下全部用例,复选框勾选参与批量执行。
        每行右侧 ↑/↓ 箭头调整执行顺序,顺序写回各用例 YAML 顶层 case_order 字段。
        位于环境条(设备)下方,与组/用例信息条左右并排。返回面板容器。"""
        panel = QWidget()
        v = QVBoxLayout(panel)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(2)
        head = QHBoxLayout()
        list_title = QLabel("用例列表")   # 与「测试步骤详情」同一标题样式(用户要求)
        list_title.setStyleSheet("color:#2563eb; font-weight:bold; font-size:13px;")
        head.addWidget(list_title)
        head.addStretch(1)
        all_btn = QPushButton("全选")
        all_btn.clicked.connect(lambda: self._set_cases_checked(Qt.Checked))
        head.addWidget(all_btn)
        none_btn = QPushButton("清空")
        none_btn.clicked.connect(lambda: self._set_cases_checked(Qt.Unchecked))
        head.addWidget(none_btn)
        del_btn = QPushButton("删除")
        del_btn.setToolTip("删除当前选中的用例文件, 或整组目录(含组内全部用例)")
        del_btn.clicked.connect(self.on_delete_selected)
        head.addWidget(del_btn)
        v.addLayout(head)

        # ★ self.case_list 必须指向真正的 QListWidget(曾被 panel 覆盖,
        #   导致勾选/运行时 AttributeError)
        self.case_list = CaseListWidget()
        self.case_list.setObjectName("caseList")
        self.case_list.set_owner(self)
        self.case_list.setFixedWidth(200)    # 宽度与设备下拉框视觉对齐;高度随步骤详情区填满右列
        self._case_states = {}               # path → 'running'/'passed'/'failed'(状态灯)
        self._collapsed_groups = set()       # 折叠的组目录名
        self._fill_case_list()
        self.case_list.itemClicked.connect(self._on_case_item_clicked)
        self.case_list.moved.connect(self.on_case_rows_dropped)
        v.addWidget(self.case_list)
        return panel

    # ── 用例排序(case_order 写入用例 YAML) ──

    @staticmethod
    def _read_case_order(path):
        """读用例 YAML 顶层 case_order;缺失/非法返回 None"""
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            v = data.get("case_order")
            return int(v) if isinstance(v, (int, float)) else None
        except Exception:
            return None

    @staticmethod
    def _write_case_order(path, n):
        """文本级写入顶层 case_order 行(不重排 YAML、不丢注释、不翻转换行符;
        update_config 的教训:newline='' 读写,原 CRLF/LF 原样保留)"""
        with open(path, encoding="utf-8", newline="") as f:
            text = f.read()
        new_text, cnt = re.subn(r"(?m)^case_order:.*$", f"case_order: {n}", text, count=1)
        if cnt == 0:
            # 无该行则插入:跳过文件头注释块,插到第一个实质行之前
            eol = "\r\n" if "\r\n" in text else "\n"
            lines = text.splitlines(keepends=True)
            idx = 0
            for ln in lines:
                s = ln.strip()
                if s and not s.startswith("#"):
                    break
                idx += 1
            lines.insert(idx, f"case_order: {n}{eol}")
            new_text = "".join(lines)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(new_text)

    def _list_group_dirs(self):
        """Test_cases/ 下的组目录(APP 分组)列表,按名称排序"""
        if not os.path.isdir(CASES_DIR):
            return []
        return sorted(d for d in os.listdir(CASES_DIR)
                      if os.path.isdir(os.path.join(CASES_DIR, d))
                      and not d.startswith((".", "_")))

    def _fill_case_list(self, order_paths=None):
        """(重)填充用例列表:**按组目录分节显示**(Test_cases/<APP名>/ = 一组),
        组内按 case_order 升序(缺失排末尾)。组头行不可选/不可勾/不可拖,
        点击组头可折叠/展开该组。order_paths 给定时按它排;
        Test_cases/ 外的当前打开文件也追加显示。"""
        import glob
        if order_paths is None:
            infos = []
            for g in self._list_group_dirs():
                gdir = os.path.join(CASES_DIR, g)
                for p in sorted(glob.glob(os.path.join(gdir, "*.yaml"))):
                    infos.append((p, g, self._read_case_order(p)))
            extra = self.case_path
            if (extra and os.path.isfile(extra)
                    and all(os.path.abspath(extra) != os.path.abspath(p) for p, *_ in infos)
                    and extra.lower().endswith((".yaml", ".yml"))
                    and CASES_DIR.lower() not in os.path.abspath(extra).lower()):
                infos.append((extra, "未分组", self._read_case_order(extra)))
            infos.sort(key=lambda it: (it[1].lower(), it[2] is None, it[2] or 0,
                                       os.path.basename(it[0])))
            paths = [p for p, *_ in infos]
            groups = {p: g for p, g, _ in infos}
        else:
            paths = list(order_paths)
            # ★ 折叠组的用例行不在可见列表中, 数据必须补全 ——
            #   否则折叠组会被误判为空组, 错插「暂无用例」占位(用户实测)
            import glob as _glob
            for g in self._list_group_dirs():
                if g in self._collapsed_groups:
                    for p in _glob.glob(os.path.join(CASES_DIR, g, '*.yaml')):
                        if p not in paths:
                            paths.append(p)
            groups = {p: os.path.basename(os.path.dirname(p)) for p in paths}
        checked = set(self._checked_case_paths())
        cur_item = self.case_list.currentItem()
        cur = cur_item.data(Qt.UserRole) if cur_item else None
        self.case_list.clear()
        last_group = object()
        for path in paths:
            group = groups.get(path) or "未分组"
            collapsed = group in self._collapsed_groups
            # ── 组头行(组变化时插入;不可选/不可勾/不可拖;点击折叠/展开) ──
            if group != last_group:
                head = QListWidgetItem(("▸ " if collapsed else "▾ ") + group)
                head.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                hf = head.font()
                hf.setBold(True)
                head.setFont(hf)
                head.setForeground(QColor("#0284c7"))
                head.setData(Qt.UserRole, None)          # 标记: 非用例行(组头)
                head.setData(CASE_STATE_ROLE, group)     # 组名存此供折叠切换
                head.setToolTip(f"{group} 的用例组,点击折叠/展开")
                self.case_list.addItem(head)
                last_group = group
            if collapsed:
                continue               # ★ 折叠组: 该组所有用例行都不填充
            name = os.path.splitext(os.path.basename(path))[0]
            # ★ item 完全原生:checkState 勾选 + text(拖拽快照的名称来源)。
            #   不要用 setItemWidget 做行内控件 —— 会盖 indicator/拦事件/断拖拽
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if path in checked else Qt.Unchecked)
            item.setData(Qt.UserRole, path)
            item.setToolTip(f"{path} | 勾选参与批量执行;点右侧箭头或拖拽调整执行顺序")
            item.setSizeHint(QSize(0, 26))   # 略高于文字行,箭头绘制/命中更从容
            state = self._case_states.get(path, "idle")
            item.setData(CASE_STATE_ROLE, state)
            item.setIcon(lamp_icon(state))   # 灯在勾选框后、用例名前(icon 位,原生布局永不重叠)
            self.case_list.addItem(item)
        # ★ 空组目录(还没建任何用例)也显示组头,提示"暂无用例"(用户要求可见所有 APP 组)
        rendered = {g for g in groups.values()}
        for g in self._list_group_dirs():
            if g in rendered:
                continue
            collapsed_g = g in self._collapsed_groups
            head = QListWidgetItem(("▸ " if collapsed_g else "▾ ") + g)
            head.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            hf = head.font()
            hf.setBold(True)
            head.setFont(hf)
            head.setForeground(QColor("#0284c7"))
            head.setData(Qt.UserRole, None)
            head.setData(CASE_STATE_ROLE, g)
            head.setToolTip(f"{g} 的用例组,点击折叠/展开")
            self.case_list.addItem(head)
            if not collapsed_g:      # ★ 折叠的空组只显示组头, 不显示占位(用户实测 bug)
                hint = QListWidgetItem("　　(暂无用例)")   # 全角缩进: 显示在组头内部层级
                hint.setFlags(Qt.ItemIsEnabled)
                hint.setForeground(QColor("#94a3b8"))
                hint.setData(Qt.UserRole, None)
                self.case_list.addItem(hint)
        for i in range(self.case_list.count()):
            # cur=None(点击组头折叠)时不得匹配组头行(UserRole 同为 None),
            # 否则高亮跳到第一个组 —— 由调用方单独保持被点击组头的选中
            if cur is not None and self.case_list.item(i).data(Qt.UserRole) == cur:
                self.case_list.setCurrentRow(i)
                break

    def on_precondition_failed(self, detail):
        """前置检查未通过(本轮已阻断):弹窗提醒 + 用例灯重置灰(未真正执行)"""
        if self.worker:
            for fp in self.worker.case_files:
                self.set_case_state(fp, "idle")
        QMessageBox.warning(self, "前置检查未通过",
                            f"已阻断本轮执行,后续用例不再运行:\n{detail}\n\n"
                            "请处理设备状态(充电/网络/APP)后重新运行。")

    def _reset_case_lamps_to_running(self, paths):
        """点运行的瞬间:全部勾选用例灯变黄(重新执行时先覆盖上次的绿/红)"""
        for p in paths:
            self.set_case_state(p, "running")

    def on_delete_selected(self):
        """删除当前选中的行: 用例行 = 删除该用例文件;组头行 = 删除整组目录。
        均需确认;当前正在编辑的用例被删时同步清空编辑区"""
        item = self.case_list.currentItem()
        if item is None or self.worker:
            return
        path = item.data(Qt.UserRole)
        if not path:
            # ── 删除整组 ──
            group = item.data(CASE_STATE_ROLE)
            if not group:
                return
            gdir = os.path.join(CASES_DIR, group)
            if not os.path.isdir(gdir):
                return
            n = len([f for f in os.listdir(gdir) if f.endswith((".yaml", ".yml"))])
            if QMessageBox.question(
                    self, "删除 APP 组",
                    f"确定删除组「{group}」及其全部 {n} 条用例文件?\n此操作不可恢复!"
            ) != QMessageBox.Yes:
                return
            shutil.rmtree(gdir)
            self._collapsed_groups.discard(group)
            for p in list(self._case_states):
                if os.path.basename(os.path.dirname(p)) == group:
                    self._case_states.pop(p, None)
            if (self.case_path and os.path.basename(os.path.dirname(
                    os.path.abspath(self.case_path))) == group):
                self._unload_case()
            self._fill_case_list()
            self._set_status(f"已删除组: {group}")
            return
        # ── 删除单个用例 ──
        if not os.path.isfile(path):
            return
        name = os.path.splitext(os.path.basename(path))[0]
        if QMessageBox.question(
                self, "删除用例", f"确定删除用例「{name}」?\n此操作不可恢复!"
        ) != QMessageBox.Yes:
            return
        os.remove(path)
        self._case_states.pop(path, None)
        if self.case_path == path:
            self._unload_case()
        self._fill_case_list()
        self._set_status(f"已删除用例: {name}")

    def set_case_state(self, path, state):
        """设置用例执行状态灯:未执行置灰,执行中黄,通过绿,失败红。
        状态存在 item 的 CASE_STATE_ROLE 上,灯用 icon 位(勾选框后、名字前)"""
        self._case_states[path] = state or "idle"
        for i in range(self.case_list.count()):
            item = self.case_list.item(i)
            if item.data(Qt.UserRole) == path:
                state = self._case_states[path]
                item.setData(CASE_STATE_ROLE, state)
                item.setIcon(lamp_icon(state))
                break

    def _move_case(self, path, delta):
        """用例行上移/下移一行,并把新顺序 1..N 写回各用例 YAML 的 case_order"""
        paths = [self.case_list.item(i).data(Qt.UserRole)
                 for i in range(self.case_list.count())
                 if self.case_list.item(i).data(Qt.UserRole)]
        if path not in paths:
            return
        i = paths.index(path)
        j = i + delta
        if not (0 <= j < len(paths)):
            return
        paths[i], paths[j] = paths[j], paths[i]
        self._fill_case_list(order_paths=paths)
        self.case_list.setCurrentRow(j)
        self._persist_case_order(paths)

    def on_case_rows_dropped(self):
        """拖拽排序松手后:行控件已被 InternalMove 甩掉,重建并写回顺序"""
        paths = [self.case_list.item(i).data(Qt.UserRole)
                 for i in range(self.case_list.count())
                 if self.case_list.item(i).data(Qt.UserRole)]
        if len(paths) < 2:
            return
        self._fill_case_list(order_paths=paths)
        self._persist_case_order(paths)

    def _persist_case_order(self, paths):
        """按给定顺序把 case_order=1..N 写回各用例 YAML"""
        try:
            for n, p in enumerate(paths, 1):
                self._write_case_order(p, n)
            self._set_status("已保存执行顺序到用例文件(case_order)")
            self.log_view.appendPlainText(
                "[排序] " + " → ".join(os.path.splitext(os.path.basename(p))[0]
                                       for p in paths))
        except Exception as e:
            QMessageBox.warning(self, "保存顺序失败", str(e))


    def _on_case_item_clicked(self, item):
        """单击用例行 → 右侧加载该用例编辑;单击组头行 → 折叠/展开该组(用户要求)"""
        if self.worker:
            return
        path = item.data(Qt.UserRole)
        if not path:
            group = item.data(CASE_STATE_ROLE)
            if group:
                if group in self._collapsed_groups:
                    self._collapsed_groups.discard(group)
                else:
                    self._collapsed_groups.add(group)
                    # ★ 收起组时,若当前编辑的用例属于该组 → 详情恢复未选中
                    #   (用例行已隐藏,继续显示其步骤会误导)
                    if (self.case_path and
                            os.path.basename(os.path.dirname(os.path.abspath(self.case_path))) == group):
                        self._unload_case()
                self._fill_case_list()
                # ★ 选中框保持在被点击的组头上(否则重建后 cur=None 会错误
                #   匹配到第一个组头, 高亮跳到别的 APP 组 —— 用户实测)
                for i in range(self.case_list.count()):
                    it = self.case_list.item(i)
                    if (it.data(Qt.UserRole) is None
                            and it.data(CASE_STATE_ROLE) == group):
                        self.case_list.setCurrentRow(i)
                        break
            return
        if not os.path.isfile(path) or path == self.case_path:
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
        self._fill_case_list()   # 打开的用例必须在左侧列表可见

    def on_precondition_failed(self, detail):
        """前置检查未通过(本轮已阻断):弹窗提醒 + 用例灯重置灰(未真正执行)"""
        if self.worker:
            for fp in self.worker.case_files:
                self.set_case_state(fp, "idle")
        QMessageBox.warning(self, "前置检查未通过",
                            f"已阻断本轮执行,后续用例不再运行:\n{detail}\n\n"
                            "请处理设备状态(充电/网络/APP)后重新运行。")



    def _set_cases_checked(self, state):
        for i in range(self.case_list.count()):
            item = self.case_list.item(i)
            if item.data(Qt.UserRole):        # 组头行(UserRole=None)不参与勾选
                item.setCheckState(state)

    def _checked_case_paths(self):
        """勾选的用例文件路径列表(勾选才参与批量执行;按列表顺序=执行顺序)"""
        out = []
        for i in range(self.case_list.count()):
            item = self.case_list.item(i)
            path = item.data(Qt.UserRole)
            if path and item.checkState() == Qt.Checked:
                out.append(path)
        return out

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
            self._set_status(f"配置已保存: {key} = {value}")
        except Exception as e:
            QMessageBox.critical(self, "保存配置失败", str(e))

    def on_detect_app(self):
        """按 APP 名称在设备已装应用中匹配包名,并解析启动页"""
        app_name = self.app_name_edit.currentText().strip()
        if not app_name:
            QMessageBox.warning(self, "提示", "请先填写被测APP名称")
            return
        device_id = self._current_device_id()
        if not device_id:
            QMessageBox.warning(self, "提示", "未检测到在线设备,无法检测包名")
            return
        # ★ 不用全局转圈光标: 模态弹窗(选择/告警)叠加 override 栈不平衡时
        #   会残留转圈(用户实测: APP 未安装时鼠标一直转); 改为按钮禁用+状态提示
        self._set_status("检测中...")
        self.detect_btn.setEnabled(False)
        self.detect_btn.setText("检测中…")
        try:
            packages = app_detect.list_packages(device_id)
            matched = app_detect.match_packages(app_name, packages)
            if matched and (len(matched) == 1 or matched[0][1] > matched[1][1]):
                package = matched[0][0]
            elif matched:
                package = self._pick_app(
                    "匹配到多个应用,请选择:", [p for p, _ in matched[:8]])
            else:
                # 名称与包名对不上(如 SmartThings → com.samsung.android.oneconnect):
                # 列出已装应用让人直接选,别把路堵死
                package = self._pick_app(
                    f"按名称「{app_name}」未匹配到已装应用。\n"
                    "请从设备已装应用中选择(或在 common/app_detect.py 的 "
                    "_APP_ALIASES 里登记别名):",
                    packages)
            if package is None:
                self.env_status.setText("")
                self._purge_name_history("hist/app_name", app_name)   # 未检测到 → 不记历史
                return
            activity = app_detect.detect_main_activity(device_id, package) or ""
            update_config({"app.package": package, "app.main_activity": activity})
            # ★ 检测成功才记历史(用户要求), 并刷新下拉
            self._push_name_history("hist/app_name", app_name)
            self._reload_name_combo(self.app_name_edit)
            self._set_status(
                f"检测结果: {package} → {activity or '启动页未识别'}(已写入配置)")
        except Exception as e:
            QMessageBox.critical(self, "检测失败", f"{type(e).__name__}: {e}")
            self.env_status.setText("")
            self._purge_name_history("hist/app_name", app_name)       # 检测失败 → 不记历史
        finally:
            self.detect_btn.setEnabled(True)
            self.detect_btn.setText("🔍 检测")
            # 兜底: 清空可能残留的全局光标覆盖栈(任何路径都不留转圈)
            while QApplication.overrideCursor() is not None:
                QApplication.restoreOverrideCursor()

    def _pick_app(self, prompt, options):
        """弹选择框,返回选中的包名;取消或无候选返回 None"""
        if not options:
            QMessageBox.warning(self, "未找到应用", "设备上没有可选的第三方应用。")
            return None
        from PySide6.QtWidgets import QInputDialog
        sel, ok = QInputDialog.getItem(self, "选择APP", prompt, options, 0, False)
        return sel if ok else None

    # ── 前置条件(可编辑/可新增) ──

    def _load_preconditions(self):
        """读配置里的前置项列表; 未配置用默认 4 项(行为与旧版一致)"""
        from core.driver import load_preconditions
        from core.session import DEFAULT_PRECONDITIONS
        try:
            items = load_preconditions()
        except Exception:
            items = None
        if not items:
            items = [dict(x) for x in DEFAULT_PRECONDITIONS]
            try:
                from core.driver import save_preconditions
                save_preconditions(items)
            except Exception:
                pass
        return items

    def _current_app_group(self):
        """当前 APP 组 = 正在编辑的用例所在目录名(供模板下拉用)"""
        if not self.case_path:
            return ""
        return os.path.basename(os.path.dirname(os.path.abspath(self.case_path)))

    def _pre_label(self, item):
        from core.session import _item_label
        return _item_label(item)

    def _build_pre_menu(self):
        """按当前前置项列表重建菜单(勾选=启用; 底部「设置…」增删改)"""
        self.pre_menu.clear()
        self.pre_actions = {}
        for i, item in enumerate(self.preconditions):
            a = QAction(self._pre_label(item), self.pre_menu)
            a.setCheckable(True)
            a.setChecked(bool(item.get("enabled", True)))
            a.setToolTip("勾选=执行该前置条件; 需要改参数或增删请点下方「设置前置条件…」")
            a.toggled.connect(lambda on, k=i: self._toggle_precondition(k, on))
            self.pre_menu.addAction(a)
            self.pre_actions[i] = a
        self.pre_menu.addSeparator()
        self.pre_menu.addAction("设置前置条件…", self.on_edit_preconditions)

    def _toggle_precondition(self, idx, on):
        if 0 <= idx < len(self.preconditions):
            self.preconditions[idx]["enabled"] = bool(on)

    def on_edit_preconditions(self):
        """打开设置对话框: 增删改前置条件并写回 config"""
        dlg = PreconditionsDialog(self.preconditions, self,
                                  app_group=self._current_app_group())
        if dlg.exec() != QDialog.Accepted:
            return
        self.preconditions = dlg.values()
        try:
            from core.driver import save_preconditions
            save_preconditions(self.preconditions)
            self._set_status("前置条件已保存", self.status_label)
        except Exception as e:
            QMessageBox.critical(self, "保存失败", str(e))
        self._build_pre_menu()

    def _selected_preconditions(self):
        """当前启用中的前置项(执行时传给 RunWorker)"""
        return [dict(x) for x in self.preconditions if x.get("enabled", True)]

    def _build_chip_strip(self):
        """测试步骤详情区标题 + 用例组/用例/优先级/步骤间隔/添加步骤(流式布局)"""
        strip = QFrame()
        strip.setObjectName("chipStrip")
        lay = FlowLayout(strip, margin=5, spacing=6)

        # ★ 区块标题: 测试步骤详情(用户要求;之后依次是 用例组/用例/优先级/间隔/添加步骤)
        title = QLabel("测试步骤详情")
        title.setStyleSheet("color:#2563eb; font-weight:bold; font-size:13px;")
        lay.addWidget(title)
        lay.addWidget(QLabel("用例组"))
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
        # ★ 「＋添加步骤」排在间隔之后(用户要求的字段顺序)
        self.add_btn = QPushButton("＋ 添加步骤")
        self.add_btn.setObjectName("chipBtn")
        self.add_btn.setToolTip("向当前用例添加步骤")
        self.add_btn.setMenu(make_action_menu(self.add_btn, self.add_step))
        lay.addWidget(self.add_btn)

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

    def _auto_save(self):
        """编辑自动保存(防抖 300ms): 停止编辑后写盘一次,
        避免勾选切换时的连续写盘抖动;「保存」按钮为手动强制保存"""
        if self.worker or not self.case_path:
            return
        if getattr(self, "_autosave_timer", None) is None:
            from PySide6.QtCore import QTimer as _QTimer
            self._autosave_timer = _QTimer(self)
            self._autosave_timer.setSingleShot(True)
            self._autosave_timer.setInterval(300)
            self._autosave_timer.timeout.connect(self._do_auto_save)
        self._autosave_timer.start()

    def _do_auto_save(self):
        if self.worker or not self.case_path:
            return
        if getattr(self, "_edit_timer", None) is not None and self._edit_timer.isActive():
            self._edit_timer.stop()
            self._refresh_yaml_text()   # 写盘前保证 YAML 视图一致
        try:
            prev_order = self._read_case_order(self.case_path)
            with open(self.case_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(self._dump_data(), f, allow_unicode=True, sort_keys=False)
            if prev_order is not None:
                self._write_case_order(self.case_path, prev_order)
            self._dirty = False
            self.status_label.setText(
                f"已自动保存 {os.path.basename(self.case_path)} "
                f"{time.strftime('%H:%M:%S')}")
        except Exception as e:
            self.log_view.appendPlainText(f"[自动保存失败] {e}")

    def on_card_edited(self):
        """卡片编辑 → 防抖刷新 YAML 文本 + 自动保存(勾选连续切换只触发一次,
        避免每次 toggle 都全量重绘导致勾选框闪动)"""
        if getattr(self, "_edit_timer", None) is None:
            self._edit_timer = QTimer(self)
            self._edit_timer.setSingleShot(True)
            self._edit_timer.setInterval(300)
            self._edit_timer.timeout.connect(self._flush_card_edit)
        self._edit_timer.start()

    def _flush_card_edit(self):
        self._refresh_yaml_text()
        self._auto_save()

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
        self._auto_save()
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
        self._auto_save()

    def dup_step(self, index):
        if self.worker:
            return
        self.steps.insert(index + 1, copy.deepcopy(self.steps[index]))
        self.expanded_key = (index + 1,)
        self.render_cards()
        self._auto_save()

    def del_step(self, index):
        if self.worker:
            return
        del self.steps[index]
        if self.expanded_key and self.expanded_key[0] >= len(self.steps):
            self.expanded_key = (len(self.steps) - 1,) if self.steps else None
        self.render_cards()
        self._auto_save()

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
        self._auto_save()
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
        self._auto_save()

    def dup_sub(self, parent_index, sub_index):
        if self.worker:
            return
        else_list = self._else_list(parent_index)
        else_list.insert(sub_index + 1, copy.deepcopy(else_list[sub_index]))
        self.expanded_key = (parent_index, sub_index + 1)
        self.render_cards()
        self._auto_save()

    def del_sub(self, parent_index, sub_index):
        if self.worker:
            return
        else_list = self.steps[parent_index].get("else") or []
        if 0 <= sub_index < len(else_list):
            del else_list[sub_index]
        if self.expanded_key and len(self.expanded_key) == 2 and self.expanded_key[0] == parent_index:
            self.expanded_key = (parent_index,)  # 收回到父卡片
        self.render_cards()
        self._auto_save()

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
        # ★ 模板上下文跟随当前用例的 APP 组(点击模板下拉自动列出该组模板)
        if self.case_path:
            from core import vision as _vision
            _vision.set_template_app_group(
                os.path.basename(os.path.dirname(os.path.abspath(self.case_path))))
        c = self.current_case
        self.module_edit.setText(str(self.data.get("module") or ""))
        self.case_name_edit.setText(str(c.get("name") or ""))
        pri = str(c.get("priority") or "P1")
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
        self.result_table.setHorizontalHeaderLabels(["#", "步骤", "结果", "耗时", "错误信息"])
        # ★ 步骤/错误信息两列拉伸占满;结果列随内容自适应(中文总结行放得下);
        #   此前 Stretch 挂在结果列上,列序调整后步骤/错误信息被挤窄显示不全
        self.result_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.result_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.result_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.result_table.setColumnWidth(0, 40)
        self.result_table.setColumnWidth(3, 64)
        self.result_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.result_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.result_table.verticalHeader().setVisible(False)
        self.result_table.cellDoubleClicked.connect(self.on_result_row)
        self.result_table.itemSelectionChanged.connect(self._on_result_selection)
        result_split.addWidget(self.result_table)
        self.preview = ClickableLabel("单击结果行显示对应截图\n点击图片可放大查看")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumWidth(240)
        # ★ Ignored:pixmap 不参与 sizeHint —— 否则每次点击行,预览图按上次的
        #   放大尺寸再缩放一轮,splitter 越撑越大(正反馈,用户实测)
        self.preview.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
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
        # ★ 自动换行: 默认 NoWrap 会把长日志行在右侧截断(用户反馈"显示不完整")
        self.log_view.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.log_view.setMaximumBlockCount(5000)   # 限制条数, 防止长时间运行涨爆内存
        self.tabs.addTab(self.log_view, "运行日志")

        splitter.addWidget(self.tabs)
        splitter.setSizes([300])
        return splitter

    # ── 文件操作 ──
    def on_new(self):
        """新建用例:弹名称输入框 → 在 Test_cases/ 创建空白用例文件 →
        列表刷新并选中新用例,右侧进入编辑(后续编辑自动保存)"""
        if self.worker:
            return
        groups = sorted(set(self._list_group_dirs()))
        # ★ 默认组 = 当前编辑用例所在的 APP 组(顺着当前 APP 新建);
        #   否则按名称取第一个组 —— 曾因此把新用例默认建进「测试」组(用户实测)
        default_group = None
        if self.case_path:
            g = os.path.basename(os.path.dirname(os.path.abspath(self.case_path)))
            default_group = g if g in groups else None
        if default_group is None:
            default_group = next((g for g in groups
                                  if g == (load_config().get("app") or {}).get("name")), None)
        if default_group is None and groups:
            default_group = groups[0]
        dlg = NewCaseDialog(groups, self, default_group=default_group)
        if dlg.exec() != QDialog.Accepted:
            return
        name, group = dlg.values()
        if not name:
            QMessageBox.warning(self, "新建用例", "用例名称不能为空")
            return
        self.create_case(name, group=group)

    def _unload_case(self):
        """清空右侧编辑区,恢复未选中状态(收起包含当前用例的组时调用)"""
        self.case_path = None
        self.data = self._empty_data()
        self.case_idx = 0
        self.expanded_key = None
        self.file_label.setText("未选中用例")
        self._load_case_into_ui()
        self.render_cards()
        self._refresh_case_selector()

    def create_case(self, name, group):
        """按名称在组目录 Test_cases/<group>/ 下创建空白用例文件
        (已存在则提示)并加载到编辑区/列表"""
        gdir = os.path.join(CASES_DIR, group)
        os.makedirs(gdir, exist_ok=True)
        path = os.path.join(gdir, f"{name}.yaml")
        if os.path.exists(path):
            QMessageBox.warning(self, "新建用例", f"用例已存在: {group}/{name}.yaml")
            return
        self.case_path = path
        self.data = self._empty_data()
        self.data["module"] = name
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(self._dump_data(), f, allow_unicode=True, sort_keys=False)
        self.case_idx = 0
        self.expanded_key = None
        self.file_label.setText(_safe_relpath(path))
        self._load_case_into_ui()
        self.render_cards()
        self._refresh_case_selector()
        self._fill_case_list()      # 新用例出现在列表
        for i in range(self.case_list.count()):
            if self.case_list.item(i).data(Qt.UserRole) == path:
                self.case_list.setCurrentRow(i)   # 选中新用例
                break
        self.status_label.setText(f"已新建用例: {group}/{name}.yaml(编辑自动保存)")
        self.log_view.appendPlainText(f"[新建] {path}")

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
        self._fill_case_list()   # ★ 打开的用例必须在左侧列表可见(Test_cases/ 外的也追加)

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
        # 保存重写整个文件会丢掉顶层 case_order(执行顺序)—— 先记后补
        prev_order = (self._read_case_order(self.case_path)
                      if os.path.isfile(self.case_path) else None)
        with open(self.case_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(self._dump_data(), f, allow_unicode=True, sort_keys=False)
        if prev_order is not None:
            self._write_case_order(self.case_path, prev_order)
        self._fill_case_list()   # 新保存的用例进入列表(勾选/选中状态保留)
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
        self._auto_save()
        self._refresh_case_selector()

    # ── 执行 ──
    def on_run(self):
        if self.worker:
            return
        self.on_save()
        case_files = self._checked_case_paths()
        if not case_files:
            QMessageBox.warning(self, "提示", "请先在左侧勾选要执行的用例")
            return
        device_id = self.device_combo.currentData()
        pre_items = self._selected_preconditions()   # 前置项列表(可编辑/可新增)
        repeat = self.repeat_spin.value()
        self.result_table.setRowCount(0)
        self._set_preview_placeholder("单击结果行显示对应截图\n点击图片可放大查看")

        self.worker = RunWorker(device_id, case_files, pre_items, repeat)
        self.worker.step_done.connect(self.on_step_done)
        # 注: 不再连接 log_line → 运行日志(执行期间的 core 日志已由全局
        #     handler 转发, 再连一次会每条显示两遍)
        self.worker.status.connect(self.on_worker_status)
        self.worker.finished_run.connect(self.on_run_finished)
        # 状态灯:执行中黄 / 通过绿 / 失败红(画在用例名后面)
        self.worker.case_started.connect(
            lambda fp, name: self.set_case_state(fp, "running"))
        self.worker.case_finished.connect(
            lambda fp, name, ok: self.set_case_state(fp, "passed" if ok else "failed"))
        self.worker.precondition_failed.connect(self.on_precondition_failed)
        # ★ 开始执行即在结果表里留痕(浅蓝开始行)—— 以前第一条步骤完成前
        #   表格一片空白,用户不知道跑没跑起来(用户反馈)
        self._append_result_row(
            "—", "▶ 开始执行",
            f"{len(case_files)} 个用例 × {repeat} 轮 · 设备 {device_id or '未选'}", "#e8f0fe")
        # ★ 点运行的瞬间全部勾选用例灯变黄(含上次跑过的,重新执行先重置)
        self._reset_case_lamps_to_running(case_files)
        self.worker.start()
        self._set_locked(True)  # 执行期间锁定编排区
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.tabs.setCurrentIndex(0)
        self.log_view.appendPlainText(
            f"[运行] {len(case_files)} 个文件 × {repeat} 轮 设备={device_id}")

    def on_stop(self):
        if self.worker:
            self._set_status("停止中(当前步骤结束后退出)...", self.status_label)
            self.worker.request_stop()

    def on_step_done(self, result):
        row = self.result_table.rowCount()
        self.result_table.insertRow(row)
        passed = result.get("passed")
        elapsed = result.get("elapsed")
        values = [str(row + 1), result.get("desc", ""),
                  "PASS" if passed else "FAIL",
                  f"{elapsed:.1f}s" if isinstance(elapsed, (int, float)) else "",
                  result.get("error", "")]
        for col, val in enumerate(values):
            item = QTableWidgetItem(val)
            if col == 2:
                item.setForeground(QColor("#16a34a") if passed else QColor("#dc2626"))
            item.setData(Qt.UserRole, result.get("screenshot", ""))
            if val:
                item.setToolTip(val)   # 长文本(步骤/错误信息)悬停看全文
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

    def on_worker_status(self, message):
        """阶段状态 → 状态栏;用例开始时在执行结果表插分隔行,让用户看到在跑"""
        self.status_label.setText(message)
        if message.startswith("执行用例"):
            self._append_result_row("—", "▶ " + message, "", "#f0f4fa")

    def _append_result_row(self, num, result, desc, bg):
        """向执行结果表追加一行彩色标记行(开始行/用例分隔行/总结行)。
        列序与表格一致:#、步骤(desc)、结果(result)、耗时、错误信息"""
        row = self.result_table.rowCount()
        self.result_table.insertRow(row)
        for col, text in enumerate((num, desc, result, "", "")):
            it = QTableWidgetItem(text)
            f = it.font()
            f.setBold(True)
            it.setFont(f)
            it.setBackground(QColor(bg))
            self.result_table.setItem(row, col, it)
        self.result_table.scrollToBottom()
        self.tabs.setCurrentIndex(0)

    def on_run_finished(self, passed, message):
        """执行结束。★ 解锁与 worker 清理必须无条件执行(try/finally)——
        显示逻辑一旦抛异常,界面会永久锁死、所有编辑入口被 worker 守卫拦住
        (用户实测:执行后无法编辑/展开步骤)"""
        try:
            self.status_label.setText(("✔ " if passed else "✘ ") + message)
            # ★ 最终结果写入执行结果表格(总结行,绿/红底粗体)——
            #   只显示在右上角状态栏太不明显(用户反馈)
            self._append_result_row(
                "—", "✔ 全部通过" if passed else "✘ 未通过", message,
                "#e6f7e9" if passed else "#fdeaea")
            self.log_view.appendPlainText(f"[结束] {message}")
        except Exception as e:
            self.log_view.appendPlainText(f"[内部] 结束处理异常: {e}")
        finally:
            self.run_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            self._set_locked(False)
            self.worker = None

    def closeEvent(self, event):
        if self.worker:
            if QMessageBox.question(self, "正在执行", "用例正在执行,停止并退出?") == QMessageBox.Yes:
                self.worker.request_stop()
                self.worker.wait(5000)
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()
