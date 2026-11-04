# -*- coding: utf-8 -*-
"""测试报告面板(用户 2026-11-04): 底部「测试报告」页签。

列出 reports/ 下所有 .xlsx 报告(按修改时间倒序), 支持双击打开、多选删除。
只做文件级操作 —— 不解析报告内容, 打开交给系统关联程序(Excel/WPS)。
"""
import os

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QLabel,
                                QListWidget, QMessageBox, QPushButton,
                                QVBoxLayout, QWidget)

from core.logger import get_logger

log = get_logger()


class ReportsPanel(QWidget):
    """报告列表 + 刷新/打开/删除。reports_dir 由宿主传入(便于测试隔离)。"""

    def __init__(self, reports_dir, parent=None):
        super().__init__(parent)
        self.reports_dir = str(reports_dir)
        v = QVBoxLayout(self)
        v.setContentsMargins(6, 6, 6, 6)
        v.setSpacing(5)

        row = QHBoxLayout()
        self.count_label = QLabel("")
        self.count_label.setStyleSheet("color:#64748b;")
        row.addWidget(self.count_label)
        row.addStretch(1)
        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.setObjectName("chipBtn")
        self.refresh_btn.clicked.connect(self.refresh)
        row.addWidget(self.refresh_btn)
        self.open_btn = QPushButton("打开报告")
        self.open_btn.setObjectName("chipBtn")
        self.open_btn.setToolTip("打开选中的报告(也可双击行)")
        self.open_btn.clicked.connect(self.open_selected)
        row.addWidget(self.open_btn)
        self.del_btn = QPushButton("删除")
        self.del_btn.setObjectName("chipBtn")
        self.del_btn.setToolTip("删除选中的报告(可多选)")
        self.del_btn.clicked.connect(self.delete_selected)
        row.addWidget(self.del_btn)
        v.addLayout(row)

        self.list = QListWidget()
        self.list.setObjectName("reportList")
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.setAlternatingRowColors(True)
        self.list.itemDoubleClicked.connect(self._open_item)
        v.addWidget(self.list)
        self.refresh()

    # ── 数据 ──
    def report_paths(self):
        """reports/ 下的 .xlsx, 按修改时间倒序(最新在前)。"""
        try:
            names = [n for n in os.listdir(self.reports_dir)
                     if n.lower().endswith(".xlsx")
                     and not n.startswith("~$")]      # Excel 打开时的临时锁文件
        except OSError:
            return []
        # ★ 逐个 stat(代码审查 2026-11-04): 文件可能在列表与排序之间被删/被占用,
        #   直接 getmtime 会抛异常把**整个列表**清空; 拿不到的那份跳过即可。
        rows = []
        for n in names:
            p = os.path.join(self.reports_dir, n)
            try:
                rows.append((os.stat(p).st_mtime, p))
            except OSError:
                continue
        rows.sort(reverse=True)
        return [p for _mt, p in rows]

    def refresh(self):
        """重建列表; 尽量保持原选中(按路径)。"""
        keep = {p for p in self.selected_paths()}
        self.list.clear()
        for p in self.report_paths():
            try:
                import time as _t
                mt = _t.strftime("%Y-%m-%d %H:%M:%S",
                                 _t.localtime(os.path.getmtime(p)))
                size_kb = os.path.getsize(p) / 1024
                text = f"{os.path.basename(p)}    {mt}    {size_kb:.0f} KB"
            except OSError:
                text = os.path.basename(p)
            from PySide6.QtWidgets import QListWidgetItem
            it = QListWidgetItem(text)
            it.setData(0x0100, p)                 # Qt.UserRole
            it.setToolTip(p)
            self.list.addItem(it)
            if p in keep:
                it.setSelected(True)
        n = self.list.count()
        self.count_label.setText(f"共 {n} 份报告" if n else "暂无报告")

    def selected_paths(self):
        return [self.list.item(i).data(0x0100)
                for i in range(self.list.count())
                if self.list.item(i).isSelected()]

    # ── 操作 ──
    def _open_item(self, item):
        """双击只打开**被双击的那一行**(多选状态下不要把别的也一起打开)。"""
        p = item.data(0x0100) if item is not None else None
        if p:
            self.open_path(p)

    def open_selected(self):
        paths = self.selected_paths()
        if not paths:
            return
        for p in paths:
            self.open_path(p)

    @staticmethod
    def open_path(path):
        """用系统关联程序打开(Excel/WPS); 失败只记日志, 不弹错误打断。"""
        try:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        except Exception as e:
            log.warning(f"[报告] 打开失败 {path}: {type(e).__name__}: {e}")

    def delete_selected(self):
        paths = self.selected_paths()
        if not paths:
            return
        names = "\n".join(os.path.basename(p) for p in paths[:8])
        more = f"\n…等 {len(paths)} 份" if len(paths) > 8 else ""
        if QMessageBox.question(
                self, "删除报告",
                f"确定删除选中的 {len(paths)} 份报告?\n\n{names}{more}\n\n此操作不可恢复!"
        ) != QMessageBox.Yes:
            return
        failed = []
        for p in paths:
            try:
                os.remove(p)
            except OSError as e:
                failed.append(f"{os.path.basename(p)}: {e}")
        self.refresh()
        if failed:
            QMessageBox.warning(self, "部分删除失败", "\n".join(failed))
