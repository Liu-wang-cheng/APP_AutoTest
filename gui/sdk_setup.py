# -*- coding: utf-8 -*-
"""首次配置 / 更换 AutopackDebugger SDK 的对话框。

用户 2026-11-05：启动时检查 `BASE_DIR\\apos\\`，缺文件就弹窗让用户选一次
AutopackDebugger 目录；复制 DLL + 提取 license 后不再依赖原目录。
"""
import os

from PySide6.QtWidgets import (QDialog, QFileDialog, QHBoxLayout, QLabel,
                                QMessageBox, QPushButton, QVBoxLayout)

from core.apos import sdk


class SdkSetupDialog(QDialog):
    def __init__(self, parent=None, target_dir=None):
        super().__init__(parent)
        self.setWindowTitle("选择 AutopackDebugger 目录")
        self.setModal(True)
        self.setMinimumWidth(460)
        self.target_dir = target_dir or sdk.default_sdk_dir()
        v = QVBoxLayout(self)
        v.setContentsMargins(18, 16, 18, 14)
        v.setSpacing(10)

        title = QLabel("请选择 AutopackDebugger 目录")
        title.setStyleSheet("font-size:15px;font-weight:bold;color:#1e293b;")
        v.addWidget(title)

        self.check_label = QLabel()
        self.check_label.setWordWrap(True)
        self.check_label.setStyleSheet(
            "background:#f8fafc;border:1px solid #e4e8ee;border-radius:7px;"
            "padding:6px 8px;color:#475569;font-size:11px;")
        v.addWidget(self.check_label)

        row = QHBoxLayout()
        row.addStretch(1)
        later = QPushButton("取消")
        later.clicked.connect(self.reject)
        choose = QPushButton("选择目录…")
        choose.setObjectName("primaryBtn")
        choose.setStyleSheet(
            "QPushButton#primaryBtn{background:#2563eb;color:white;border:none;"
            "font-weight:bold;padding:6px 18px;border-radius:6px;}")
        choose.clicked.connect(self.choose)
        row.addWidget(later)
        row.addWidget(choose)
        v.addLayout(row)
        self._refresh()

    def _refresh(self):
        info = sdk.check_sdk(self.target_dir)
        if info["ok"]:
            self.check_label.setText("✔ SDK 已就绪：" + str(self.target_dir))
        else:
            self.check_label.setText("✘ 缺少文件：" + "、".join(info["missing"]))

    def choose(self):
        src = QFileDialog.getExistingDirectory(self, "选择 AutopackDebugger 目录")
        if not src:
            return
        src_info = sdk.check_sdk(src, strict=False)
        if not src_info["ok"]:
            QMessageBox.warning(self, "目录不完整",
                                "该目录缺少：\n" + "\n".join(src_info["missing"]))
            return
        try:
            res = sdk.install_sdk(src, self.target_dir)
        except Exception as e:
            QMessageBox.warning(self, "复制失败", f"{type(e).__name__}: {e}")
            return
        if not res["ok"]:
            QMessageBox.warning(self, "复制不完整",
                                "复制后仍缺少：\n" + "\n".join(res["missing"]))
            self._refresh()
            return
        self.accept()


def ensure_sdk(parent=None, force=False):
    """确保工具目录里有 SDK；缺文件时弹窗。测试/CI 环境下不弹窗。"""
    if os.environ.get("APOS_SKIP_SDK_PROMPT") or os.environ.get("PYTEST_CURRENT_TEST"):
        return sdk.check_sdk(sdk.default_sdk_dir())["ok"]
    if not force and sdk.check_sdk(sdk.default_sdk_dir())["ok"]:
        return True
    dlg = SdkSetupDialog(parent)
    return dlg.exec() == QDialog.Accepted
