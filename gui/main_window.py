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
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QIntValidator, QDoubleValidator, QColor, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
    QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
    QSplitter, QTabWidget, QTableWidget, QTableWidgetItem, QToolButton,
    QVBoxLayout, QWidget,
)

from common.driver import BASE_DIR, load_config
from gui import schema
from gui.runner_thread import RunWorker

CASES_DIR = os.path.join(BASE_DIR, "Test_cases")

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
    padding: 5px 14px; color: #333;
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
    background: transparent; color: #64748b; padding: 7px 18px;
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
    w = QLineEdit("" if value is None else str(value))
    w.setPlaceholderText(hint)
    return w, lambda: w.text()


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
    """单个步骤卡片: 折叠=摘要行,展开=参数表单(高级参数另收一层)"""

    def __init__(self, main, index):
        super().__init__()
        self.main = main
        self.index = index
        self.step = main.steps[index]
        self.getters = {}
        self.expanded = (main.expanded_idx == index)
        self.setObjectName("stepCardOpen" if self.expanded else "stepCard")
        self.setCursor(Qt.PointingHandCursor if not self.expanded else Qt.ArrowCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 7, 10, 9)
        lay.setSpacing(6)
        self._build_header(lay)
        if self.expanded:
            self._build_form(lay)

    # ── 折叠态: 编号 + 组件标签 + 摘要 + 徽标 + 操作按钮 ──
    def _build_header(self, lay):
        head = QHBoxLayout()
        head.setSpacing(8)
        num = QLabel(str(self.index + 1))
        num.setObjectName("numLabel")
        num.setFixedWidth(18)
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

        for text, tip, fn in [("▲", "上移", lambda: self.main.move_step(self.index, -1)),
                              ("▼", "下移", lambda: self.main.move_step(self.index, 1)),
                              ("⧉", "复制", lambda: self.main.dup_step(self.index)),
                              ("✕", "删除", lambda: self.main.del_step(self.index))]:
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            b.setFixedSize(22, 20)
            if text == "✕":
                b.setStyleSheet("QToolButton:hover { color:#dc2626; background:#fef2f2; }")
            b.clicked.connect(fn)
            head.addWidget(b)
        lay.addLayout(head)

    def mousePressEvent(self, event):
        """点击折叠卡片 → 原位展开(展开态不响应,避免编辑时误收起)"""
        if not self.expanded and event.button() == Qt.LeftButton:
            self.main.expand_card(self.index)
        super().mousePressEvent(event)

    def _refresh_summary(self):
        self.summary_label.setText(schema.step_summary(self.step))
        badges = []
        else_items = self.step.get("else")
        if isinstance(else_items, list) and else_items:
            badges.append(f"▸else {len(else_items)}步")
        if self.step.get("screenshot"):
            badges.append("📷")
        if self.step.get("timeout"):
            badges.append(f"⏱{self.step['timeout']}s")
        if self.step.get("wait"):
            badges.append(f"+{self.step['wait']}s")
        if self.step.get("retry"):
            badges.append(f"↻{self.step['retry']}")
        # 徽标并入摘要尾部,避免再建控件
        if badges:
            self.summary_label.setText(self.summary_label.text() + "   " + " ".join(badges))

    # ── 展开态: 参数表单 ──
    def _build_form(self, lay):
        action = self.main._find_action(self.step)
        form = QWidget()
        fv = QVBoxLayout(form)
        fv.setContentsMargins(26, 0, 4, 0)
        fv.setSpacing(5)

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

        lay.addWidget(form)

    def _add_field(self, grid, row, field):
        lbl = QLabel(field["label"])
        lbl.setObjectName("fieldLabel")
        grid.addWidget(lbl, row, 0, Qt.AlignTop)
        w, getter = _make_field_widget(field, self.step.get(field["key"]))
        w.setMinimumWidth(240)
        w.setMaximumWidth(430)
        grid.addWidget(w, row, 1)
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
        self.expanded_idx = -1

        root = QWidget()
        v = QVBoxLayout(root)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(8)
        v.addWidget(self._build_toolbar())
        v.addWidget(self._build_chip_strip())
        v.addWidget(self._make_cards_area(), 3)
        v.addWidget(self._make_bottom(), 2)
        self.setCentralWidget(root)
        self.setStyleSheet(STYLESHEET)

        self._load_case_into_ui()
        self.render_cards()

    # ── 顶部工具栏 ──
    def _build_toolbar(self):
        bar = QWidget()
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        for text, fn in [("新建", self.on_new), ("打开", self.on_open), ("保存", self.on_save)]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            lay.addWidget(b)
        self.file_label = QLabel("未打开文件")
        self.file_label.setStyleSheet("color:#94a3b8;")
        lay.addWidget(self.file_label)
        lay.addSpacing(8)

        self.add_btn = QPushButton("＋ 添加步骤")
        self.add_btn.setStyleSheet("color:#2563eb; border-color:#b9cff5;")
        menu = QMenu(self.add_btn)
        for cat in schema.CATEGORY_ORDER:
            menu.addSection(cat)
            for a in schema.ACTIONS:
                if a["category"] == cat:
                    act = QAction(a["label"], menu)
                    act.triggered.connect(lambda _, k=a["key"]: self.add_step(k))
                    menu.addAction(act)
        menu.addSection("其他")
        act = QAction("延时等待", menu)
        act.triggered.connect(lambda: self.add_step("__wait"))
        menu.addAction(act)
        self.add_btn.setMenu(menu)
        lay.addWidget(self.add_btn)

        lay.addStretch()
        lay.addWidget(QLabel("设备:"))
        self.device_combo = QComboBox()
        self._fill_devices()
        lay.addWidget(self.device_combo)

        pre_btn = QPushButton("前置条件 ▾")
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

    def _build_chip_strip(self):
        """用例信息行 + 常用组件快捷条"""
        strip = QFrame()
        strip.setObjectName("chipStrip")
        lay = QHBoxLayout(strip)
        lay.setContentsMargins(10, 7, 10, 7)
        lay.setSpacing(6)

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
        tip = QLabel("更多动作见右上「＋添加步骤」;点卡片展开编辑参数")
        tip.setStyleSheet("color:#b6c0cd;")
        lay.addWidget(tip)
        lay.addStretch()
        self.step_count_label = QLabel("")
        self.step_count_label.setStyleSheet("color:#2563eb; font-weight:bold;")
        lay.addWidget(self.step_count_label)
        return strip

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
            self.cards_lay.insertWidget(self.cards_lay.count() - 1, StepCard(self, i))
        self.step_count_label.setText(f"共 {len(self.steps)} 步")
        self._refresh_yaml_text()

    def expand_card(self, index):
        self.expanded_idx = index
        self.render_cards()

    def on_card_edited(self):
        self._refresh_yaml_text()

    # ── 步骤操作 ──
    def add_step(self, key):
        if key == "__wait":
            step = {"desc": "延时等待", "wait": 10}
        else:
            step = schema.new_step(key)
        self.steps.append(step)
        self.expanded_idx = len(self.steps) - 1
        self.render_cards()
        self._scroll_to_end()

    def _scroll_to_end(self):
        bar = self.cards_scroll.verticalScrollBar()
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, lambda: bar.setValue(bar.maximum()))

    def move_step(self, index, delta):
        new = index + delta
        if not (0 <= new < len(self.steps)):
            return
        self.steps[index], self.steps[new] = self.steps[new], self.steps[index]
        if self.expanded_idx == index:
            self.expanded_idx = new
        self.render_cards()

    def dup_step(self, index):
        self.steps.insert(index + 1, copy.deepcopy(self.steps[index]))
        self.expanded_idx = index + 1
        self.render_cards()

    def del_step(self, index):
        del self.steps[index]
        if self.expanded_idx >= len(self.steps):
            self.expanded_idx = len(self.steps) - 1
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
        self._refresh_yaml_text()

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
        self.result_table = QTableWidget(0, 4)
        self.result_table.setHorizontalHeaderLabels(["#", "结果", "步骤", "错误信息"])
        self.result_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.result_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.result_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.result_table.verticalHeader().setVisible(False)
        self.result_table.cellDoubleClicked.connect(self.on_result_row)
        result_split.addWidget(self.result_table)
        self.preview = QLabel("双击结果行查看截图")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumWidth(240)
        result_split.addWidget(self.preview)
        result_split.setSizes([620, 300])
        self.tabs.addTab(result_split, "执行结果")

        # YAML 源码
        src = QWidget()
        sv = QVBoxLayout(src)
        self.yaml_edit = QPlainTextEdit()
        sv.addWidget(self.yaml_edit)
        srow = QHBoxLayout()
        for text, fn in [("← 从卡片刷新", self._refresh_yaml_text),
                         ("应用到卡片 →", self._apply_yaml_text)]:
            b = QPushButton(text)
            b.clicked.connect(fn)
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
        self.case_path = None
        self.data = self._empty_data()
        self.case_idx = 0
        self.expanded_idx = -1
        self.file_label.setText("未打开文件")
        self._load_case_into_ui()
        self.render_cards()

    def on_open(self):
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
        self.expanded_idx = -1
        self.file_label.setText(os.path.relpath(path, BASE_DIR))
        self._load_case_into_ui()
        self.render_cards()

    def _dump_data(self):
        return {"module": self.data.get("module", "未命名"),
                "cases": [dict(c, steps=[schema.serialize_step(s) for s in c.get("steps", [])])
                          for c in self.data.get("cases", [])]}

    def on_save(self):
        if not self.case_path:
            os.makedirs(CASES_DIR, exist_ok=True)
            path, _ = QFileDialog.getSaveFileName(
                self, "保存用例", os.path.join(CASES_DIR, "新用例.yaml"), "YAML 用例 (*.yaml)")
            if not path:
                return
            self.case_path = path
        self._sync_header()
        problems = []
        for i, s in enumerate(self.steps):
            for err in schema.validate_step(s):
                problems.append(f"步骤{i + 1}: {err}")
        if problems and QMessageBox.question(
                self, "存在参数问题,仍要保存?", "\n".join(problems[:10])) != QMessageBox.Yes:
            return
        with open(self.case_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(self._dump_data(), f, allow_unicode=True, sort_keys=False)
        self.file_label.setText(os.path.relpath(self.case_path, BASE_DIR))
        self.status_label.setText(f"已保存: {os.path.basename(self.case_path)}")
        self.log_view.appendPlainText(f"[保存] {self.case_path}")

    # ── YAML 源码同步 ──
    def _refresh_yaml_text(self):
        if not hasattr(self, "yaml_edit"):
            return
        self.yaml_edit.setPlainText(
            yaml.safe_dump(self._dump_data(), allow_unicode=True, sort_keys=False))

    def _apply_yaml_text(self):
        try:
            data = yaml.safe_load(self.yaml_edit.toPlainText()) or {}
            if not isinstance(data, dict):
                raise ValueError("顶层必须是键值结构")
        except Exception as e:
            QMessageBox.critical(self, "解析失败", f"YAML 有误:\n{e}")
            return
        self.data = data
        self.case_idx = 0
        self.expanded_idx = -1
        self._load_case_into_ui()
        self.render_cards()

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
        self.preview.setText("双击结果行查看截图")

        self.worker = RunWorker(device_id, self.case_path, pre)
        self.worker.step_done.connect(self.on_step_done)
        self.worker.log_line.connect(self.log_view.appendPlainText)
        self.worker.status.connect(lambda s: self.status_label.setText(s))
        self.worker.finished_run.connect(self.on_run_finished)
        self.worker.start()
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
        values = [str(row + 1), "PASS" if passed else "FAIL",
                  result.get("desc", ""), result.get("error", "")]
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

    def on_result_row(self, row, _col):
        shot = self.result_table.item(row, 0).data(Qt.UserRole)
        if shot and os.path.exists(shot):
            self._show_screenshot(shot)
        else:
            self.preview.setText("该步骤无截图")

    def _show_screenshot(self, path):
        pix = QPixmap(path)
        if not pix.isNull():
            self.preview.setPixmap(
                pix.scaled(self.preview.width(), self.preview.height(),
                           Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def on_run_finished(self, passed, message):
        self.status_label.setText(("✔ " if passed else "✘ ") + message)
        self.log_view.appendPlainText(f"[结束] {message}")
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.worker = None

    def _fill_devices(self):
        try:
            cfg = load_config()
        except Exception:
            cfg = {"device": {"list": []}}
        seen = set()
        for dev in cfg.get("device", {}).get("list", []):
            self.device_combo.addItem(f"{dev.get('name', dev['id'])} ({dev['id']})", dev["id"])
            seen.add(dev["id"])
        default = cfg.get("device", {}).get("default")
        if default and default != "auto" and default not in seen:
            self.device_combo.addItem(f"默认 ({default})", default)

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
