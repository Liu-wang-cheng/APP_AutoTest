# -*- coding: utf-8 -*-
"""顶部双页面导航：自动化测试 / 设备参数。

主窗口只做两件事：把现有测试页交给 `install_pages`，并在关窗时调用
`device_page.shutdown()`。这样 main_window.py 的行数不会继续膨胀。
"""
import os
import re

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QComboBox, QFrame, QHBoxLayout, QLabel,
                                QMessageBox, QPushButton, QStackedWidget,
                                QVBoxLayout, QWidget)

from gui.device_page import DevicePage
from gui.sdk_setup import ensure_sdk

_NAV_QSS = """
QPushButton#pageTab {
    background: transparent; border: none; border-bottom: 2px solid transparent;
    padding: 8px 14px 6px; color: #94a3b8; font-size: 13px;
}
QPushButton#pageTab:hover { color: #475569; }
QPushButton#pageTab:checked {
    color: #2563eb; border-bottom-color: #2563eb; font-weight: 700;
}
QLabel#navStatus { color: #475569; font-size: 11px; padding-left: 14px; }
"""


class PageNav(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(_NAV_QSS)
        h = QHBoxLayout(self)
        h.setContentsMargins(14, 0, 14, 0)
        h.setSpacing(2)
        self.btn_test = QPushButton("自动化测试")
        self.btn_device = QPushButton("设备参数")
        for b in (self.btn_test, self.btn_device):
            b.setObjectName("pageTab")
            b.setCheckable(True)
            b.setAutoExclusive(True)
            h.addWidget(b)
        h.addStretch(1)
        self.run_status = QLabel("● 空闲")
        self.run_status.setObjectName("navStatus")
        self.dev_status = QLabel("● 设备未连接")
        self.dev_status.setObjectName("navStatus")
        h.addWidget(self.run_status)
        h.addWidget(self.dev_status)
        self.btn_test.setChecked(True)

    def set_active(self, name):
        (self.btn_device if name == "device" else self.btn_test).setChecked(True)

    def set_running(self, running):
        self.run_status.setText("● 运行中" if running else "● 空闲")
        self.run_status.setStyleSheet(
            "color:#2563eb;font-size:11px;" if running else "color:#475569;font-size:11px;")

    def set_device(self, connected, label=""):
        if connected:
            self.dev_status.setText("● 设备 已连接" + (f" · {label}" if label else ""))
            self.dev_status.setStyleSheet("color:#16a34a;font-size:11px;")
        else:
            self.dev_status.setText("● 设备未连接")
            self.dev_status.setStyleSheet("color:#475569;font-size:11px;")

    def set_connecting(self, connecting, ip=""):
        """切换/连接中: 右上角先显示「连接中 · IP」，结束后再由 set_device 定稿。"""
        if not connecting:
            return
        self.dev_status.setText("● 设备 连接中" + (f" · {ip}" if ip else ""))
        self.dev_status.setStyleSheet("color:#d97706;font-size:11px;")


def bind_robot_ip(window, device):
    """测试页顶部「扫地机IP」下拉框接到设备参数页的连接逻辑。"""
    window._robot_device = device
    window.robot_ip_combo.activated.connect(
        lambda *_: select_robot_ip(window, device))
    window.robot_ip_combo.lineEdit().returnPressed.connect(
        lambda: select_robot_ip(window, device))
    device.devicesChanged.connect(lambda: refresh_robot_ips(window, device))
    device.connectionChanged.connect(
        lambda *_: refresh_robot_ips(window, device, auto=False))
    # 连接是后台线程完成的：结果回来再更新提示(避免"正在连接"被误当成已连接)
    device.connect_finished.connect(
        lambda ok, ip, msg: window._set_status(
            f"扫地机已连接: {ip}" if ok else
            f"扫地机连接失败: {ip}" + (f"（{msg}）" if msg else "")))
    refresh_robot_ips(window, device)
    # 默认自动连接：启动/页面就绪后直接连上次 IP，不需要再点一次
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        QTimer.singleShot(900, lambda: auto_connect_robot(window, device))


def auto_connect_robot(window, device):
    if device.connected or device.connecting or getattr(window, "_robot_auto_connecting", False):
        return
    ip = device._selected_ip()
    if not ip:
        return
    window._robot_auto_connecting = True
    try:
        select_robot_ip(window, device, warn=False)
    finally:
        window._robot_auto_connecting = False


def _fit_robot_combo(combo, floor=240, cap=560, extra=72):
    """扫地机IP下拉按最长内容定宽（型号-SN-IP 很长时不能截断）。"""
    fm = combo.fontMetrics()
    texts = [combo.itemText(i) for i in range(combo.count())]
    current = combo.currentText().strip()
    if current:
        texts.append(current)
    longest = max((fm.horizontalAdvance(t) for t in texts), default=0)
    width = max(floor, min(longest + extra, cap))
    if combo.width() != width:
        combo.setFixedWidth(width)


def refresh_robot_ips(window, device, auto=True):
    """刷新扫地机IP下拉。

    auto=False: 连接状态变化触发的刷新**不再自动重连** —— 否则连接失败会
    「失败 → 刷新 → 自动重连 → 再失败」无限循环(用户 2026-11-06 日志里
    "连接失败" 后又出现 "正在连接")。
    """
    combo = window.robot_ip_combo
    current = device.connected_ip or combo.currentData()
    if not current:
        text = combo.currentText().strip()
        if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", text):
            current = text
    combo.blockSignals(True)
    combo.clear()
    for dev in device._known_devices():
        ip = str(dev.get("ip") or "").strip()
        if not ip:
            continue
        label = "-".join(x for x in (dev.get("model", ""), dev.get("sn", ""), ip) if x)
        combo.addItem(label, ip)
    target = current or str(device.settings.value("apos/last_ip", "") or "")
    idx = combo.findData(target)
    if idx >= 0:
        combo.setCurrentIndex(idx)
    elif target:
        combo.setEditText(target)
    combo.blockSignals(False)
    _fit_robot_combo(combo)
    if (auto and combo.count() and not device.connected
            and not getattr(device, "connecting", False)
            and not os.environ.get("PYTEST_CURRENT_TEST")):
        QTimer.singleShot(100, lambda: auto_connect_robot(window, device))


def select_robot_ip(window, device, warn=True):
    combo = window.robot_ip_combo
    text = combo.currentText().strip()
    idx = combo.currentIndex()
    ip = combo.itemData(idx) if idx >= 0 and text == combo.itemText(idx) else ""
    if not ip:
        m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", text)
        ip = m.group(1) if m else text
    if not ip:
        return
    started = device.connect_to(ip)
    if started:
        # 后台连接：已连着就提示已连接，否则先提示"正在连接"，结果由 connect_finished 更新
        window._set_status(
            f"扫地机已连接: {ip}" if device.connected
            else f"正在连接扫地机: {ip}")
    elif warn and not (device.connecting or getattr(device, "_disconnecting", False)):
        QMessageBox.warning(window, "连接扫地机失败",
                            f"无法连接 {ip}。请检查 IP 和网络后重试。")


def install_pages(window, test_page):
    """把测试页与设备页装进顶部导航 + QStackedWidget，返回新的中央控件。"""
    device = DevicePage(parent=window)
    stack = QStackedWidget()
    stack.addWidget(test_page)
    stack.addWidget(device)
    nav = PageNav()

    def show(name):
        idx = 1 if name == "device" else 0
        stack.setCurrentIndex(idx)
        nav.set_active(name)
        # ★ 切页后强制重新布局: 页面隐藏期间若有布局请求进来(实测: 在设备参数页
        #   滚动参数表会触发), 流式布局算出的高度可能被缓存成 0, 切回来时整行
        #   控件看不见。这里按当前尺寸重算一次, 不依赖 Qt 是否补发 resize。
        page = stack.widget(idx)
        if page is not None and page.layout() is not None:
            page.layout().activate()

    nav.btn_test.clicked.connect(lambda: show("test"))
    nav.btn_device.clicked.connect(lambda: show("device"))
    device.connectionChanged.connect(nav.set_device)
    device.connectingChanged.connect(nav.set_connecting)
    window.test_page = test_page
    window.device_page = device
    window.pages = stack
    window.page_nav = nav
    window._show_page = show
    # 自动化测试页是默认页；只有用户点击顶部「设备参数」才切过去
    show("test")
    # 扫地机 IP 单独一行(不和 APP 设备挤在同一行)
    robot_row = QFrame()
    robot_row.setObjectName("chipStrip")
    rh = QHBoxLayout(robot_row)
    rh.setContentsMargins(5, 4, 5, 4)
    rh.setSpacing(6)
    rh.addWidget(QLabel("扫地机IP"))
    robot_combo = QComboBox()
    robot_combo.setEditable(True)
    robot_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
    robot_combo.setMinimumContentsLength(16)
    robot_combo.lineEdit().textChanged.connect(
        lambda *_: _fit_robot_combo(robot_combo))
    robot_combo.setToolTip("连接扫地机后可读取电量/电压/温度；选择或输入 IP 后自动连接")
    rh.addWidget(robot_combo)
    rh.addStretch(1)
    window.robot_ip_combo = robot_combo
    layout = test_page.layout()
    layout.insertWidget(3, robot_row)
    # ★ 提示行(未选中用例/就绪/环境提示)放到扫地机IP下面(用户 2026-11-06):
    #   固定头部顺序 = 工具栏 / APP设备 / 扫地机IP / 提示行。
    hint = getattr(window, "hint_row", None)
    if hint is not None:
        layout.removeWidget(hint)
        layout.insertWidget(3, hint)
    bind_robot_ip(window, device)

    root = QWidget()
    v = QVBoxLayout(root)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(0)
    v.addWidget(nav)
    v.addWidget(stack, 1)

    if not os.environ.get("PYTEST_CURRENT_TEST") and not os.environ.get("APOS_SKIP_SDK_PROMPT"):
        def startup_check():
            if ensure_sdk(window):
                device._refresh_sdk()
        QTimer.singleShot(300, startup_check)
    return root
