# -*- coding: utf-8 -*-
"""与界面主题一致的更新弹窗（从 main_window 拆出，控制主窗口行数）。"""
from PySide6.QtWidgets import QMessageBox


def update_message(parent, heading, body="", kind="info", buttons=None,
                   default=0):
    """主题化 QMessageBox；返回被点击按钮的文本。"""
    box = QMessageBox(parent)
    box.setWindowTitle("检查更新")
    box.setIcon({"info": QMessageBox.Information, "warn": QMessageBox.Warning,
                 "error": QMessageBox.Critical}.get(kind, QMessageBox.Information))
    box.setText(f"<div style='font-size:14px;font-weight:bold;color:#1f2937'>{heading}</div>")
    if body:
        box.setInformativeText(body)
    box.setStyleSheet("""
        QMessageBox { background: #ffffff; }
        QMessageBox QLabel { color: #475569; font-family: "Microsoft YaHei UI";
                             font-size: 12px; line-height: 160%; }
        QMessageBox QPushButton {
            background: #ffffff; border: 1px solid #d9dee6; border-radius: 6px;
            padding: 6px 20px; color: #333; min-width: 80px;
        }
        QMessageBox QPushButton:hover { border-color: #2563eb; color: #2563eb; }
        QMessageBox QPushButton#primaryBtn {
            background: #2563eb; color: white; border: none; font-weight: bold;
        }
        QMessageBox QPushButton#primaryBtn:hover { background: #1d4fd7; }
    """)
    texts = list(buttons or ["知道了"])
    made = []
    for i, t in enumerate(texts):
        b = box.addButton(t, QMessageBox.AcceptRole if i == default
                          else QMessageBox.RejectRole)
        if i == default:
            b.setObjectName("primaryBtn")
        made.append(b)
    box.setDefaultButton(made[default])
    parent.raise_()
    parent.activateWindow()
    box.raise_()
    box.activateWindow()
    box.exec()
    clicked = box.clickedButton()
    for b, t in zip(made, texts):
        if b is clicked:
            return t
    return ""
