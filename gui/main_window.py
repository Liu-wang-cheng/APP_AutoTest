"""用例编排器主窗口

布局: 顶部工具栏(文件/设备/前置/运行) + 左组件面板 + 中步骤列表 + 右参数表单
      底部标签页: 执行结果 / YAML 源码 / 运行日志
数据模型: 全程以 dict 列表持有步骤,YAML 文件是唯一持久化格式
"""
import copy
import json
import os

import yaml
from PySide6.QtCore import Qt
from PySide6.QtGui import QIntValidator, QDoubleValidator, QColor, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QGridLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
    QScrollArea, QSplitter, QTabWidget, QTableWidget, QTableWidgetItem,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from common.driver import BASE_DIR, load_config
from gui import schema
from gui.runner_thread import RunWorker

CASES_DIR = os.path.join(BASE_DIR, "Test_cases")

EMPTY_CASE = {"name": "新用例", "priority": "P1", "steps": []}


class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("扫地机用例编排器 - vacuum_app_test")
        self.case_path = None           # 当前打开的 YAML 文件
        self.data = self._empty_data()  # {"module":..., "cases":[...]}
        self.case_idx = 0
        self.worker = None
        self._form_widgets = {}         # 参数表单取值器

        root = QWidget()
        v = QVBoxLayout(root)
        v.setContentsMargins(0, 0, 0, 0)
        v.addWidget(self._build_toolbar())
        v.addWidget(self._make_body(), 3)
        v.addWidget(self._make_bottom(), 2)
        self.setCentralWidget(root)

        self._load_case_into_ui()
        self._render_steps()

    # ── 顶部工具栏 ──
    def _build_toolbar(self):
        bar = QWidget()
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(8, 6, 8, 6)

        for text, fn in [("新建", self.on_new), ("打开", self.on_open), ("保存", self.on_save)]:
            btn = QPushButton(text)
            btn.clicked.connect(fn)
            lay.addWidget(btn)
        self.file_label = QLabel("未打开文件(保存时选择位置)")
        self.file_label.setStyleSheet("color: #666;")
        lay.addWidget(self.file_label)
        lay.addSpacing(16)

        lay.addWidget(QLabel("设备:"))
        self.device_combo = QComboBox()
        self._fill_devices()
        lay.addWidget(self.device_combo)

        lay.addSpacing(8)
        lay.addWidget(QLabel("前置:"))
        self.pre_checks = {}
        for key, label in [("restart", "重启APP"), ("charging", "等充电"),
                           ("map_load", "等地图"), ("battery", "等电量")]:
            cb = QCheckBox(label)
            cb.setChecked(True)
            self.pre_checks[key] = cb
            lay.addWidget(cb)

        lay.addStretch()
        self.run_btn = QPushButton("▶ 运行")
        self.run_btn.setStyleSheet("color: green; font-weight: bold;")
        self.run_btn.clicked.connect(self.on_run)
        lay.addWidget(self.run_btn)
        self.stop_btn = QPushButton("■ 停止")
        self.stop_btn.setStyleSheet("color: #c00; font-weight: bold;")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.on_stop)
        lay.addWidget(self.stop_btn)

        self.status_label = QLabel("就绪")
        lay.addWidget(self.status_label)
        return bar

    # ── 主体三栏 ──
    def _make_body(self):
        splitter = QSplitter(Qt.Horizontal)

        # 左: 动作组件面板(按分类分组,双击添加)
        palette_box = QGroupBox("动作组件(双击添加)")
        pv = QVBoxLayout(palette_box)
        tree = QTreeWidget()
        tree.setHeaderLabel("")
        root_item = tree.invisibleRootItem()
        for cat in schema.CATEGORY_ORDER:
            cat_item = QTreeWidgetItem([cat])
            root_item.addChild(cat_item)
            for a in schema.ACTIONS:
                if a["category"] == cat:
                    child = QTreeWidgetItem([a["label"]])
                    child.setData(0, Qt.UserRole, a["key"])
                    cat_item.addChild(child)
        wait_item = QTreeWidgetItem(["延时等待"])
        wait_item.setData(0, Qt.UserRole, "__wait")
        root_item.addChild(wait_item)
        tree.itemDoubleClicked.connect(self.on_add_step)
        pv.addWidget(tree)
        splitter.addWidget(palette_box)

        # 中: 用例信息 + 步骤列表
        mid = QWidget()
        mv = QVBoxLayout(mid)
        head = QGroupBox("用例信息")
        hg = QGridLayout(head)
        self.module_edit = QLineEdit()
        self.case_name_edit = QLineEdit()
        self.priority_edit = QLineEdit()
        self.case_wait_edit = QLineEdit()
        self.case_wait_edit.setValidator(QIntValidator(0, 99999))
        for row, (label, w) in enumerate([("用例组(module)", self.module_edit),
                                          ("用例名称", self.case_name_edit),
                                          ("优先级", self.priority_edit),
                                          ("用例级间隔(秒)", self.case_wait_edit)]):
            hg.addWidget(QLabel(label), row, 0)
            hg.addWidget(w, row, 1)
        self.module_edit.editingFinished.connect(self._sync_header)
        self.case_name_edit.editingFinished.connect(self._sync_header)
        self.priority_edit.editingFinished.connect(self._sync_header)
        self.case_wait_edit.editingFinished.connect(self._sync_header)
        mv.addWidget(head)

        steps_box = QGroupBox("步骤(可拖拽排序)")
        sv = QVBoxLayout(steps_box)
        self.steps_list = QListWidget()
        self.steps_list.setDragDropMode(QAbstractItemView.InternalMove)
        self.steps_list.currentRowChanged.connect(self.on_step_selected)
        self.steps_list.model().rowsMoved.connect(self.on_steps_reordered)
        sv.addWidget(self.steps_list)
        btn_row = QHBoxLayout()
        for text, fn in [("上移", lambda: self._move_step(-1)), ("下移", lambda: self._move_step(1)),
                         ("复制", self.on_dup_step), ("删除", self.on_del_step),
                         ("清空", self.on_clear_steps)]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            btn_row.addWidget(b)
        sv.addLayout(btn_row)
        mv.addWidget(steps_box, 1)
        splitter.addWidget(mid)

        # 右: 参数表单
        param_box = QGroupBox("步骤参数")
        pv2 = QVBoxLayout(param_box)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.param_host = QWidget()
        self.param_lay = QVBoxLayout(self.param_host)
        scroll.setWidget(self.param_host)
        pv2.addWidget(scroll)
        self.form_tip = QLabel("双击左侧组件添加步骤;选中步骤后在此编辑参数")
        self.form_tip.setWordWrap(True)
        self.form_tip.setStyleSheet("color: #888;")
        pv2.addWidget(self.form_tip)
        splitter.addWidget(param_box)
        splitter.setSizes([200, 420, 380])
        return splitter

    # ── 底部标签页 ──
    def _make_bottom(self):
        tabs = QTabWidget()
        self.tabs = tabs

        # 执行结果
        result_split = QSplitter(Qt.Horizontal)
        self.result_table = QTableWidget(0, 4)
        self.result_table.setHorizontalHeaderLabels(["#", "结果", "步骤", "错误信息"])
        self.result_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.result_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.result_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.result_table.cellDoubleClicked.connect(self.on_result_row)
        result_split.addWidget(self.result_table)
        self.preview = QLabel("双击结果行查看截图")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumWidth(260)
        result_split.addWidget(self.preview)
        result_split.setSizes([560, 320])
        tabs.addTab(result_split, "执行结果")

        # YAML 源码(兜底编辑 else 分支等复杂结构)
        src = QWidget()
        sv = QVBoxLayout(src)
        self.yaml_edit = QPlainTextEdit()
        sv.addWidget(self.yaml_edit)
        srow = QHBoxLayout()
        btn_refresh = QPushButton("← 从编辑器刷新")
        btn_refresh.clicked.connect(self._refresh_yaml_text)
        btn_apply = QPushButton("应用到编辑器 →")
        btn_apply.clicked.connect(self._apply_yaml_text)
        srow.addWidget(btn_refresh)
        srow.addWidget(btn_apply)
        srow.addStretch()
        sv.addLayout(srow)
        tabs.addTab(src, "YAML 源码")

        # 运行日志
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        tabs.addTab(self.log_view, "运行日志")
        return tabs

    # ── 数据模型 ──
    def _empty_data(self):
        return {"module": "新用例组", "cases": [dict(EMPTY_CASE, steps=[])]}

    @property
    def current_case(self):
        cases = self.data.setdefault("cases", [])
        if not cases:
            cases.append(dict(EMPTY_CASE, steps=[]))
        self.case_idx = min(self.case_idx, len(cases) - 1)
        return cases[self.case_idx]

    @property
    def steps(self):
        return self.current_case.setdefault("steps", [])

    def _load_case_into_ui(self):
        c = self.current_case
        self.module_edit.setText(self.data.get("module", ""))
        self.case_name_edit.setText(c.get("name", ""))
        self.priority_edit.setText(str(c.get("priority", "P1")))
        self.case_wait_edit.setText("" if c.get("wait") is None else str(c["wait"]))

    def _sync_header(self):
        self.data["module"] = self.module_edit.text().strip() or "未命名"
        c = self.current_case
        c["name"] = self.case_name_edit.text().strip() or "未命名用例"
        c["priority"] = self.priority_edit.text().strip() or "P1"
        txt = self.case_wait_edit.text().strip()
        c["wait"] = int(txt) if txt.isdigit() else None
        self._refresh_yaml_text()

    def _render_steps(self):
        self.steps_list.blockSignals(True)
        self.steps_list.clear()
        for i, step in enumerate(self.steps):
            item = QListWidgetItem(f"{i + 1}. {schema.step_summary(step)}")
            item.setData(Qt.UserRole, json.dumps(step, ensure_ascii=False))
            self.steps_list.addItem(item)
        self.steps_list.blockSignals(False)
        self._refresh_yaml_text()

    def _renumber(self):
        for i in range(self.steps_list.count()):
            item = self.steps_list.item(i)
            step = json.loads(item.data(Qt.UserRole))
            item.setText(f"{i + 1}. {schema.step_summary(step)}")

    # ── 步骤操作 ──
    def on_add_step(self, item, _col=0):
        key = item.data(0, Qt.UserRole)
        if key == "__wait":
            step = {"desc": "延时等待", "wait": 10}
        else:
            step = schema.new_step(key)
        self.steps.append(step)
        witem = QListWidgetItem(f"{len(self.steps)}. {schema.step_summary(step)}")
        witem.setData(Qt.UserRole, json.dumps(step, ensure_ascii=False))
        self.steps_list.addItem(witem)
        self.steps_list.setCurrentRow(self.steps_list.count() - 1)
        self._refresh_yaml_text()

    def on_step_selected(self, row):
        self._build_form(row)

    def on_steps_reordered(self, *args):
        """拖拽排序后: 以列表项顺序为准同步 steps"""
        order = [json.loads(self.steps_list.item(i).data(Qt.UserRole))
                 for i in range(self.steps_list.count())]
        self.steps[:] = order
        self._renumber()
        self._refresh_yaml_text()

    def _move_step(self, delta):
        row = self.steps_list.currentRow()
        if row < 0:
            return
        new = row + delta
        if not (0 <= new < len(self.steps)):
            return
        self.steps[row], self.steps[new] = self.steps[new], self.steps[row]
        self._render_steps()
        self.steps_list.setCurrentRow(new)
        self._build_form(new)

    def on_dup_step(self):
        row = self.steps_list.currentRow()
        if row < 0:
            return
        self.steps.insert(row + 1, copy.deepcopy(self.steps[row]))
        self._render_steps()
        self.steps_list.setCurrentRow(row + 1)
        self._build_form(row + 1)

    def on_del_step(self):
        row = self.steps_list.currentRow()
        if row < 0:
            return
        del self.steps[row]
        self._render_steps()
        self._build_form(self.steps_list.currentRow())

    def on_clear_steps(self):
        if self.steps and QMessageBox.question(
                self, "确认", "清空当前用例全部步骤?") == QMessageBox.Yes:
            self.steps.clear()
            self._render_steps()
            self._build_form(-1)

    # ── 参数表单 ──
    def _find_action(self, step):
        for key in step:
            if key in schema.ACTION_BY_KEY:
                return schema.ACTION_BY_KEY[key]
        return None

    def _clear_form(self):
        while self.param_lay.count():
            item = self.param_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._form_widgets = {}

    def _build_form(self, row):
        self._clear_form()
        if row < 0 or row >= len(self.steps):
            self.form_tip.setText("双击左侧组件添加步骤;选中步骤后在此编辑参数")
            return
        step = self.steps[row]
        action = self._find_action(step)

        if action:
            box = QGroupBox(f"动作: {action['label']}")
            grid = QGridLayout(box)
            for r, f in enumerate(action["fields"]):
                if f["type"] == "group":
                    self._add_group_field(grid, r, step, f)
                else:
                    self._add_field(grid, r, step, f)
            if not action["fields"]:
                grid.addWidget(QLabel("该动作无参数"), 0, 0)
            self.param_lay.addWidget(box)
            if action.get("tip"):
                self.form_tip.setText("提示: " + action["tip"])
            else:
                self.form_tip.setText("编辑后自动写回步骤;串号约定见各输入框灰字提示")

        gen = QGroupBox("通用参数")
        grid = QGridLayout(gen)
        for r, f in enumerate(schema.GENERIC_FIELDS):
            self._add_field(grid, r, step, f)
        self.param_lay.addWidget(gen)
        self.param_lay.addStretch()

    def _make_widget(self, f, value):
        """按字段类型建控件,返回 (widget, 取值getter)"""
        t = f["type"]
        if t == "bool":
            w = QCheckBox()
            w.setChecked(bool(value))
            return w, w.isChecked
        if t == "int":
            w = QLineEdit("" if value is None else str(value))
            w.setValidator(QIntValidator(-100000, 100000))
            w.setPlaceholderText(f.get("hint", ""))
            return w, self._int_getter(w)
        if t == "float":
            w = QLineEdit("" if value is None else str(value))
            w.setValidator(QDoubleValidator(0, 100000, 4))
            w.setPlaceholderText(f.get("hint", ""))
            return w, self._float_getter(w)
        if t == "int4":
            w = QLineEdit(",".join(map(str, value)) if value else "")
            w.setPlaceholderText(f.get("hint", "x1,y1,x2,y2"))
            return w, self._int4_getter(w)
        w = QLineEdit("" if value is None else str(value))
        w.setPlaceholderText(f.get("hint", ""))
        return w, lambda: w.text()

    @staticmethod
    def _int_getter(w):
        def get():
            txt = w.text().strip()
            return int(txt) if txt.lstrip("-").isdigit() else None
        return get

    @staticmethod
    def _float_getter(w):
        def get():
            try:
                return float(w.text().strip())
            except ValueError:
                return None
        return get

    @staticmethod
    def _int4_getter(w):
        def get():
            parts = [p.strip() for p in w.text().split(",") if p.strip()]
            if not parts:
                return []
            if len(parts) != 4 or not all(p.lstrip("-").isdigit() for p in parts):
                return None  # 输入未完成,保持原值
            return [int(p) for p in parts]
        return get

    def _add_field(self, grid, row, step, f):
        grid.addWidget(QLabel(f["label"]), row, 0)
        w, getter = self._make_widget(f, step.get(f["key"]))
        w.setMinimumWidth(180)
        grid.addWidget(w, row, 1)
        self._form_widgets[f["key"]] = getter
        signal = w.toggled if isinstance(w, QCheckBox) else w.editingFinished
        signal.connect(self._form_to_step)

    def _add_group_field(self, grid, row, step, f):
        """组合字段(add_timer 等): 嵌套子表单,子值全空时序列化为标量 True"""
        box = QGroupBox(f["label"])
        sub_grid = QGridLayout(box)
        cur = step.get(f["key"])
        cur = cur if isinstance(cur, dict) else {}
        getters = {}
        for r, sub in enumerate(f["fields"]):
            sub_grid.addWidget(QLabel(sub["label"]), r, 0)
            w, getter = self._make_widget(sub, cur.get(sub["key"]))
            w.setMinimumWidth(160)
            sub_grid.addWidget(w, r, 1)
            getters[sub["key"]] = getter
            signal = w.toggled if isinstance(w, QCheckBox) else w.editingFinished
            signal.connect(self._form_to_step)
        grid.addWidget(box, row, 1)
        self._form_widgets[f["key"]] = ("__group__", getters)

    def _form_to_step(self, *args):
        """表单任一字段编辑结束 → 写回当前步骤并刷新卡片文字"""
        row = self.steps_list.currentRow()
        if row < 0 or row >= len(self.steps):
            return
        step = self.steps[row]
        for key, getter in self._form_widgets.items():
            if isinstance(getter, tuple) and getter[0] == "__group__":
                vals = {k: g() for k, g in getter[1].items()}
                vals = {k: v for k, v in vals.items() if v not in (None, "")}
                step[key] = vals if vals else True
            else:
                v = getter()
                if v is not None:
                    step[key] = v
        item = self.steps_list.item(row)
        if item:
            item.setData(Qt.UserRole, json.dumps(step, ensure_ascii=False))
            self._renumber()
        self._refresh_yaml_text()

    # ── 文件操作 ──
    def on_new(self):
        self.case_path = None
        self.data = self._empty_data()
        self.case_idx = 0
        self.file_label.setText("未打开文件(保存时选择位置)")
        self._load_case_into_ui()
        self._render_steps()
        self._build_form(-1)

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
        self.file_label.setText(os.path.relpath(path, BASE_DIR))
        self._load_case_into_ui()
        self._render_steps()
        self._build_form(-1)

    def _dump_data(self):
        """编辑器数据 → 可序列化结构(步骤过 serialize_step 清理)"""
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
            for err in schema.validate_step(s):  # 校验原始步骤(空必填项要在序列化丢弃前发现)
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
        self._load_case_into_ui()
        self._render_steps()
        self._build_form(-1)

    # ── 执行 ──
    def on_run(self):
        if self.worker:
            return
        self.on_save()  # 先落盘,执行端读文件
        if not self.case_path:
            return
        device_id = self.device_combo.currentData()
        pre = {k: cb.isChecked() for k, cb in self.pre_checks.items()}
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
                item.setForeground(QColor("green") if passed else QColor("red"))
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

    # ── 其他 ──
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
