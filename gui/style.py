# -*- coding: utf-8 -*-
# 由 gui/main_window.py 拆分而来(结构整理 2026-10-15):
# 常量与样式: 状态灯 / 配色 / 全局 QSS(内嵌图标占位符的替换)。
# ★ 拆分只为可读性 —— 这批类原先就在同一个模块里, 行为未改动。

# 只导入本模块用到的名字(结构整理 2026-10-15)
import os
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from core.driver import BASE_DIR
from core.logger import get_logger

log = get_logger()


CASE_STATE_COLORS = {"idle": "#cbd5e1", "running": "#f5b301",
                     "passed": "#22c55e", "failed": "#ef4444"}


CASE_STATE_ROLE = int(Qt.UserRole) + 1     # item 存状态字符串


_LAMP_SIZE = 12                            # 状态灯直径


_ARROW_W = 44                              # 右侧箭头命中区宽(↑22 + ↓22)


_FIELD_LABEL_W = 96                        # 步骤字段标签列固定宽(对齐整齐)


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


CATEGORY_COLORS = {
    "操作": "#2563eb", "断言": "#ea580c", "数据": "#7c3aed", "开关": "#0d9488",
    "时间": "#0891b2", "地图编辑": "#16a34a", "定时": "#b45309", "流程控制": "#64748b",
}


QUICK_ACTIONS = ["click", "assert", "switch_to", "set_time", "wait_for", "sleep"]


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
/* ★ if 子步骤卡片上的分支徽标(用户要求 2026-10-12): 展开后 then/else 的子卡片连排,
   卡片头部原先看不出属于哪一支, 只能靠"往上找分组标题 + 顺序"推断。 */
QLabel#branchBadge { color: #1d4ed8; background: #e0ebff; border-radius: 3px;
                     padding: 1px 5px; font-size: 11px; }
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


_ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
for _name, _file in [("CHEV_UP", "spin_chevron_up.png"),
                     ("CHEV_UP_HOVER", "spin_chevron_up_hover.png"),
                     ("CHEV_DOWN", "spin_chevron_down.png"),
                     ("CHEV_DOWN_HOVER", "spin_chevron_down_hover.png")]:
    STYLESHEET = STYLESHEET.replace(f"@{_name}@",
                                    os.path.join(_ASSETS, _file).replace("\\", "/"))
