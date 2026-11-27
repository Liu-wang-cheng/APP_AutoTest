# -*- coding: utf-8 -*-
"""顶部双页面导航：自动化测试 / 设备参数。

主窗口只做两件事：把现有测试页交给 `install_pages`，并在关窗时调用
`device_page.shutdown()`。这样 main_window.py 的行数不会继续膨胀。
"""
import os
import re
import time

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtWidgets import (QComboBox, QFrame, QHBoxLayout, QLabel,
                                QListWidget, QMessageBox, QPushButton,
                                QStackedWidget, QVBoxLayout, QWidget)

from gui.ai_config_page import AiConfigPage
from gui.device_page import DevicePage
from gui.endurance_page import EnduranceCloseFilter, EndurancePage
from gui.loop_clean_page import LoopCleanPage, LoopCloseFilter
from gui.sdk_setup import ensure_sdk
from gui.widgets import NoWheelComboBox
_NAV_QSS = """
QPushButton#pageTab {
    background: transparent; border: none; border-bottom: 2px solid transparent;
    padding: 8px 10px 6px; color: #94a3b8; font-size: 12.5px;
}
QPushButton#pageTab:hover { color: #475569; }
QPushButton#pageTab:checked {
    color: #2563eb; border-bottom-color: #2563eb; font-weight: 700;
}
QLabel#navStatus { color: #475569; font-size: 11px; padding-left: 14px; }
"""


class _RobotIpCombo(NoWheelComboBox):
    """导航条 IP 下拉: 自绘弹层, 右边缘与输入框对齐、一次定位(用户 2026-11-27)。

    原生 QComboBox 弹层在 show 之后还会重定位, 无论怎么挪都会看到"右→左"的
    位移; 这里不用原生弹层, 直接建一个 Qt.Popup 列表, 位置只算一次。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._popup = None
        self._popup_closed_at = 0.0

    def eventFilter(self, obj, ev):
        # Qt.Popup 点击外部会自己隐藏(不经过 hidePopup), 这里补记关闭时间,
        # 否则紧接着的 showPopup 会把它重新打开 → 闪动(用户 2026-11-27)。
        if (obj is self._popup and ev.type() in (QEvent.Hide, QEvent.Close)
                and self._popup is not None):
            self._popup = None
            self._popup_closed_at = time.monotonic()
        return super().eventFilter(obj, ev)

    def mousePressEvent(self, ev):
        """已展开时点下拉按钮 = 关闭; 下次点击才重新打开(用户 2026-11-27)。"""
        if self._popup is not None and self._popup.isVisible():
            self._hide_custom_popup()
            ev.accept()
            return
        super().mousePressEvent(ev)

    def showPopup(self):
        # ★ 再次点击下拉框本意是"关掉": Qt.Popup 会在点击时先自动关闭,
        #   随后又走到 showPopup —— 200ms 内刚关过就直接返回, 否则会闪一下。
        if self._popup is not None and self._popup.isVisible():
            self._hide_custom_popup()
            return
        if time.monotonic() - self._popup_closed_at < 0.2:
            return
        self._hide_custom_popup()
        n = self.count()
        if n <= 0:
            return
        pop = QFrame(None, Qt.Popup)
        pop.setObjectName("robotIpPopup")
        pop.setStyleSheet(
            "QFrame#robotIpPopup{background:#ffffff;border:1px solid #d9dee6;"
            "border-radius:6px;}"
            "QListWidget{border:none;background:#ffffff;outline:none;"
            "font-size:12px;}"
            "QListWidget::item{padding:5px 8px;}"
            "QListWidget::item:selected{background:#eef4ff;color:#2563eb;}")
        lay = QVBoxLayout(pop)
        lay.setContentsMargins(2, 2, 2, 2)
        lst = QListWidget()
        lst.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        lst.setUniformItemSizes(True)
        for i in range(n):
            lst.addItem(self.itemText(i))
        lst.setCurrentRow(max(0, self.currentIndex()))
        lay.addWidget(lst)
        fm = self.fontMetrics()
        width = max(self.width(), min(
            max(fm.horizontalAdvance(self.itemText(i)) for i in range(n)) + 24,
            620))
        rows = min(n, 12)
        pop.resize(width, rows * (fm.height() + 10) + 8)
        lst.itemClicked.connect(lambda it: self._pick_row(lst.row(it), pop))
        lst.itemActivated.connect(lambda it: self._pick_row(lst.row(it), pop))
        pop.installEventFilter(self)
        self._popup = pop
        # ★ 先不显示内容 → show() 让 Qt 完成它自己的定位 → 下一拍按
        #   输入框右边缘重排 → 再打开绘制。用户只看到最终位置, 不会滑动。
        pop.setUpdatesEnabled(False)
        pop.show()
        QTimer.singleShot(0, lambda p=pop, w=width: self._place_custom_popup(p, w))
        lst.setFocus()

    def _place_custom_popup(self, pop, width):
        if pop is not self._popup:
            return
        try:
            right = self.mapToGlobal(self.rect().topRight()).x()
            screen = self.screen().availableGeometry()
            x = max(screen.left() + 4, right - width)
            y = self.mapToGlobal(self.rect().bottomLeft()).y() + 2
            pop.move(x, y)
        finally:
            pop.setUpdatesEnabled(True)
            pop.update()

    def _pick_row(self, row, pop):
        if row < 0:
            return
        self.setCurrentIndex(row)
        ip = self.itemData(row)
        if ip:
            self.setEditText(str(ip))
        pop.close()
        self.activated.emit(row)

    def _hide_custom_popup(self):
        if self._popup is not None:
            try:
                self._popup.close()
                self._popup.deleteLater()
            except Exception:
                pass
            self._popup = None
        self._popup_closed_at = time.monotonic()

    def hidePopup(self):
        self._hide_custom_popup()


class PageNav(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(_NAV_QSS)
        h = QHBoxLayout(self)
        h.setContentsMargins(14, 0, 14, 0)
        h.setSpacing(2)
        self.btn_test = QPushButton("自动化测试")
        self.btn_device = QPushButton("设备参数")
        self.btn_endurance = QPushButton("续航测试")
        self.btn_loop = QPushButton("循环清扫")
        self.btn_fault = QPushButton("灯显语音测试")
        self.btn_ai = QPushButton("AI 配置")
        for b in (self.btn_test, self.btn_device, self.btn_endurance,
                  self.btn_loop, self.btn_fault, self.btn_ai):
            b.setObjectName("pageTab")
            b.setCheckable(True)
            b.setAutoExclusive(True)
            h.addWidget(b)
        h.addStretch(1)
        # ★ 扫地机连接全局控件(用户 2026-11-27): 全工具只有这一份 IP 下拉 +
        #   连接/断开按钮。右上角分两行(用户 2026-11-27): 上=IP 与连接按钮,
        #   下=运行/连接状态指示灯。
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 3, 0, 3)
        rv.setSpacing(2)
        ip_line = QHBoxLayout()
        ip_line.setContentsMargins(0, 0, 0, 0)
        ip_line.setSpacing(6)
        ip_line.addStretch(1)
        self.robot_ip_combo = _RobotIpCombo()
        self.robot_ip_combo.setEditable(True)
        self.robot_ip_combo.setMinimumWidth(200)
        self.robot_ip_combo.lineEdit().setPlaceholderText("扫描或输入 IP")
        self.robot_ip_combo.setToolTip("扫描或输入扫地机 IP, 选定后自动连接")
        ip_line.addWidget(self.robot_ip_combo)
        self.robot_scan_btn = QPushButton("扫描")
        self.robot_scan_btn.setObjectName("chipBtn")
        ip_line.addWidget(self.robot_scan_btn)
        # 一个按钮: 未连接=连接, 已连接=断开(用户 2026-11-27)
        self.robot_connect_btn = QPushButton("连接")
        self.robot_connect_btn.setObjectName("chipBtn")
        ip_line.addWidget(self.robot_connect_btn)
        status_line = QHBoxLayout()
        status_line.setContentsMargins(0, 0, 0, 0)
        status_line.setSpacing(10)
        status_line.addStretch(1)
        self.run_status = QLabel("● 空闲")
        self.run_status.setObjectName("navStatus")
        self.dev_status = QLabel("● 扫地机未连接")
        self.dev_status.setObjectName("navStatus")
        status_line.addWidget(self.run_status)
        status_line.addWidget(self.dev_status)
        rv.addLayout(ip_line)
        rv.addLayout(status_line)
        h.addWidget(right)
        self._nav_combo_floor = 200
        self._nav_combo_cap = 520
        self._compact = False
        #: 正在跑的任务来源（自动化/续航…）——任何一个在跑都显示「运行中」
        self._active = set()
        self.btn_test.setChecked(True)

    def set_active(self, name):
        btn = {"device": self.btn_device, "endurance": self.btn_endurance,
               "loop": self.btn_loop,
               "fault": self.btn_fault, "ai": self.btn_ai}.get(
                   name, self.btn_test)
        btn.setChecked(True)

    def set_running(self, running, source="test"):
        """任务运行状态（用户 2026-11-26）：续航测试也要点亮「运行中」。

        source 区分来源，多个入口同时运行时不互相覆盖。
        """
        if running:
            self._active.add(source)
        else:
            self._active.discard(source)
        busy = bool(self._active)
        self.run_status.setText("● 运行中" if busy else "● 空闲")
        self.run_status.setStyleSheet(
            "color:#2563eb;font-size:11px;" if busy
            else "color:#475569;font-size:11px;")

    def set_device(self, connected, label=""):
        if connected:
            # label = 型号-SN(IP 已在左侧输入框), 完整值放 tooltip
            short = str(label or "")
            self.dev_status.setText(
                "● 已连接" + (f" · {short}" if short else ""))
            self.dev_status.setToolTip(str(label) or "已连接")
            self.dev_status.setStyleSheet("color:#16a34a;font-size:11px;")
        else:
            self.dev_status.setText("● 扫地机未连接")
            self.dev_status.setToolTip("")
            self.dev_status.setStyleSheet("color:#475569;font-size:11px;")

    def set_connecting(self, connecting, ip=""):
        """切换/连接中: 右上角先显示「连接中 · IP」，结束后再由 set_device 定稿。"""
        if not connecting:
            return
        self.dev_status.setText("● 连接中" + (f" · {ip}" if ip else ""))
        self.dev_status.setStyleSheet("color:#d97706;font-size:11px;")

    def resizeEvent(self, ev):
        """窄窗口自适应(用户 2026-11-27): 收窄 IP 框, 未连接时藏「断开」。"""
        super().resizeEvent(ev)
        compact = self.width() < 1250
        if compact != self._compact:
            self._compact = compact
            self._nav_combo_floor = 130 if compact else 200
            self._nav_combo_cap = 260 if compact else 520
        self.fit_robot_combo()

    def fit_robot_combo(self):
        """输入框只按 IP 定宽(选中后只显示 IP); 下拉弹层仍按最长项放宽。

        ★ 用户 2026-11-27: 输入框原来按 型号-SN-IP 定宽, 选中后只显示 IP 时
          显得过长。宽度按当前显示内容算, 弹层由 view 单独撑开。
        """
        combo = self.robot_ip_combo
        fm = combo.fontMetrics()
        current = combo.currentText().strip()
        width = max(150, min(fm.horizontalAdvance(current) + 34, 240))
        if combo.width() != width:
            combo.setFixedWidth(width)
        # 下拉列表保持 型号-SN-IP 的完整宽度, 但绝不能超出当前窗口
        items = [combo.itemText(i) for i in range(combo.count())]
        longest = max((fm.horizontalAdvance(t) for t in items), default=0)
        view = combo.view()
        want = max(width, min(longest + 18, 620))
        if view.minimumWidth() != want:
            view.setMinimumWidth(want)


def bind_robot_ip(window, device):
    """右上角导航条的全局「扫地机IP」接到设备参数页的连接逻辑。"""
    window._robot_device = device
    nav = window.nav
    def _toggle_robot(*_):
        if getattr(device, "connected", False):
            device.disconnect_device()
        else:
            select_robot_ip(window, device)
    nav.robot_connect_btn.clicked.connect(_toggle_robot)
    nav.robot_scan_btn.clicked.connect(device.scan)
    nav.robot_ip_combo.lineEdit().returnPressed.connect(
        lambda: select_robot_ip(window, device))
    nav.robot_ip_combo.activated.connect(
        lambda *_: select_robot_ip(window, device))
    device.devicesChanged.connect(lambda: refresh_robot_ips(window, device))
    device.connectionChanged.connect(
        lambda *_: refresh_robot_ips(window, device, auto=False))
    # 连接按钮文案跟随: 选中 IP == 已连接 IP → 「已连接」; 其余 → 「连接」
    def _sync_btns(*_):
        connecting = bool(getattr(device, "connecting", False))
        connected = bool(getattr(device, "connected", False))
        combo = nav.robot_ip_combo
        text = combo.currentText().strip()
        m = __import__("re").search(r"(\d{1,3}(?:\.\d{1,3}){3})", text)
        selected = m.group(1) if m else text
        nav.robot_connect_btn.setText(
            "连接中…" if connecting else ("断开" if connected else "连接"))
        nav.robot_connect_btn.setEnabled(not connecting)
        nav.fit_robot_combo()
    device.connectionChanged.connect(_sync_btns)
    device.connectingChanged.connect(_sync_btns)
    nav.robot_ip_combo.lineEdit().textChanged.connect(_sync_btns)
    nav.robot_ip_combo.activated.connect(_sync_btns)
    device.connect_finished.connect(
        lambda ok, ip, msg: window._set_status(
            f"扫地机已连接: {ip}" if ok else
            f"扫地机连接失败: {ip}" + (f"（{msg}）" if msg else "")))
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
    """扫地机IP下拉按最长内容定宽；导航条的走 PageNav.fit_robot_combo。"""
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
    if not current:
        # ★ 未连接时也跟随设备页当前/上次使用的 IP（用户 2026-11-27）
        try:
            current = device._selected_ip()
        except Exception:
            current = ""
    connected_ip = str(device.connected_ip or "")
    info = getattr(device, "_device_info", None) or {}
    devs = list(device._known_devices())
    if connected_ip and not any(
            str(d.get("ip") or "") == connected_ip for d in devs):
        # 设备列表里可能还没有这条（型号/SN 读得比连接结果晚）——用设备信息补上
        devs.append({"ip": connected_ip})
    combo.blockSignals(True)
    combo.clear()
    for dev in devs:
        ip = str(dev.get("ip") or "").strip()
        if not ip:
            continue
        model = dev.get("model") or (info.get("model", "")
                                     if ip == connected_ip else "")
        sn = dev.get("sn") or (info.get("sn", "")
                               if ip == connected_ip else "")
        label = "-".join(x for x in (model, sn, ip) if x)
        combo.addItem(label, ip)
    target = (current
              or str(getattr(device, "settings", None)
                     and device.settings.value("apos/last_ip", "")
                     or ""))
    idx = combo.findData(target)
    if idx >= 0:
        combo.setCurrentIndex(idx)
        # 选中后输入框只显示 IP; 下拉列表里仍是 型号-SN-IP(用户 2026-11-27)
        combo.setEditText(str(target))
    elif target:
        combo.setEditText(target)
    combo.blockSignals(False)
    nav = getattr(getattr(window, "nav", None), "fit_robot_combo", None)
    if nav is not None and combo is getattr(window, "robot_ip_combo", None):
        nav()
    else:
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
    # 选中后输入框只显示 IP(下拉项仍是 型号-SN-IP; 用户 2026-11-27)
    if combo.isEditable() and combo.currentText().strip() != ip:
        combo.setEditText(ip)
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
    endurance_page = EndurancePage(device=device, parent=window)
    loop_page = LoopCleanPage(device=device, parent=window)
    window.installEventFilter(
        EnduranceCloseFilter(endurance_page, parent=window))
    window.installEventFilter(
        LoopCloseFilter(loop_page, parent=window))
    fault_page = device.fault_page
    ai_page = AiConfigPage(parent=window)
    stack = QStackedWidget()
    stack.addWidget(test_page)
    stack.addWidget(device)
    stack.addWidget(endurance_page)
    stack.addWidget(loop_page)
    stack.addWidget(fault_page)
    stack.addWidget(ai_page)
    nav = PageNav()
    window.nav = nav
    pages = {"test": 0, "device": 1, "endurance": 2, "loop": 3,
             "fault": 4, "ai": 5}

    def show(name):
        idx = pages.get(name, 0)
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
    nav.btn_endurance.clicked.connect(lambda: show("endurance"))
    nav.btn_loop.clicked.connect(lambda: show("loop"))
    nav.btn_fault.clicked.connect(lambda: show("fault"))
    nav.btn_ai.clicked.connect(lambda: show("ai"))
    device.connectionChanged.connect(nav.set_device)
    device.connectingChanged.connect(nav.set_connecting)
    # 续航测试运行时右上角显示「运行中」（用户 2026-11-26）
    endurance_page.runningChanged.connect(
        lambda running: nav.set_running(running, "endurance"))
    loop_page.runningChanged.connect(
        lambda running: nav.set_running(running, "loop"))
    # 灯显语音测试的「批量测试」也点亮同一个运行指示灯
    device.runningChanged.connect(
        lambda running: nav.set_running(running, "fault"))
    window.test_page = test_page
    window.device_page = device
    window.endurance_page = endurance_page
    window.loop_page = loop_page
    window.fault_page = fault_page
    window.ai_page = ai_page
    window.pages = stack
    window.page_nav = nav
    window._show_page = show
    # 自动化测试页是默认页；只有用户点击顶部「设备参数」才切过去
    show("test")
    # ★ 扫地机IP连接已统一到右上角导航条(用户 2026-11-27), 测试页不再放
    #   自己的 IP 行; robot_ip_combo 指向导航条上的全局控件(旧引用兼容)。
    window.robot_ip_combo = nav.robot_ip_combo
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
