# -*- coding: utf-8 -*-
"""一次性迁移: 前置条件 GUI(编辑/新增对话框 + 动态菜单)。用后删除。"""
p = "gui/main_window.py"
s = open(p, encoding="utf-8").read()

# ── 1) 两个对话框类(插在 NewCaseDialog 前) ──
anchor = "class NewCaseDialog(QDialog):"
assert anchor in s, "anchor"
cls = '''class PreconditionEditDialog(QDialog):
    """新增/编辑单个前置条件: 选类型 + 填参数(按类型动态生成表单)"""

    def __init__(self, item=None, parent=None):
        super().__init__(parent)
        from core.session import PRECONDITION_TYPES
        self._types = PRECONDITION_TYPES
        self._item = dict(item) if item else None
        self.setWindowTitle("编辑前置条件" if item else "添加前置条件")
        v = QVBoxLayout(self)

        v.addWidget(QLabel("类型"))
        self.type_combo = QComboBox()
        for t, spec in self._types.items():
            self.type_combo.addItem(spec["label"], t)
        v.addWidget(self.type_combo)

        self.form_host = QWidget()
        self.form = QFormLayout(self.form_host)
        v.addWidget(self.form_host)

        btns = QHBoxLayout()
        cancel = QPushButton("取消")
        ok = QPushButton("确定")
        ok.setObjectName("runBtn")
        cancel.clicked.connect(self.reject)
        ok.clicked.connect(self.accept)
        btns.addStretch(1)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        v.addLayout(btns)

        if item:      # 编辑: 锁定类型
            k = self.type_combo.findData(item.get("type"))
            if k >= 0:
                self.type_combo.setCurrentIndex(k)
            self.type_combo.setEnabled(False)
        self.type_combo.currentIndexChanged.connect(self._build_form)
        self._build_form()

    def _build_form(self):
        while self.form.count():
            it = self.form.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
        spec = self._types.get(self.type_combo.currentData()) or {}
        self._edits = {}
        for prm in spec.get("params", []):
            e = QLineEdit()
            cur = (self._item or {}).get(prm["key"], prm.get("default"))
            e.setText("" if cur is None else str(cur))
            if prm.get("hint"):
                e.setPlaceholderText(prm["hint"])
            self.form.addRow(prm["label"], e)
            self._edits[prm["key"]] = (e, prm)

    def values(self):
        """→ 前置项 dict(类型 + 参数, 按字段类型转换)"""
        t = self.type_combo.currentData()
        item = {"type": t, "enabled": True}
        if self._item:
            item["enabled"] = bool(self._item.get("enabled", True))
        for k, (e, prm) in self._edits.items():
            txt = e.text().strip()
            if prm["type"] == "int":
                if txt == "":
                    txt = prm.get("default", 0)
                try:
                    item[k] = int(txt)
                except ValueError:
                    item[k] = prm.get("default", 0)
            else:
                if txt:
                    item[k] = txt
        return item


class PreconditionsDialog(QDialog):
    """前置条件设置: 勾选启用 / 编辑 / 删除 / 新增(用户要求可编辑可新增)"""

    def __init__(self, items, parent=None):
        super().__init__(parent)
        from core.session import _item_label
        self.setWindowTitle("前置条件设置")
        self.resize(560, 380)
        self._label = _item_label
        self.items = [dict(x) for x in items]
        v = QVBoxLayout(self)
        v.addWidget(QLabel("按顺序执行; 取消勾选则不执行"))
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["启用 / 前置条件", "编辑", "删除"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setColumnWidth(1, 56)
        self.table.setColumnWidth(2, 56)
        self.table.verticalHeader().setVisible(False)
        v.addWidget(self.table)

        row = QHBoxLayout()
        add_btn = QPushButton("＋ 添加前置条件")
        add_btn.setObjectName("chipBtn")
        add_btn.clicked.connect(self._add)
        row.addWidget(add_btn)
        row.addStretch(1)
        cancel = QPushButton("取消")
        ok = QPushButton("确定")
        ok.setObjectName("runBtn")
        cancel.clicked.connect(self.reject)
        ok.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(ok)
        v.addLayout(row)
        self._render()

    def _render(self):
        self.table.setRowCount(0)
        for i, item in enumerate(self.items):
            self.table.insertRow(i)
            cb = QCheckBox(self._label(item))
            cb.setChecked(bool(item.get("enabled", True)))
            cb.toggled.connect(lambda on, k=i: self._toggle(k, on))
            self.table.setCellWidget(i, 0, cb)
            eb = QPushButton("编辑")
            eb.clicked.connect(lambda _=False, k=i: self._edit(k))
            self.table.setCellWidget(i, 1, eb)
            db = QPushButton("删除")
            db.setStyleSheet("color:#dc2626;")
            db.clicked.connect(lambda _=False, k=i: self._del(k))
            self.table.setCellWidget(i, 2, db)

    def _toggle(self, idx, on):
        if 0 <= idx < len(self.items):
            self.items[idx]["enabled"] = bool(on)

    def _edit(self, idx):
        dlg = PreconditionEditDialog(self.items[idx], self)
        if dlg.exec() == QDialog.Accepted:
            self.items[idx] = dlg.values()
            self._render()

    def _del(self, idx):
        if 0 <= idx < len(self.items):
            self.items.pop(idx)
            self._render()

    def _add(self):
        dlg = PreconditionEditDialog(None, self)
        if dlg.exec() == QDialog.Accepted:
            self.items.append(dlg.values())
            self._render()

    def values(self):
        return self.items


class NewCaseDialog(QDialog):'''
s = s.replace(anchor, cls, 1)

# ── 2) 导入 QFormLayout ──
old_imp = "    QDoubleSpinBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout,"
assert old_imp in s, "import line"
new_imp = "    QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout,"
s = s.replace(old_imp, new_imp)

open(p, "w", encoding="utf-8", newline="").write(s)
print("precondition dialogs added")
