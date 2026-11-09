# -*- coding: utf-8 -*-
"""设备参数页：扫描/连接机器人，实时参数表与曲线。

页签在 MainWindow 顶部（自动化测试 / 设备参数），本页是独立整页。
"""
import json
import os
import re
import threading
import time

from PySide6.QtCore import QSettings, QThread, QTimer, Signal, Qt
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox,
                                QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                                QFrame, QHBoxLayout, QHeaderView, QLabel,
                                QLineEdit, QListWidget, QListWidgetItem, QMenu,
                                QMessageBox, QPushButton, QTabWidget,
                                QTableWidget, QTableWidgetItem, QVBoxLayout,
                                QWidget)

from core.apos import catalog, scanner, sdk as sdk_mod
from core.apos.client import AposClient, AposError
from core.apos.mcu import McuStream
from core.apos.service import ParamService
from core.apos.stream import UploadStream
from core.logger import get_logger
from gui.curve_view import CurveView
from gui.sdk_setup import ensure_sdk
from gui.widgets import FlowLayout

log = get_logger()


def _default_settings():
    return QSettings("vacuum_test", "case_studio")


def _is_pytest():
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


class ScanThread(QThread):
    finished = Signal(list)
    failed = Signal(str)

    def __init__(self, timeout=4.0, parent=None, keep_ips=None, known=None):
        super().__init__(parent)
        self.timeout = timeout
        self.keep_ips = keep_ips or set()
        self.known = {str(d.get("ip")): d for d in (known or [])
                      if isinstance(d, dict) and d.get("ip")}
        self.cancel_event = threading.Event()

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        items = []
        udp_error = ""
        try:
            items = scanner.scan(timeout=self.timeout,
                                 cancel_event=self.cancel_event,
                                 keep_ips=self.keep_ips)
        except Exception as e:
            # ★ 5550 被 AutopackDebugger 独占时 scanner.scan 会在 bind 处直接
            #   抛 OSError —— 这时也必须走 TCP 8801 兜底，不能只报"扫描失败"。
            udp_error = f"{type(e).__name__}: {e}"
            log.info(f"[扫描] UDP 扫描失败({udp_error}), 改用 TCP 兜底")
        try:
            # ★ UDP 5550 会被 AutopackDebugger 占用：它开着时广播响应会被它
            #   收走，我们只看到当前已连接的那台。没有发现"新设备"时用 TCP
            #   8801 扫一遍本网段兜底（用户 2026-11-09）。
            # UDP 完全没回包才兜底（通常就是 5550 被 AutopackDebugger 占用）；
            # 正常环境不额外扫网段，避免启动时和自动连接抢机器人。
            if not items:
                items = self._tcp_fallback(items)
            self.finished.emit(items)
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            if udp_error:
                msg = f"UDP: {udp_error}; TCP 兜底: {msg}"
            self.failed.emit(msg)

    def _tcp_fallback(self, items):
        found = {it["ip"]: it for it in items}
        client = None
        for ip in self.keep_ips:
            if ip and ip not in found and ip in self.known:
                found[ip] = dict(self.known[ip])

        def probe(ip):
            nonlocal client
            if ip in self.keep_ips:
                return self.known.get(ip) or {"ip": ip}
            try:
                if client is None:
                    client = AposClient()
                if not client.connect(ip, retries=1, delay=0.1):
                    return None
                data = client.read(["/System/CustomSN", "/System/FirmwareInfo"])
                fw = data.get("/System/FirmwareInfo")
                if isinstance(fw, str):
                    try:
                        fw = json.loads(fw)
                    except Exception:
                        fw = {}
                return {"ip": ip,
                        "sn": str(data.get("/System/CustomSN") or "").strip(),
                        "model": str((fw or {}).get("Model") or "").strip()
                        if isinstance(fw, dict) else ""}
            except Exception:
                return None
            finally:
                if client is not None:
                    try:
                        client.disconnect()
                    except Exception:
                        pass

        for cidr in scanner.local_cidrs():
            for it in scanner.tcp_fallback(cidr, probe,
                                           timeout=0.25,
                                           cancel_event=self.cancel_event):
                found.setdefault(it["ip"], it)
        log.info(f"[扫描] TCP 兜底发现 {len(found)} 台")
        return sorted(found.values(),
                      key=lambda x: tuple(int(p) for p in x["ip"].split(".")))


def read_robot_info(service, version_retries=1, include_base_station=True):
    """读型号/SN/固件版本/基站版本（只访问 SDK，不碰 UI，可在后台线程调用）。

    version_retries: 刚连上时基站版本文件可能还没就绪，连接流程多试几次。
    include_base_station=False: 只读快速项(SN/型号/固件)，基站版本单独后台补。
    """
    info = {"model": "", "sn": "", "firmware_version": "",
            "base_station_version": ""}
    try:
        data = service.read_once(["/System/CustomSN", "/System/FirmwareInfo"])
        info["sn"] = str(data.get("/System/CustomSN") or "").strip()
        fw = data.get("/System/FirmwareInfo")
        if isinstance(fw, str):
            try:
                fw = json.loads(fw)
            except Exception:
                fw = {}
        if isinstance(fw, dict):
            info["model"] = str(fw.get("Model") or "").strip()
            info["firmware_version"] = str(fw.get("Version") or "").strip()
    except Exception:
        pass
    client = getattr(service, "client", None)
    if include_base_station and client is not None:
        try:
            info["base_station_version"] = client.read_base_station_version(
                retries=version_retries)
        except Exception:
            pass
    return info


class ConnectThread(QThread):
    """后台连接机器人：SDK 连接 + 读设备信息都在子线程做，避免卡住界面。

    ★ 用户 2026-11-06 反馈切换 IP 时界面卡顿 —— 原来 connect/read 全在 UI 线程。
    """

    done = Signal(bool, str, dict)      # (ok, ip, 基础信息：型号/SN/固件)
    info_ready = Signal(str, dict)      # 补充信息(基站版本)，连接完成后再补

    def __init__(self, service, ip, parent=None):
        super().__init__(parent)
        self.service = service
        self.ip = ip
        self._cancel = threading.Event()

    def cancel(self):
        """尽快结束（UI 层已通知"连接完成"，余下的基站版本读取可以跳过）。"""
        self._cancel.set()

    def _sleep(self, seconds):
        """可取消的等待：切换设备/关窗时不用干等 3 秒。"""
        end = time.time() + seconds
        while not self._cancel.is_set():
            left = end - time.time()
            if left <= 0:
                break
            time.sleep(min(0.1, left))

    def run(self):
        t_start = time.perf_counter()
        try:
            ok = bool(self.service.connect(self.ip))
            log.info(f"[连接] {self.ip} SDK 连接用时 "
                     f"{time.perf_counter() - t_start:.2f}s")
        except Exception as e:
            self.done.emit(False, self.ip,
                           {"error": f"{type(e).__name__}: {e}"})
            return
        if not ok:
            client = getattr(self.service, "client", None)
            reason = getattr(client, "last_error", "")
            if reason == "unreachable":
                msg = "IP 不可达（设备离线 / 不在同一网段 / 端口 8801 未开放）"
            else:
                msg = "机器人未响应（SDK 初始化失败，检查 IP / 网络 / 端口 8801-8802）"
            self.done.emit(False, self.ip, {"error": msg})
            return
        # ★ 先只读快速项(SN/型号/固件)，立刻报告"已连接"；基站版本读文件 +
        #   发事件可能要 2~3 秒(没有基站的机型更久)，不能拖慢连接速度。
        info = {}
        try:
            info = read_robot_info(self.service, include_base_station=False)
        except Exception:
            info = {}
        # 刚连上时偶发读不到 SN/固件（机器人忙/流刚起来），补读几次
        for _ in range(3):
            if info.get("sn") or info.get("model"):
                break
            try:
                time.sleep(0.4)
                info = read_robot_info(self.service,
                                       include_base_station=False)
            except Exception:
                pass
        self.done.emit(True, self.ip, info)
        # 先把推送流开起来（参数/曲线立刻有数据），再在后台读基站版本。
        # 基站在机器人上是文件、只能走 librpc_base，读的时候会临时停流，
        # 由 ParamService.read_base_station 负责停/恢复。
        start_stream = getattr(self.service, "start_stream", None)
        if callable(start_stream):
            try:
                start_stream()
            except Exception as e:
                log.debug(f"[连接] 上传流启动失败(退回轮询): {e}")
        reader = getattr(self.service, "read_base_station", None)
        client = getattr(self.service, "client", None)
        version = ""
        # ★ 先让推送流跑几秒（曲线/参数先出数据），再停流读基站版本文件；
        #   否则一连上就停流 2~6 秒，看起来像"连接很慢/参数不出来"。
        self._sleep(3.0)
        if not self._cancel.is_set():
            try:
                if callable(reader):
                    version = reader()
                elif client is not None:
                    version = client.read_base_station_version(retries=3,
                                                               delay=1.0)
            except Exception:
                version = ""
        if version and not self._cancel.is_set():
            self.info_ready.emit(self.ip, {"base_station_version": version})


class DisconnectThread(QThread):
    """后台断开连接：ParamService.stop 最多 join 5 秒，放 UI 线程会卡死窗口。"""

    done = Signal()

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service

    def run(self):
        try:
            self.service.disconnect()
        except Exception:
            pass
        self.done.emit()


class DevicePage(QWidget):
    connectionChanged = Signal(bool, str)     # (connected, label)
    connectingChanged = Signal(bool, str)     # (是否连接中, ip) —— 右上角显示"连接中"
    devicesChanged = Signal()                 # 设备列表/型号/SN 更新
    connect_finished = Signal(bool, str, str)  # (ok, ip, 错误信息) 连接结束

    def __init__(self, parent=None, settings=None, service_factory=None,
                 sdk_dir=None):
        super().__init__(parent)
        self.settings = settings or _default_settings()
        self.sdk_dir = sdk_dir or sdk_mod.default_sdk_dir()
        self._service_factory = service_factory
        self._service = None
        self._scan_thread = None
        self._connect_thread = None
        self._disconnect_thread = None
        self._connect_ip = ""
        self._disconnecting = False
        self._connected = False
        self._connected_ip = ""
        self._connecting = False
        self._current_page = 0
        self._rows = []               # [(group, item, row_index)]
        self._last_snapshot = {"ok": {}, "failed": {}, "ts": 0}
        self._device_info = {}        # 型号/SN/固件版本/基站版本（连接后补齐）
        self._devices = []            # 本次扫描到的可连接设备(实时列表)
        self._auto_scan_done = False
        self._scan_quiet = False
        self._build()
        self._load_devices()
        self._refresh_sdk()
        self._timer = QTimer(self)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._poll)
        self._timer.start()
        # ★ 启动后台扫一次: IP 列表只保留"此刻能连"的设备(用户 2026-11-06),
        #   历史里已经离线的 IP 会被这次扫描清掉。
        if not _is_pytest():
            self._auto_scan_done = True
            QTimer.singleShot(1500, lambda: self.scan(quiet=True))

    # ── UI ──
    def _build(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 12, 14, 14)
        v.setSpacing(8)

        head = QHBoxLayout()
        left = QVBoxLayout()
        title = QLabel("设备参数")
        title.setStyleSheet("font-size:16px;font-weight:700;color:#1e293b;")
        left.addWidget(title)
        head.addLayout(left)
        head.addSpacing(18)

        # ★ 顶部控件用流式布局(2026-11-06): 窗口窄时自动换行, 不把页面的
        #   最小宽度顶大 —— 否则切到本页会把整个窗口自动撑宽(用户要求界面
        #   大小只由手动拖拽决定)。
        bar_host = QWidget()
        bar = FlowLayout(bar_host, margin=0, spacing=7)
        bar.addWidget(QLabel("扫地机IP"))
        self.device_combo = QComboBox()
        self.device_combo.setEditable(True)
        self.device_combo.setMinimumContentsLength(16)
        self.device_combo.setMaximumWidth(560)
        self.device_combo.lineEdit().setPlaceholderText("扫描或输入 IP")
        self.device_combo.activated.connect(self._device_activated)
        self.device_combo.lineEdit().returnPressed.connect(self._device_activated)
        # 型号-SN-IP 比较长：文本框按最长内容自适应宽度（用户 2026-11-06）
        self.device_combo.lineEdit().textChanged.connect(
            lambda *_: self._fit_ip_combo_width())
        bar.addWidget(self.device_combo)
        self.scan_btn = QPushButton("扫描")
        self.scan_btn.setObjectName("chipBtn")
        self.scan_btn.clicked.connect(self.scan)
        bar.addWidget(self.scan_btn)
        self.connect_btn = QPushButton("连接")
        self.connect_btn.setObjectName("runBtn")
        self.connect_btn.clicked.connect(self.connect_device)
        bar.addWidget(self.connect_btn)
        self.disconnect_btn = QPushButton("断开")
        self.disconnect_btn.clicked.connect(self.disconnect_device)
        bar.addWidget(self.disconnect_btn)
        self.interval_combo = QComboBox()
        for s in ("1", "2", "5", "10"):
            self.interval_combo.addItem(f"刷新 {s} 秒", int(s))
        self.interval_combo.currentIndexChanged.connect(self._interval_changed)
        bar.addWidget(self.interval_combo)
        head.addWidget(bar_host, 1)
        v.addLayout(head)

        # ★ 说明 + 扫描/连接状态单独一行(用户 2026-11-06): 不和 IP/按钮/刷新挤在一起。
        #   这两段都是短提示, 不换行(换行会变成两行, 用户反馈)。
        hint_row = QHBoxLayout()
        hint_row.setSpacing(14)
        sub = QLabel("实时读取扫地机内部属性（只读）")
        sub.setStyleSheet("color:#94a3b8;font-size:11px;")
        sub.setWordWrap(False)
        hint_row.addWidget(sub)
        self.status_label = QLabel("")     # 只放扫描提示; 连接状态统一在右上角导航
        self.status_label.setStyleSheet("color:#94a3b8;font-size:11px;")
        self.status_label.setWordWrap(False)
        hint_row.addWidget(self.status_label)
        hint_row.addStretch(1)
        v.addLayout(hint_row)

        self.sdk_line = QFrame()
        self.sdk_line.setStyleSheet(
            "QFrame{background:#f8fafc;border:1px solid #e4e8ee;border-radius:6px;}")
        sl = QHBoxLayout(self.sdk_line)
        sl.setContentsMargins(8, 5, 8, 5)
        sl.setSpacing(8)
        self.sdk_label = QLabel()
        self.sdk_label.setStyleSheet("color:#475569;font-size:11px;")
        self.sdk_label.setWordWrap(True)
        sl.addWidget(self.sdk_label, 1)
        self.sdk_btn = QPushButton("更换 SDK")
        self.sdk_btn.setObjectName("chipBtn")
        self.sdk_btn.clicked.connect(self._change_sdk)
        sl.addWidget(self.sdk_btn)
        v.addWidget(self.sdk_line)

        self.info_line = QFrame()
        self.info_line.setStyleSheet(
            "QFrame{background:#f8fafc;border:1px solid #e4e8ee;border-radius:6px;}")
        il = QHBoxLayout(self.info_line)
        il.setContentsMargins(8, 5, 8, 5)
        il.setSpacing(8)
        self.info_label = QLabel()
        self.info_label.setStyleSheet("color:#475569;font-size:11px;")
        self.info_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.info_label.setWordWrap(True)
        il.addWidget(self.info_label, 1)
        v.addWidget(self.info_line)
        self._update_info_label()

        self.tabs = QTabWidget()
        self.tabs.currentChanged.connect(self._tab_changed)
        self.params_tab = QWidget()
        self._build_params(self.params_tab)
        self.tabs.addTab(self.params_tab, "实时参数")
        self.curve_view = CurveView(settings=self.settings)
        self.curve_view.pathsChanged.connect(self._curve_paths_changed)
        self.tabs.addTab(self.curve_view, "曲线")
        disabled = QLabel("快速控制 / 故障模拟：后续版本")
        disabled.setStyleSheet("color:#b0b9c6;padding:20px;")
        self.tabs.addTab(disabled, "快速控制 · 后续")
        v.addWidget(self.tabs, 1)

    def _build_params(self, parent):
        v = QVBoxLayout(parent)
        v.setContentsMargins(0, 8, 0, 0)
        v.setSpacing(7)
        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索参数、路径…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._rebuild_table)
        bar.addWidget(self.search, 1)
        self.only_changed = QCheckBox("只看变化")
        self.only_changed.toggled.connect(self._rebuild_table)
        bar.addWidget(self.only_changed)
        self.count_label = QLabel("")
        self.count_label.setStyleSheet("color:#94a3b8;font-size:11px;")
        bar.addWidget(self.count_label)
        v.addLayout(bar)

        body = QHBoxLayout()
        body.setSpacing(9)
        self.cat_list = QListWidget()
        self.cat_list.setObjectName("caseList")
        self.cat_list.setFixedWidth(172)
        self.cat_list.currentRowChanged.connect(self._rebuild_table)
        body.addWidget(self.cat_list)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ["参数名", "当前值", "单位", "更新时间"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectItems)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setWordWrap(True)
        self.table.setTextElideMode(Qt.ElideNone)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_menu)
        QShortcut(QKeySequence.Copy, self.table, self._copy_selection)
        # ★ 固定列宽(用户 2026-11-06): 参数名/单位/时间固定, 「当前值」占满剩余宽度。
        #   内容再长也只在单元格内换行, 列宽/窗口不会随数据变化。
        hdr = self.table.horizontalHeader()
        hdr.setStretchLastSection(False)
        for col, width in ((0, 220), (2, 70), (3, 100)):
            hdr.setSectionResizeMode(col, QHeaderView.Interactive)
            self.table.setColumnWidth(col, width)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        body.addWidget(self.table, 1)
        v.addLayout(body, 1)
        self._init_categories()

    def _init_categories(self):
        self.cat_list.clear()
        total = sum(len(g["items"]) for g in catalog.PARAM_GROUPS)
        QListWidgetItem(f"全部    {total}", self.cat_list)
        for g in catalog.PARAM_GROUPS:
            QListWidgetItem(f'{g["name"]}    {len(g["items"])}', self.cat_list)
        self.cat_list.setCurrentRow(0)
        self._rebuild_table()

    # ── SDK ──
    def _refresh_sdk(self):
        info = sdk_mod.check_sdk(self.sdk_dir)
        if info["ok"]:
            self.sdk_label.setText(f"SDK 目录  {self.sdk_dir}    ✔ 文件齐全")
            self.sdk_label.setStyleSheet("color:#475569;font-size:11px;")
            self.connect_btn.setEnabled(True)
        else:
            self.sdk_label.setText(
                "SDK 目录  未检测到 AutopackDebugger    ✘ 需要选择目录")
            self.sdk_label.setStyleSheet("color:#b45309;font-size:11px;")
            self.connect_btn.setEnabled(False)

    def _change_sdk(self):
        if ensure_sdk(self, force=True):
            self._refresh_sdk()
            self._service = None

    # ── 设备列表 ──
    def _load_devices(self):
        """启动时只恢复"上次连接的 IP"作为输入框文本。

        ★ 不再加载历史设备列表(用户 2026-11-06): IP 列表只应显示本次扫描到、
          此刻能连的设备；历史里离线的 IP 一律不出现。启动后会自动后台扫描一次。
        """
        last_ip = str(self.settings.value("apos/last_ip", "") or "").strip()
        if last_ip:
            self.device_combo.setEditText(last_ip)
        self._fit_ip_combo_width()

    def _fit_ip_combo_width(self):
        """输入框宽度跟随内容 —— 「型号-SN-IP」可能很长, 不能截断（有上下限）。"""
        fm = self.device_combo.fontMetrics()
        texts = [self.device_combo.itemText(i)
                 for i in range(self.device_combo.count())]
        current = self.device_combo.currentText().strip()
        if current:
            texts.append(current)
        longest = max((fm.horizontalAdvance(t) for t in texts), default=0)
        width = max(240, min(longest + 72, 560))
        if self.device_combo.width() != width:
            self.device_combo.setFixedWidth(width)

    def _select_ip(self, ip):
        for i in range(self.device_combo.count()):
            if str(self.device_combo.itemData(i) or "") == ip:
                self.device_combo.setCurrentIndex(i)
                return True
        return False

    def _remember_ip(self, ip=None):
        ip = str(ip or self._selected_ip() or "").strip()
        if ip:
            self.settings.setValue("apos/last_ip", ip)

    def _add_device_item(self, dev, select=True):
        ip = (dev.get("ip") or "").strip()
        if not ip:
            return
        label = "-".join(x for x in (dev.get("model", ""), dev.get("sn", ""), ip) if x)
        for i in range(self.device_combo.count()):
            if self.device_combo.itemData(i) == ip:
                self.device_combo.setItemText(i, label)
                if select:
                    self.device_combo.setCurrentIndex(i)
                self._fit_ip_combo_width()
                return
        self.device_combo.addItem(label, ip)
        if select:
            self.device_combo.setCurrentIndex(self.device_combo.count() - 1)
        self._fit_ip_combo_width()

    def _save_devices(self, devices):
        # ★ 只保存在内存里(本次扫描/当前连接): IP 列表是实时的, 不做历史持久化
        self._devices = [d for d in devices if str(d.get("ip") or "").strip()]
        self.devicesChanged.emit()

    def _known_devices(self):
        return list(self._devices)

    def _selected_ip(self):
        idx = self.device_combo.currentIndex()
        text = self.device_combo.currentText().strip()
        if idx >= 0 and text == self.device_combo.itemText(idx):
            data = self.device_combo.itemData(idx)
            if data:
                return str(data)
        m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", text)
        return m.group(1) if m else text

    # ── 扫描 ──
    def scan(self, quiet=False):
        if self._scan_thread and self._scan_thread.isRunning():
            return
        self._scan_quiet = quiet          # 启动时后台扫描失败不弹窗
        self.scan_btn.setEnabled(False)
        self.status_label.setText("扫描中…")
        self.status_label.setStyleSheet("color:#2563eb;")
        self._scan_thread = ScanThread(timeout=6.0, parent=self,
                                       keep_ips={self._connected_ip or self._connect_ip},
                                       known=list(self._devices))
        self._scan_thread.finished.connect(self._scan_done)
        self._scan_thread.failed.connect(self._scan_failed)
        self._scan_thread.start()

    def _scan_done(self, devices):
        self.scan_btn.setEnabled(True)
        keep_ip = self._connected_ip or self._connect_ip
        prev_ip = self._selected_ip()
        devices = list(devices)
        # ★ 正在连接/已连接的设备即使这轮 UDP 漏收或被探测拒绝, 也要保留在列表里
        #   (否则会出现"连着它、列表里却没有它")
        if keep_ip and not any(str(d.get("ip")) == keep_ip for d in devices):
            info = self._device_info or {}
            devices.append({"ip": keep_ip,
                            "model": info.get("model", ""),
                            "sn": info.get("sn", "")})
        # ★ 用本次扫描结果**替换**列表(用户 2026-11-06): 历史里已经离线的设备
        #   不该继续出现在 IP 下拉里 —— 列表必须是"这次扫得到、此刻能连"的。
        self._save_devices(devices)
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        for d in devices:
            self._add_device_item(d, select=False)
        self.device_combo.blockSignals(False)
        target = (keep_ip or prev_ip
                  or str(self.settings.value("apos/last_ip", "") or "").strip())
        if target and self._select_ip(target):
            pass
        elif self.device_combo.count():
            self.device_combo.setCurrentIndex(0)
        elif target:
            self.device_combo.setEditText(target)
        self._fit_ip_combo_width()
        if not devices:
            self.status_label.setText("未发现可连接的设备，可手动输入 IP")
            self.status_label.setStyleSheet("color:#b45309;")
            log.info("[扫描] 未发现可连接设备")
            return
        log.info("[扫描] 可连接设备 %d 台: %s" % (
            len(devices), ", ".join(str(d.get("ip")) for d in devices)))
        self.status_label.setText(f"已发现 {len(devices)} 台可连接设备")
        self.status_label.setStyleSheet("color:#16a34a;")
        if (os.environ.get("AUTOTEST_AUTO_CONNECT") and devices
                and not self.connected and not self._connecting):
            QTimer.singleShot(100, self.connect_device)

    def _scan_failed(self, msg):
        self.scan_btn.setEnabled(True)
        self.status_label.setText("扫描失败")
        self.status_label.setStyleSheet("color:#dc2626;")
        if not getattr(self, "_scan_quiet", False):
            QMessageBox.warning(self, "扫描失败", msg)
        else:
            # 启动扫描失败(常见: 端口被 AutopackDebugger 占用) → 进入本页时再试
            self._auto_scan_done = False

    # ── 连接 ──
    def _make_service(self):
        if self._service is not None:
            return self._service
        if self._service_factory is not None:
            self._service = self._service_factory()
        else:
            client = AposClient(self.sdk_dir)
            # ★ 和 AutopackDebugger 一样走属性上传流(推送)；批量读退居兜底。
            self._service = ParamService(
                client, catalog.all_param_paths(),
                interval=self._table_interval(),
                stream=UploadStream(self.sdk_dir),
                stream_paths=catalog.all_monitor_paths(),
                mcu=McuStream())
        return self._service

    def _table_interval(self):
        return float(self.settings.value("apos/table_interval", 2))

    def _curve_hz(self):
        return float(self.settings.value("apos/curve_hz", 5))

    def _device_activated(self, *_):
        """下拉选中 / 输入框回车 → 直接连接，不用再点「连接」。"""
        ip = self._selected_ip()
        if not ip:
            return
        if self._connected and ip == self._connected_ip:
            return
        if self._connecting or self._disconnecting:
            return                  # 切换中: 等这次结束，避免并发连接
        self.connect_device()

    def _table_menu(self, pos):
        item = self.table.itemAt(pos)
        if item is None:
            return
        row = item.row()
        menu = QMenu(self)
        a_value = menu.addAction("复制值")
        a_name = menu.addAction("复制参数名")
        menu.addSeparator()
        a_chart = menu.addAction("添加到曲线…")
        menu.addSeparator()
        a_row = menu.addAction("复制整行")
        a_sel = menu.addAction("复制选中单元格")
        act = menu.exec(self.table.viewport().mapToGlobal(pos))
        if act is a_chart:
            rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
            self._add_rows_to_curves(rows or [row])
        elif act is a_value:
            QApplication.clipboard().setText(item.text())
        elif act is a_name:
            QApplication.clipboard().setText(
                (self.table.item(row, 0).text() if self.table.item(row, 0) else ""))
        elif act is a_row:
            vals = [self.table.item(row, c).text() if self.table.item(row, c) else ""
                    for c in range(self.table.columnCount())]
            QApplication.clipboard().setText("\t".join(vals))
        elif act is a_sel:
            self._copy_selection()

    def _add_rows_to_curves(self, rows):
        """把表格里选中的参数添加到曲线页的「自定义」页（每条一张图）。"""
        # rows 是表格行号（中间夹着分组标题行），不能直接当下标用。
        by_row = {r: item for _g, item, r in self._rows}
        items, seen = [], set()
        for r in sorted(set(rows)):
            item = by_row.get(r)
            if item is None:
                continue
            if item["path"] in seen:
                continue
            seen.add(item["path"])
            items.append(item)
        if not items:
            return
        added = 0
        target_name = ""
        for it in items:
            default = it.get("name") or it["path"].rsplit("/", 1)[-1]
            # ★ 值的解释方式必须跟着曲线走: 里程计 scale、地检 bit、沿墙 decode
            #   丢了的话曲线会画成原始值/一直空白（审查 2026-11-09）。
            meta = {k: it[k] for k in self.curve_view.VALUE_KEYS
                    if it.get(k) not in (None, "")}
            target = None
            if len(items) == 1:
                title, series, target = self._ask_curve_target(
                    default, self.curve_view.curve_targets())
                if not title:
                    return                     # 用户取消
            else:
                title = series = default       # 多选: 按默认名各建一张图
            if target is None:
                self.curve_view.add_custom_curve(it["path"], title, series,
                                                 it.get("unit", ""), meta=meta)
            elif target.get("kind") == "custom":
                self.curve_view.add_series_to_chart(
                    target["index"], it["path"], series, it.get("unit", ""),
                    meta=meta)
                target_name = target["label"]
            else:
                self.curve_view.add_series_to_group(
                    target["page"], target["group"], it["path"], series,
                    it.get("unit", ""), meta=meta)
                target_name = target["label"]
            added += 1
        if target_name:
            self.status_label.setText(
                f"已添加到曲线图「{target_name}」：{added} 条")
        else:
            self.status_label.setText(f"已添加曲线图：{added} 张（页名即曲线图名称）")
        self.status_label.setStyleSheet("color:#16a34a;")

    def _ask_curve_target(self, default, targets):
        """右键添加曲线：选「新建曲线图」或加入已有图（自定义/内置），并可命名。

        returns (标题, 参数名称, 目标 dict 或 None)；取消返回 (None, None, None)。
        """
        dlg = QDialog(self)
        dlg.setWindowTitle("添加到曲线")
        form = QFormLayout(dlg)
        target_combo = QComboBox()
        target_combo.addItem("新建曲线图")
        for t in targets:
            target_combo.addItem(f'加入 {t["label"]}')
        form.addRow("目标", target_combo)
        chart_edit = QLineEdit(default)
        series_edit = QLineEdit(default)
        form.addRow("曲线图名称", chart_edit)
        form.addRow("参数名称", series_edit)

        def _sync_target(*_):
            idx = target_combo.currentIndex()
            if idx > 0:
                chart_edit.setText(targets[idx - 1]["label"])
                chart_edit.setEnabled(False)
            else:
                chart_edit.setEnabled(True)
                if not chart_edit.text().strip():
                    chart_edit.setText(default)

        target_combo.currentIndexChanged.connect(_sync_target)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        form.addRow(buttons)
        series_edit.selectAll()
        series_edit.setFocus()
        if dlg.exec() != QDialog.Accepted:
            return None, None, None
        idx = target_combo.currentIndex()
        if idx > 0:
            target = targets[idx - 1]
            title = target["label"]
        else:
            title = chart_edit.text().strip() or default
            target = None
        series = series_edit.text().strip() or default
        return title, series, target

    def _copy_selection(self):
        ranges = self.table.selectedRanges()
        if not ranges:
            cur = self.table.currentItem()
            if cur is not None:
                QApplication.clipboard().setText(cur.text())
            return
        lines = []
        for r in ranges:
            for row in range(r.topRow(), r.bottomRow() + 1):
                vals = []
                for col in range(r.leftColumn(), r.rightColumn() + 1):
                    it = self.table.item(row, col)
                    vals.append(it.text() if it is not None else "")
                lines.append("\t".join(vals))
        QApplication.clipboard().setText("\n".join(lines))

    def connect_device(self):
        """发起连接（后台线程，UI 不阻塞）。结果见 _connect_done / connect_finished。"""
        if self._connecting:
            return
        # ★ 上一台的 ConnectThread 在 done 之后还要补读基站版本（几秒），
        #   这段时间不能把新连接插进去：两个线程会同时操作同一个 SDK 句柄。
        old = self._connect_thread
        if old is not None and old.isRunning():
            old.cancel()
            old.wait(800)          # 可取消的等待, 通常几十毫秒就退出
            if old.isRunning():
                self.status_label.setText("上一台设备仍在初始化，请稍后重试")
                return
        ip = self._selected_ip()
        if not ip:
            QMessageBox.warning(self, "提示", "请先扫描或输入扫地机 IP")
            return
        # ★ 先占住"连接中"标志: 创建 SDK 服务也要时间, 这期间别让自动连接/再次
        #   点击插进来并发连接(偶发"连接失败"的来源之一)。
        self._connecting = True
        started = False
        try:
            if not ensure_sdk(self):
                return
            try:
                service = self._make_service()
            except AposError as e:
                QMessageBox.warning(self, "SDK 不可用", str(e))
                return
            # ★ 不再在 UI 线程 disconnect + connect + 读参数(切换 IP 卡顿的根因):
            #   AposClient.connect 内部会先断开旧连接; 读设备信息也在 ConnectThread 里做。
            self._connected = False
            self._connected_ip = ""
            self._connect_ip = ip
            self.connect_btn.setEnabled(False)
            self.device_combo.setEnabled(False)
            self.disconnect_btn.setEnabled(False)
            # 连接状态统一由右上角导航显示(connectingChanged)，本页不再重复提示
            self.connectingChanged.emit(True, ip)
            thread = ConnectThread(service, ip, self)
            thread.done.connect(self._connect_done)
            thread.info_ready.connect(self._info_ready)
            # ★ 不能在 done 时就丢引用: 线程还要补读基站版本, 提前清空会让
            #   shutdown() 等不到它, 窗口析构销毁运行中的 QThread → Qt 崩溃。
            thread.finished.connect(
                lambda t=thread: self._connect_thread_finished(t))
            self._connect_thread = thread
            thread.start()
            started = True
        finally:
            if not started:
                self._connecting = False

    def _connect_thread_finished(self, thread):
        """ConnectThread 真正退出后才丢引用（done 时线程可能还在读基站版本）。"""
        if self._connect_thread is thread:
            self._connect_thread = None
        thread.deleteLater()

    def _connect_done(self, ok, ip, info):
        """后台连接结束（UI 线程）：更新状态/保存设备/读参数路径。"""
        self._connecting = False
        self._connect_ip = ""
        self.connect_btn.setEnabled(True)
        self.device_combo.setEnabled(True)
        self.connectingChanged.emit(False, "")
        info = info or {}
        if not ok:
            self._connected = False
            self._connected_ip = ""
            self._device_info = {}
            self._update_info_label()
            self.disconnect_btn.setEnabled(False)
            msg = str(info.get("error") or "").strip()
            log.info(f"[连接] {ip} 失败: {msg or '未知原因'}")
            self.connectionChanged.emit(False, "")
            self.connect_finished.emit(False, ip, msg)
            return
        self._connected = True
        self._connected_ip = ip
        self._remember_ip(ip)
        self.disconnect_btn.setEnabled(True)
        # ★ 换设备后清掉上一台机器的曲线历史/表格值（用户 2026-11-09）
        self._last_snapshot = {"ok": {}, "failed": {}, "ts": 0}
        self.curve_view.clear_history()
        self._rebuild_table()
        model = str(info.get("model") or "").strip()
        sn = str(info.get("sn") or "").strip()
        old = next((d for d in self._known_devices() if d.get("ip") == ip), {})
        model = model or old.get("model", "")
        sn = sn or old.get("sn", "")
        if model or sn:
            dev = {"model": model, "sn": sn, "ip": ip, "last_seen": time.time()}
            merged = {d.get("ip"): d for d in self._known_devices()}
            merged[ip] = dev
            self._save_devices(list(merged.values()))
            self._add_device_item(dev, select=True)
        self._device_info = info
        self._device_info["model"] = model
        self._update_info_label()
        self._set_paths_for_tab()
        self.connectionChanged.emit(True, f"{model} · {ip}" if model else ip)
        self.connect_finished.emit(True, ip, "")

    def _info_ready(self, ip, extra):
        """后台补到的补充信息（基站版本）：只更新当前连接的那台。"""
        if not self._connected or ip != self._connected_ip:
            return
        self._device_info.update(extra or {})
        self._update_info_label()

    def robot_info(self):
        """给测试报告用：型号 / SN / 固件版本 / 基站版本。

        读不到的项返回空串（报告里保持「待获取」）。只访问 SDK，不碰控件，
        因此可以在执行线程里调用。
        """
        empty = {"model": "", "sn": "", "firmware_version": "",
                 "base_station_version": ""}
        if not (self._connected and self._service is not None):
            return empty
        # ★ 连接时已经读过型号/SN/固件/基站版本, 直接用缓存 —— 推送流开着的时候
        #   不能再走 librpc_base 读文件(基站版本), 否则会卡十几秒甚至失败。
        info = dict(empty)
        cached = dict(getattr(self, "_device_info", {}) or {})
        for key in info:
            info[key] = str(cached.get(key) or "").strip()
        stream_on = bool(getattr(getattr(self._service, "stream", None),
                                "running", False))
        need_info = not (info["sn"] and info["firmware_version"])
        # 基站版本要读机器人文件(走 librpc_base)。推送流开着时不能再走后一套
        # 客户端，只能用连接时缓存的那份。
        need_bs = not info["base_station_version"] and not stream_on
        if need_info or need_bs:
            try:
                fresh = read_robot_info(self._service,
                                        include_base_station=not stream_on)
            except Exception:
                fresh = {}
            for key in ("model", "sn", "firmware_version"):
                info[key] = info[key] or str(fresh.get(key) or "").strip()
            if not stream_on:
                info["base_station_version"] = (
                    info["base_station_version"]
                    or str(fresh.get("base_station_version") or "").strip())
        return info

    def _update_info_label(self):
        info = self._device_info or {}

        def show(key):
            value = str(info.get(key) or "").strip()
            return value or "—"

        self.info_label.setText(
            f"SN {show('sn')}    ·    固件版本 {show('firmware_version')}"
            f"    ·    基站版本 {show('base_station_version')}")

    def disconnect_device(self):
        """断开连接（后台线程；ParamService.stop 最多 join 5 秒，不能卡 UI）。"""
        if self._connecting or self._disconnecting:
            return
        # 连接线程可能还在补读基站版本（会临时停/开推送流）: 让它尽快收尾
        if self._connect_thread is not None and self._connect_thread.isRunning():
            self._connect_thread.cancel()
        self._connected = False
        self._connected_ip = ""
        self._device_info = {}
        self._update_info_label()
        self.disconnect_btn.setEnabled(False)
        self.connect_btn.setEnabled(False)
        if self._service is None:
            self._disconnect_done()
            return
        self._disconnecting = True
        thread = DisconnectThread(self._service, self)
        thread.done.connect(self._disconnect_done)
        thread.finished.connect(thread.deleteLater)
        self._disconnect_thread = thread
        thread.start()

    def _disconnect_done(self):
        self._disconnecting = False
        self._disconnect_thread = None
        self.connect_btn.setEnabled(True)
        self.disconnect_btn.setEnabled(False)
        self.connectionChanged.emit(False, "")

    # ── 数据刷新 ──
    @property
    def connected(self):
        return bool(self._connected and self._service is not None)

    @property
    def connecting(self):
        return bool(self._connecting)

    @property
    def connected_ip(self):
        return self._connected_ip if self._connected else ""

    def connect_to(self, ip):
        """切换到指定 IP 并发起连接（后台线程）。

        返回 True = 已连接或已开始连接；False = 没发起（无 IP / 正在切换）。
        连接结果通过 connect_finished 通知。
        """
        ip = str(ip or "").strip()
        if not ip:
            return False
        if self.connected and ip == self._connected_ip:
            return True
        if self._connecting or self._disconnecting:
            return False
        if not self._select_ip(ip):
            self.device_combo.setEditText(ip)
        self.connect_device()
        return bool(self._connecting)

    def battery_reader(self):
        """给 BatteryMonitor 用：已连接时返回电量/电压/温度，否则 -1（回退 OCR）。"""
        if not (self._connected and self._service is not None):
            return -1
        try:
            data = self._service.read_once([
                "/BatteryBattery/BatteryLevel",
                "/BatteryBattery/BatteryVol",
                "/BatteryBattery/BatteryTemp",
            ])
            level = data.get("/BatteryBattery/BatteryLevel")
            if level is None:
                return -1
            vol = data.get("/BatteryBattery/BatteryVol")
            temp = data.get("/BatteryBattery/BatteryTemp")
            return {
                "battery": int(level),
                # 参数里是 mV，界面/报告统一用 V
                "voltage_v": (float(vol) / 1000.0) if vol not in (None, "") else None,
                "temp_c": float(temp) if temp not in (None, "") else None,
                "source": "param",
            }
        except Exception:
            return -1

    def snapshot_provider(self):
        """失败时由 RunWorker 调用，返回最近一次参数快照。"""
        if self._connected and self._service is not None:
            try:
                return self._service.latest()
            except Exception:
                return None
        return None

    def _poll(self):
        if not self._connected or self._service is None:
            return
        try:
            snap = self._service.latest()
        except Exception:
            return
        # ★ 快照没更新就什么都不做: 定时器 200ms 跑一次, 而参数 1~2 秒才刷新,
        #   不跳过会反复给 100 行 × 3 列赋同样的值 + resizeRowsToContents(卡顿)。
        if snap.get("ts") and snap.get("ts") == self._last_snapshot.get("ts"):
            return
        self._apply_snapshot(snap)
        self.curve_view.update_snapshot(snap)

    def _apply_snapshot(self, snap):
        # ★ 整批读取失败(无线抖动/机器人忙): 保留上一次的值, 不把整页刷成"读取失败"。
        #   单条参数失败仍按失败显示。
        if (not snap.get("ok") and snap.get("failed")
                and self._last_snapshot.get("ok")):
            return
        self._last_snapshot = snap
        if not self._rows:
            return
        ts = snap.get("ts") or 0
        stamp = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else ""
        failed = snap.get("failed") or {}
        changed = False
        for group, item, row in self._rows:
            text = catalog.display_value(item, snap)
            cell = self.table.item(row, 1)
            if cell is None:
                cell = QTableWidgetItem()
                self.table.setItem(row, 1, cell)
            is_failed = item["path"] in failed
            optional = bool(item.get("optional"))
            if is_failed and optional:
                new_text = "—"
                cell.setForeground(QColor("#94a3b8"))
                cell.setToolTip("本机型未返回该参数")
            else:
                new_text = text or ("读取失败" if is_failed else "—")
                cell.setForeground(QColor(
                    "#dc2626" if (is_failed or not text) else "#1e293b"))
                # 摘要类参数(地检/点激光对象)在表格里只显示前两项，完整内容看 tooltip
                tip = text
                if item.get("field") == "summary":
                    raw = (snap.get("ok") or {}).get(item["path"])
                    if raw is not None:
                        try:
                            tip = (json.dumps(raw, ensure_ascii=False, indent=2,
                                              default=str)
                                   if isinstance(raw, (dict, list)) else str(raw))
                        except Exception:
                            tip = str(raw)
                cell.setToolTip(tip)
            if cell.text() != new_text:
                cell.setText(new_text)
                changed = True
            hidden = self.only_changed.isChecked() and not text
            if self.table.isRowHidden(row) != hidden:
                self.table.setRowHidden(row, hidden)
                changed = True
            stamp_cell = self.table.item(row, 3)
            if stamp_cell is None:
                stamp_cell = QTableWidgetItem()
                self.table.setItem(row, 3, stamp_cell)
            stamp_cell.setText(stamp)
        # 行高只跟参数名/值(第 0/1 列)有关, 时间列变化不需要重算
        if changed:
            self.table.resizeRowsToContents()

    def _rebuild_table(self, *_):
        query = self.search.text().strip().lower()
        cat = self.cat_list.currentRow() - 1 if self.cat_list.currentRow() > 0 else -1
        rows = []
        for gi, group in enumerate(catalog.PARAM_GROUPS):
            if cat >= 0 and gi != cat:
                continue
            items = [it for it in group["items"]
                     if not query or query in it["name"].lower()
                     or query in it["path"].lower()]
            if not items:
                continue
            rows.append((group, None, items))
        total = sum(len(x[2]) for x in rows)
        self.count_label.setText(f"共 {total} 项")
        self.table.setRowCount(0)
        self._rows = []
        r = 0
        for group, _none, items in rows:
            self.table.insertRow(r)
            hdr = QTableWidgetItem(f'{group["name"]}')
            hdr.setBackground(QColor("#f8fafc"))
            hdr.setForeground(QColor("#475569"))
            self.table.setSpan(r, 0, 1, 4)
            self.table.setItem(r, 0, hdr)
            r += 1
            for item in items:
                self.table.insertRow(r)
                name_cell = QTableWidgetItem(catalog.item_label(item))
                name_cell.setToolTip(item["path"])
                self.table.setItem(r, 0, name_cell)
                self.table.setItem(r, 1, QTableWidgetItem("—"))
                self.table.setItem(r, 2, QTableWidgetItem(item.get("unit", "")))
                self.table.setItem(r, 3, QTableWidgetItem(""))
                self._rows.append((group, item, r))
                r += 1
        if self._last_snapshot.get("ts"):
            self._apply_snapshot(self._last_snapshot)

    # ── 页签 / 路径 ──
    def _tab_changed(self, index):
        self._current_page = index
        self._set_paths_for_tab()

    def _set_paths_for_tab(self):
        if self._service is None or not self._connected:
            return
        if self._current_page == 1:
            self._service.set_paths(self._apos_paths(self.curve_view.current_paths()),
                                    interval=1.0 / max(1.0, self._curve_hz()))
        else:
            # 表格页也采曲线路径：连上即开始积累历史，切到曲线页不用从零等
            self._service.set_paths(catalog.all_monitor_paths(),
                                    interval=self._table_interval())

    @staticmethod
    def _apos_paths(paths):
        """MCU 反馈来自 7809 调试流，不能当作 APOS 属性去读。"""
        return [p for p in paths if not catalog.is_mcu_path(p)]

    def _curve_paths_changed(self, paths):
        if self._service is not None and self._connected and self._current_page == 1:
            self._service.set_paths(self._apos_paths(paths),
                                    interval=1.0 / max(1.0, self._curve_hz()))

    def _interval_changed(self, *_):
        self.settings.setValue("apos/table_interval", self._table_interval())
        if self._current_page == 0:
            self._set_paths_for_tab()

    def showEvent(self, ev):
        super().showEvent(ev)
        if not self._auto_scan_done and not _is_pytest():
            self._auto_scan_done = True
            QTimer.singleShot(200, self.scan)
        # 首次进入时检查 SDK（主窗口启动时也会检查一次）
        if not _is_pytest():
            self._refresh_sdk()

    def shutdown(self):
        self._timer.stop()
        # 先取消连接线程（正在补读基站版本时最多还要几秒），再等它退出。
        # ★ 不能只等 3 秒就跳过: 线程 parent 是本页, 窗口析构时销毁运行中的
        #   QThread 会直接 qFatal。
        t = self._connect_thread
        if t is not None and t.isRunning():
            try:
                t.cancel()
            except Exception:
                pass
        for attr in ("_connect_thread", "_disconnect_thread"):
            t = getattr(self, attr, None)
            if t is not None and t.isRunning():
                t.wait(6000)          # 关窗: 等后台连接/断开收尾
        if self._service is not None:
            try:
                self._service.disconnect()
            except Exception:
                pass
        if self._scan_thread and self._scan_thread.isRunning():
            self._scan_thread.cancel()
            self._scan_thread.wait(2000)
