# -*- coding: utf-8 -*-
# 由 gui/main_window.py 拆分而来(结构整理 2026-10-15):
# 各类对话框: 图片查看、故障检测词、前置项编辑/管理、新建用例。
# ★ 拆分只为可读性 —— 这批类原先就在同一个模块里, 行为未改动。

# 只导入本模块用到的名字(结构整理 2026-10-15)
import os
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QAction, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QFormLayout,
                                QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu,
                                QMessageBox, QPlainTextEdit, QPushButton,
                                QScrollArea, QSpinBox, QTableWidget,
                                QTableWidgetItem, QVBoxLayout, QWidget)
from core.logger import get_logger
from gui.step_card import _StepsEditor
from gui.style import STYLESHEET
from gui.widgets import _FitCombo

log = get_logger()


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


class StatusWordsDialog(QDialog):
    """故障词/空闲词配置弹窗(用户 2026-10-13)。

    两个文本框, 词以逗号/顿号/分号/换行/空格分隔(与 core.session._split_texts 兼容)。
    保存路径: 主 config.yaml 的 runner.fault_texts / runner.idle_texts —— 在线编辑
    场景下用户期望改完即时生效, 不重启。ActionRunner 启动时会从 config 读一次, 这里
    保存后立即调 core.session.set_status_words 同步到全局, 当轮如果再触发哨兵
    立即用新词表。
    误触防护(2026-10-13): 两个数字框配连续命中次数 —— 1=瞬时即触发(开发用),
    3=默认(过瞬态 toast), 上限 10。改完同样即时生效。
    """

    def __init__(self, fault_texts, idle_texts,
                 fault_streak=3, idle_streak=3, parent=None):
        super().__init__(parent)
        self.setWindowTitle("故障与空闲词配置")
        lay = QVBoxLayout(self)
        hint = QLabel(
            "文本等待与断言的轮询里, 页面出现以下词时的处理方式:\n"
            "· 故障词: 立即报错, 报错与日志带**整段原文**(如「滚刷被困, 请清理」)\n"
            "· 空闲词: 立即判不成立, 不再傻等满超时\n"
            "多个词用逗号/顿号/分号/换行/空格分隔。\n"
            "★ 误触防护(2026-10-13): 连续命中**下方的次数**才触发 —— 半秒就消失的\n"
            "  toast / 状态栏瞬闪的「等待」字样不会再误判。1=瞬时即触发(开发用)。\n"
            "修改即时生效, 不重启。")
        hint.setStyleSheet("color:#475569;")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        lay.addWidget(QLabel("故障词(命中 → 立即报错):"))
        self.fault_edit = QPlainTextEdit(self)
        self.fault_edit.setPlainText(fault_texts or "")
        self.fault_edit.setPlaceholderText("故障,错误,卡住,被困,问题,检查,失败,异常")
        self.fault_edit.setFixedHeight(70)
        lay.addWidget(self.fault_edit)

        # ★ 连续命中次数(误触防护, 2026-10-13 用户加): 两个 QSpinBox 与词表一同保存
        row = QHBoxLayout()
        row.addWidget(QLabel("故障词连续次数:"))
        self.fault_streak = QSpinBox()
        self.fault_streak.setRange(1, 10)
        self.fault_streak.setValue(int(fault_streak))
        self.fault_streak.setToolTip("必须连续命中多少轮才触发 —— 防止瞬态 toast 误判")
        row.addWidget(self.fault_streak)
        row.addSpacing(20)
        row.addWidget(QLabel("空闲词连续次数:"))
        self.idle_streak = QSpinBox()
        self.idle_streak.setRange(1, 10)
        self.idle_streak.setValue(int(idle_streak))
        self.idle_streak.setToolTip("必须连续命中多少轮才触发")
        row.addWidget(self.idle_streak)
        row.addStretch()
        lay.addLayout(row)

        lay.addWidget(QLabel("空闲词(命中 → 立即判不成立):"))
        self.idle_edit = QPlainTextEdit(self)
        self.idle_edit.setPlainText(idle_texts or "")
        self.idle_edit.setPlaceholderText("待机,空闲,等待")
        self.idle_edit.setFixedHeight(50)
        lay.addWidget(self.idle_edit)

        btns = QHBoxLayout()
        btns.addStretch()
        defaults = QPushButton("恢复默认")
        defaults.clicked.connect(self._restore_defaults)
        btns.addWidget(defaults)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        ok = QPushButton("保存")
        ok.setObjectName("statusWordsSaveBtn")
        ok.setDefault(True)
        ok.clicked.connect(self._save)
        btns.addWidget(ok)
        lay.addLayout(btns)

    def _restore_defaults(self):
        from core.session import (DEFAULT_FAULT_WORDS, DEFAULT_IDLE_WORDS,
                                 DEFAULT_FAULT_STREAK, DEFAULT_IDLE_STREAK)
        self.fault_edit.setPlainText(",".join(DEFAULT_FAULT_WORDS))
        self.idle_edit.setPlainText(",".join(DEFAULT_IDLE_WORDS))
        self.fault_streak.setValue(DEFAULT_FAULT_STREAK)
        self.idle_streak.setValue(DEFAULT_IDLE_STREAK)

    def _save(self):
        from core import driver
        fault = self.fault_edit.toPlainText().strip()
        idle = self.idle_edit.toPlainText().strip()
        fault_n = int(self.fault_streak.value())
        idle_n = int(self.idle_streak.value())
        # ★ 写盘 + 同步到内存(2026-10-13): 缺一不可 —— 写盘是永久化, 同步
        #   是让当轮后续步骤立即用上新词表(否则要等下一个 ActionRunner 启动)
        try:
            driver.update_config({"runner.fault_texts": fault,
                                  "runner.idle_texts": idle,
                                  "runner.fault_streak": fault_n,
                                  "runner.idle_streak": idle_n})
        except Exception as e:
            QMessageBox.warning(self, "保存失败", f"写配置失败: {e}")
            return
        try:
            from core import session
            session.set_status_words(fault, idle,
                                     fault_streak=fault_n, idle_streak=idle_n)
        except Exception:
            pass
        self.fault_texts, self.idle_texts = fault, idle
        self.fault_streak_value, self.idle_streak_value = fault_n, idle_n
        self.accept()


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
        from core.session import item_label
        self._app_group = app_group
        self.setWindowTitle("前置条件设置")
        self.resize(560, 380)
        # ★ 对话框不会自动继承主窗口样式表 → 字体/字号不一致, 勾选框发虚
        self.setStyleSheet(STYLESHEET)
        self._label = item_label
        self.items = [dict(x) for x in items]
        v = QVBoxLayout(self)
        self._hint_label = QLabel(
            "按顺序依次执行(用 ↑↓ 调整顺序); 取消勾选则不执行; 参数用「编辑」调整")
        v.addWidget(self._hint_label)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["启用 / 前置条件", "顺序", "编辑", "删除"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setColumnWidth(1, 64)
        self.table.setColumnWidth(2, 60)
        self.table.setColumnWidth(3, 60)
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
            # ★ 顺序调整(用户要求可手动调整): 左右并排的 ↑↓, 列表顺序=执行顺序
            order = QWidget()
            oh = QHBoxLayout(order)
            oh.setContentsMargins(0, 0, 0, 0)
            oh.setSpacing(2)
            for text, delta, tip in (("↑", -1, "上移(更早执行)"), ("↓", 1, "下移(更晚执行)")):
                b = QPushButton(text)
                b.setAutoDefault(False)      # QToolButton 没有这个方法(踩过)
                b.setToolTip(tip)
                # ★ 全局 QSS 给 QPushButton 设了 padding: 4px 12px —— 箭头按钮只有
                #   26px 宽, 左右内边距就吃掉 24px, 字被整个截掉(用户实测"看不到箭头")。
                #   小按钮必须内联覆盖 padding, 并固定尺寸放得下箭头。
                b.setFixedSize(26, 22)
                b.setStyleSheet("QPushButton { padding: 0px; font-weight: bold; }")
                b.setEnabled(0 <= i + delta < len(self.items))   # 首/末行置灰
                b.clicked.connect(lambda _=False, k=i, d=delta: self._move(k, d))
                oh.addWidget(b)
            self.table.setCellWidget(i, 1, order)
            eb = QPushButton("编辑")
            eb.setAutoDefault(False)      # ★ 否则回车/焦点变化会误触发
            eb.clicked.connect(lambda _=False, k=i: self._edit(k))
            self.table.setCellWidget(i, 2, eb)
            db = QPushButton("删除")
            db.setAutoDefault(False)
            db.clicked.connect(lambda _=False, k=i: self._del(k))
            self.table.setCellWidget(i, 3, db)
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

    def _move(self, idx, delta):
        """上移/下移一条前置条件 —— 列表顺序就是执行顺序(用户要求可手动调整)"""
        new = idx + delta
        if not (0 <= new < len(self.items)):
            return
        self.items[idx], self.items[new] = self.items[new], self.items[idx]
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
