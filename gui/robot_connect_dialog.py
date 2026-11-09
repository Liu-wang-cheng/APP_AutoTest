# -*- coding: utf-8 -*-
"""执行前选择/切换扫地机 IP 的弹窗。

用例执行前确认本轮对应的扫地机：连接后电量监测直接读参数（电量/电压/温度），
不连接则用 APP 电量兜底。弹窗里可以直接切换已保存的设备。
"""
import re

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import (QComboBox, QDialog, QHBoxLayout, QLabel,
                               QMessageBox, QPushButton, QVBoxLayout)


class RobotConnectDialog(QDialog):
    """返回 Accepted 表示继续执行；use_param 表示是否用参数电量。"""

    def __init__(self, device_page, parent=None):
        super().__init__(parent)
        self.device_page = device_page
        self.use_param = False
        self.setWindowTitle("选择扫地机")
        self.setMinimumWidth(460)
        self._build()
        self._load_devices()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(10)

        title = QLabel("执行前确认扫地机 IP")
        title.setStyleSheet("font-size:15px;font-weight:700;color:#1e293b;")
        root.addWidget(title)

        tip = QLabel("连接后电量监测可直接读取扫地机参数中的电量、电压和电池温度；\n"
                     "不连接则使用 APP 页面电量兜底。")
        tip.setStyleSheet("color:#64748b;font-size:11px;")
        root.addWidget(tip)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("扫地机IP"))
        self.combo = QComboBox()
        self.combo.setEditable(True)
        self.combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.combo.setMinimumWidth(280)
        self.combo.activated.connect(self._update_status)
        self.combo.lineEdit().returnPressed.connect(self._update_status)
        row.addWidget(self.combo, 1)
        root.addLayout(row)

        self.status = QLabel()
        self.status.setStyleSheet("color:#64748b;font-size:11px;")
        root.addWidget(self.status)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.app_btn = QPushButton("不连接，用 APP 电量继续")
        self.app_btn.setObjectName("chipBtn")
        self.app_btn.clicked.connect(self._use_app)
        buttons.addWidget(self.app_btn)
        self.connect_btn = QPushButton("连接并继续")
        self.connect_btn.setObjectName("primaryBtn")
        self.connect_btn.setDefault(True)
        self.connect_btn.clicked.connect(self._connect)
        buttons.addWidget(self.connect_btn)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        root.addLayout(buttons)

    def _load_devices(self):
        current = self.device_page._selected_ip()
        for dev in self.device_page._known_devices():
            ip = str(dev.get("ip") or "").strip()
            if not ip:
                continue
            label = "-".join(x for x in (dev.get("model", ""), dev.get("sn", ""), ip) if x)
            self.combo.addItem(label, ip)
        if current:
            for i in range(self.combo.count()):
                if self.combo.itemData(i) == current:
                    self.combo.setCurrentIndex(i)
                    break
            else:
                self.combo.setEditText(current)
        self._update_status()

    def _selected_ip(self):
        idx = self.combo.currentIndex()
        text = self.combo.currentText().strip()
        if idx >= 0 and text == self.combo.itemText(idx):
            data = self.combo.itemData(idx)
            if data:
                return str(data)
        m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", text)
        return m.group(1) if m else text

    def _update_status(self, *_):
        ip = self._selected_ip()
        if self.device_page.connected and ip == self.device_page.connected_ip:
            self.status.setText(f"当前已连接：{ip}")
            self.status.setStyleSheet("color:#16a34a;font-size:11px;")
            self.connect_btn.setText("继续使用该 IP")
            self.app_btn.setEnabled(True)
        elif self.device_page.connected:
            self.status.setText(f"当前连接：{self.device_page.connected_ip}，"
                                f"切换后将连接 {ip or '新 IP'}")
            self.status.setStyleSheet("color:#b45309;font-size:11px;")
            self.connect_btn.setText("切换并继续")
            self.app_btn.setEnabled(True)
        else:
            self.status.setText("当前未连接")
            self.status.setStyleSheet("color:#b45309;font-size:11px;")
            self.connect_btn.setText("连接并继续")
            self.app_btn.setEnabled(True)

    def _connect(self):
        ip = self._selected_ip()
        if not ip:
            QMessageBox.warning(self, "提示", "请选择或输入扫地机 IP")
            return
        if self.device_page.connected and ip == self.device_page.connected_ip:
            self.use_param = True
            self.accept()
            return
        if self.device_page.connecting:
            return
        self.connect_btn.setEnabled(False)
        self.connect_btn.setText("连接中…")
        # ★ 连接已改成后台线程(界面不卡)；这里用事件循环等结果，
        #   期间窗口仍可重绘/响应，弹窗也保持模态。
        result = {"ok": False, "msg": ""}
        loop = QEventLoop(self)
        started = False

        def on_finished(ok, done_ip, msg):
            if done_ip != ip:
                return
            result.update(ok=ok, msg=msg)
            loop.quit()

        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        self.device_page.connect_finished.connect(on_finished)
        try:
            started = self.device_page.connect_to(ip)
            if started and self.device_page.connecting:
                timer.start(25000)
                loop.exec()
        finally:
            timer.stop()
            try:
                self.device_page.connect_finished.disconnect(on_finished)
            except (RuntimeError, TypeError):
                pass
            self.connect_btn.setEnabled(True)
            self.connect_btn.setText("连接并继续")
        if not result["ok"]:
            why = result["msg"] or ("连接超时" if started else "无法发起连接")
            QMessageBox.warning(self, "连接失败",
                                f"无法连接 {ip}：{why}\n"
                                f"也可以选择「不连接，用 APP 电量继续」。")
            self._update_status()
            return
        self.use_param = True
        self.accept()

    def _use_app(self):
        self.use_param = False
        self.accept()
