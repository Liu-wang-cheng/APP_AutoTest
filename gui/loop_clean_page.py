# -*- coding: utf-8 -*-
"""循环清扫页：多组合轮换，清扫到下限电量回充，充到目标电量继续。

逻辑在 core.loop_clean；页面只做参数收集、线程调度、实时状态和日志。
"""
import json
import os
import re
import threading
import time

from PySide6.QtCore import (QEvent, QObject, QSettings, Qt, QThread,
                            QUrl, Signal)
from PySide6.QtGui import QColor, QDesktopServices, QPalette
from PySide6.QtWidgets import (QAbstractItemView, QFrame,
                               QGridLayout, QHBoxLayout, QHeaderView,
                               QLabel, QMessageBox, QPlainTextEdit,
                               QPushButton, QSpinBox, QTableWidget,
                               QTableWidgetItem,
                               QVBoxLayout, QWidget)

from core.driver import BASE_DIR, REPORT_SUBDIRS
from core.endurance import _acquire_lock, _release_lock
from core.loop_clean import run_loop_clean
from gui.widgets import NoWheelComboBox

MODE_ITEMS = (
    ("仅扫", "sweepOnly"),
    ("扫拖", "sweepMop"),
    ("单拖", "mopOnly"),
    ("先扫后拖", "sweepThenMop"),
)


class LoopWorker(QThread):
    log_line = Signal(str)
    state = Signal(dict)
    finished_run = Signal(bool, str, str)      # 完成/原因/结果目录

    def __init__(self, ip, combos, clean_battery, recharge_battery,
                 max_rounds, sample_interval, parent=None, peer=None):
        super().__init__(parent)
        self._ip = ip
        self._combos = list(combos)
        self._clean = clean_battery
        self._recharge = recharge_battery
        self._max = max_rounds
        self._interval = sample_interval
        self._peer = peer
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        lock = None
        peer_paused = False
        try:
            lock = _acquire_lock()
            if self._peer is not None:
                try:
                    peer_paused = bool(self._peer.pause_param_stream())
                except Exception:
                    peer_paused = False
            result, base = run_loop_clean(
                self._ip, self._combos, clean_battery=self._clean,
                recharge_battery=self._recharge, max_rounds=self._max,
                sample_interval=self._interval,
                log_fn=self.log_line.emit, stop_event=self._stop,
                on_state=self.state.emit)
            rounds = len(result.get("rounds") or [])
            self.finished_run.emit(
                bool(result.get("completed")),
                "%d 轮，%s" % (rounds, result.get("end_reason", "")), base)
        except Exception as e:
            self.finished_run.emit(False, "启动失败: %s" % e, "")
        finally:
            if peer_paused:
                try:
                    self._peer.resume_param_stream()
                except Exception:
                    pass
            if lock is not None:
                try:
                    _release_lock(lock)
                except Exception:
                    pass


class LoopCleanPage(QWidget):
    runningChanged = Signal(bool)

    def __init__(self, device=None, parent=None, settings=None):
        super().__init__(parent)
        self.device = device
        self.worker = None
        self._result_dir = ""
        self._select_all_next = True
        self._auto_ip = ""          # 自动填入的默认 IP(用户手动改过就不再跟随)
        self.settings = settings
        self._persist = (settings is not None
                         or not os.environ.get("PYTEST_CURRENT_TEST"))
        if self.settings is None and self._persist:
            self.settings = QSettings("vacuum_test", "case_studio")
        self._loading = True
        self._build_ui()
        self._restore_defaults()
        if self._persist:
            self._load_settings()
        self._loading = False
        self._refresh_ips()
        self._refresh_conn()
        if device is not None:
            try:
                device.devicesChanged.connect(self._refresh_ips)
                device.connectionChanged.connect(self._on_conn_changed)
                device.connectingChanged.connect(self._on_connecting)
            except Exception:
                pass

    # ── UI ──
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 14)
        root.setSpacing(10)
        head = QHBoxLayout()
        head.setSpacing(12)
        title = QLabel("循环清扫")
        title.setStyleSheet("font-size:16px;font-weight:700;color:#1e293b;")
        head.addWidget(title)
        tip = QLabel("按组合轮换：电量 ≥ 清扫电量开始清扫，< 回充电量回充；"
                     "次数 0 表示无限循环。")
        tip.setStyleSheet("color:#94a3b8;font-size:11px;")
        head.addWidget(tip)
        head.addStretch(1)
        root.addLayout(head)

        ip_row = QHBoxLayout()
        ip_row.addWidget(QLabel("设备IP"))
        self.ip_combo = NoWheelComboBox()
        self.ip_combo.setEditable(True)
        self.ip_combo.setMinimumWidth(220)
        self.ip_combo.setPlaceholderText("机器人 IP")
        self.ip_combo.currentTextChanged.connect(
            lambda *_: self._sync_conn_ui())
        ip_row.addWidget(self.ip_combo)
        self.connect_btn = QPushButton("连接")
        self.connect_btn.setObjectName("runBtn")
        self.connect_btn.clicked.connect(self._connect)
        self.disconnect_btn = QPushButton("断开")
        self.disconnect_btn.setObjectName("chipBtn")
        self.disconnect_btn.clicked.connect(self._disconnect)
        ip_row.addWidget(self.connect_btn)
        ip_row.addWidget(self.disconnect_btn)
        self.conn_label = QLabel("● 未连接")
        self.conn_label.setStyleSheet("color:#94a3b8;font-size:11px;")
        ip_row.addWidget(self.conn_label)
        ip_row.addStretch(1)
        root.addLayout(ip_row)

        opt_row = QHBoxLayout()
        opt_row.setSpacing(8)
        self.low_spin = QSpinBox()
        self.low_spin.setRange(10, 100)
        self.low_spin.setValue(30)
        self.low_spin.setSuffix("%")
        self.target_spin = QSpinBox()
        self.target_spin.setRange(5, 95)
        self.target_spin.setValue(20)
        self.target_spin.setSuffix("%")
        self.rounds_spin = QSpinBox()
        self.rounds_spin.setRange(0, 999)
        self.rounds_spin.setValue(0)
        self.rounds_spin.setToolTip("0 = 无限循环，直到手动停止")
        self.sample_spin = QSpinBox()
        self.sample_spin.setRange(5, 120)
        self.sample_spin.setValue(15)
        self.sample_spin.setSuffix(" 秒")
        for spin in (self.low_spin, self.target_spin, self.rounds_spin,
                     self.sample_spin):
            spin.valueChanged.connect(lambda *_: self._save_settings())
        opt_row.addWidget(QLabel("清扫电量"))
        opt_row.addWidget(self.low_spin)
        opt_row.addSpacing(14)
        opt_row.addWidget(QLabel("回充电量"))
        opt_row.addWidget(self.target_spin)
        opt_row.addSpacing(14)
        opt_row.addWidget(QLabel("清扫次数"))
        opt_row.addWidget(self.rounds_spin)
        opt_row.addSpacing(14)
        opt_row.addWidget(QLabel("采样"))
        opt_row.addWidget(self.sample_spin)
        opt_row.addStretch(1)
        root.addLayout(opt_row)

        combo_bar = QHBoxLayout()
        combo_bar.addWidget(QLabel("测试组合"))
        self.all_btn = QPushButton("全选")
        self.all_btn.setObjectName("chipBtn")
        self.all_btn.clicked.connect(self._toggle_select)
        add_btn = QPushButton("添加组合")
        add_btn.setObjectName("chipBtn")
        add_btn.clicked.connect(lambda: self._add_row())
        del_btn = QPushButton("删除选中")
        del_btn.setObjectName("chipBtn")
        del_btn.clicked.connect(self._delete_selected)
        reset_btn = QPushButton("恢复默认")
        reset_btn.setObjectName("chipBtn")
        reset_btn.clicked.connect(self._restore_defaults)
        combo_bar.addWidget(self.all_btn)
        combo_bar.addWidget(add_btn)
        combo_bar.addWidget(del_btn)
        combo_bar.addStretch(1)
        combo_bar.addWidget(reset_btn)
        root.addLayout(combo_bar)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["启用", "模式", "吸力", "水量"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.Stretch)
        h.setSectionResizeMode(2, QHeaderView.Fixed)
        h.setSectionResizeMode(3, QHeaderView.Fixed)
        self.table.setColumnWidth(2, 120)
        self.table.setColumnWidth(3, 120)
        # 选中行不要蓝底(与灯显语音 PRD 表格一致)
        pal = self.table.palette()
        for role in (QPalette.Active, QPalette.Inactive, QPalette.Disabled):
            pal.setColor(role, QPalette.Highlight, QColor("#ffffff"))
            pal.setColor(role, QPalette.HighlightedText, QColor("#1e293b"))
        self.table.setPalette(pal)
        self.table.setStyleSheet(
            "QTableWidget{outline:0;}"
            "QTableWidget::item:selected{background:#ffffff;color:#1e293b;}"
            "QTableWidget::item:focus{outline:none;border:none;}")
        self.table.verticalHeader().setDefaultSectionSize(36)
        root.addWidget(self.table, 1)

        self.table.itemChanged.connect(lambda *_: self._save_settings())
        run_row = QHBoxLayout()
        self.start_btn = QPushButton("开始循环清扫")
        self.start_btn.setObjectName("runBtn")
        self.start_btn.clicked.connect(self.start)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setObjectName("stopBtn")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop)
        self.state_label = QLabel("● 空闲")
        self.state_label.setStyleSheet("color:#475569;font-size:12px;")
        run_row.addWidget(self.start_btn)
        run_row.addWidget(self.stop_btn)
        run_row.addWidget(self.state_label)
        run_row.addStretch(1)
        self.open_dir_btn = QPushButton("打开结果目录")
        self.open_dir_btn.setObjectName("enduranceActionBtn")
        self.open_dir_btn.clicked.connect(self._open_dir)
        run_row.addWidget(self.open_dir_btn)
        root.addLayout(run_row)

        card = QFrame()
        card.setObjectName("chipStrip")
        grid = QGridLayout(card)
        grid.setContentsMargins(12, 6, 12, 6)
        grid.setHorizontalSpacing(20)
        grid.setVerticalSpacing(6)
        self.metrics = {}
        for i, (key, label) in enumerate((
                ("combo", "当前组合"), ("battery", "电量"),
                ("status", "状态"), ("round", "当前轮次"),
                ("rounds", "已完成轮次"), ("round_time", "本轮时长"),
                ("errors", "报错次数"))):
            cap = QLabel(label)
            cap.setStyleSheet("color:#94a3b8;font-size:11px;")
            val = QLabel("—")
            val.setStyleSheet("color:#1e293b;font-size:13px;"
                              "font-weight:600;")
            grid.addWidget(cap, 0, i)
            grid.addWidget(val, 1, i)
            self.metrics[key] = val
        root.addWidget(card)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("运行日志")
        self.log.setMaximumBlockCount(5000)
        root.addWidget(self.log, 1)

    # ── 组合表 ──
    #: 循环清扫默认组合：仅扫1档 + 扫拖1档(水量1)，默认不勾选
    LOOP_DEFAULT_COMBOS = (("sweepOnly", 1, 0), ("sweepMop", 1, 1))

    def _restore_defaults(self):
        self.table.setRowCount(0)
        for mode, fan, water in self.LOOP_DEFAULT_COMBOS:
            self._add_row(mode, fan, water, checked=False)
        self._select_all_next = True
        if hasattr(self, "all_btn"):
            self.all_btn.setText("全选")
        self._save_settings()

    def _add_row(self, mode="sweepOnly", fan=4, water=0, checked=False):
        r = self.table.rowCount()
        self.table.insertRow(r)
        # ★ 原生 item 勾选(与自动化用例列表同一效果/同一 Qt 管线):
        #   不塞 QCheckBox 控件, 避免行内控件与选中高亮层交替重绘导致闪动。
        item = QTableWidgetItem()
        item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable
                      | Qt.ItemIsUserCheckable)
        item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        self.table.setItem(r, 0, item)
        mode_combo = NoWheelComboBox()
        for label, value in MODE_ITEMS:
            mode_combo.addItem(label, value)
        idx = mode_combo.findData(mode)
        mode_combo.setCurrentIndex(max(0, idx))
        self.table.setCellWidget(r, 1, mode_combo)
        fan_combo = self._level_combo(
            [("关", 0), ("1 档", 1), ("2 档", 2), ("3 档", 3),
             ("4 档", 4), ("5 档", 5)], int(fan))
        self.table.setCellWidget(r, 2, fan_combo)
        water_combo = self._level_combo(
            [("关", 0), ("1 档", 1), ("2 档", 2), ("3 档", 3),
             ("4 档", 4)], int(water))
        self.table.setCellWidget(r, 3, water_combo)

        def _sync(_=None, c=mode_combo, w=water_combo):
            w.setEnabled(c.currentData() != "sweepOnly")
        mode_combo.currentIndexChanged.connect(_sync)
        _sync()
        mode_combo.currentIndexChanged.connect(
            lambda *_: self._save_settings())
        fan_combo.currentIndexChanged.connect(
            lambda *_: self._save_settings())
        water_combo.currentIndexChanged.connect(
            lambda *_: self._save_settings())

    @staticmethod
    def _level_combo(items, current):
        combo = NoWheelComboBox()
        combo.setEditable(False)
        for label, value in items:
            combo.addItem(label, value)
        idx = combo.findData(int(current))
        combo.setCurrentIndex(max(0, idx))
        return combo

    def _row_checked(self, row):
        item = self.table.item(row, 0)
        return bool(item is not None and item.checkState() == Qt.Checked)

    def _set_row_checked(self, row, checked):
        item = self.table.item(row, 0)
        if item is not None:
            item.setCheckState(Qt.Checked if checked else Qt.Unchecked)

    def _row_values(self, row):
        mode = self.table.cellWidget(row, 1).currentData()
        fan = self.table.cellWidget(row, 2).currentData()
        water = self.table.cellWidget(row, 3).currentData()
        return self._row_checked(row), mode, int(fan), int(water)

    def _combos_snapshot(self):
        rows = []
        for r in range(self.table.rowCount()):
            enabled, mode, fan, water = self._row_values(r)
            rows.append({"enabled": enabled, "mode": mode,
                         "fan": fan, "water": water})
        return rows

    def _save_settings(self):
        if getattr(self, "_loading", False) or not self._persist:
            return
        try:
            self.settings.setValue(
                "loop/combos",
                json.dumps(self._combos_snapshot(), ensure_ascii=False))
            self.settings.setValue("loop/clean_battery",
                                   self.low_spin.value())
            self.settings.setValue("loop/recharge_battery",
                                   self.target_spin.value())
            self.settings.setValue("loop/max_rounds",
                                   self.rounds_spin.value())
            self.settings.setValue("loop/sample_interval",
                                   self.sample_spin.value())
        except Exception:
            pass

    def _load_settings(self):
        try:
            raw = self.settings.value("loop/combos")
        except Exception:
            raw = None
        rows = None
        if raw:
            try:
                rows = json.loads(raw)
            except Exception:
                rows = None
        modes = {value for _label, value in MODE_ITEMS}
        if isinstance(rows, list):
            self.table.setRowCount(0)
            for row in rows:
                if not isinstance(row, dict) or row.get("mode") not in modes:
                    continue
                try:
                    fan = int(row.get("fan", 0))
                    water = int(row.get("water", 0))
                except (TypeError, ValueError):
                    continue
                if not (0 <= fan <= 5 and 0 <= water <= 4):
                    continue
                self._add_row(row.get("mode"), fan, water,
                              checked=bool(row.get("enabled")))
            self._select_all_next = True
            if hasattr(self, "all_btn"):
                self.all_btn.setText("全选")
        for key, spin in (("loop/clean_battery", self.low_spin),
                          ("loop/recharge_battery", self.target_spin),
                          ("loop/max_rounds", self.rounds_spin),
                          ("loop/sample_interval", self.sample_spin)):
            try:
                v = int(self.settings.value(key))
            except (TypeError, ValueError):
                continue
            spin.setValue(v)

    def _collect_combos(self):
        out = []
        for r in range(self.table.rowCount()):
            enabled, mode, fan, water = self._row_values(r)
            if not enabled:
                continue
            if mode == "sweepOnly" and water:
                water = 0
            out.append((mode, fan, water))
        return out

    def _toggle_select(self):
        if self._select_all_next:
            self._check_all()
            self.all_btn.setText("反选")
        else:
            self._invert_checks()
            self.all_btn.setText("全选")
        self._select_all_next = not self._select_all_next
        self._save_settings()

    def _check_all(self):
        for r in range(self.table.rowCount()):
            self._set_row_checked(r, True)

    def _invert_checks(self):
        for r in range(self.table.rowCount()):
            self._set_row_checked(r, not self._row_checked(r))

    def _delete_selected(self):
        for r in range(self.table.rowCount() - 1, -1, -1):
            enabled, _m, _f, _w = self._row_values(r)
            if enabled:
                self.table.removeRow(r)
        self._save_settings()

    # ── 连接 ──
    def _refresh_ips(self):
        current = self.ip_combo.currentText().strip()
        devs, connected_ip, info = [], "", {}
        if self.device is not None:
            try:
                devs = list(self.device._known_devices())
            except Exception:
                devs = []
            try:
                connected_ip = str(self.device.connected_ip or "")
            except Exception:
                connected_ip = ""
            try:
                info = getattr(self.device, "_device_info", None) or {}
            except Exception:
                info = {}
        self.ip_combo.blockSignals(True)
        self.ip_combo.clear()
        seen = set()
        for dev in devs:
            ip = str(dev.get("ip") or "").strip()
            if not ip or ip in seen:
                continue
            seen.add(ip)
            model = dev.get("model") or (info.get("model", "")
                                         if ip == connected_ip else "")
            sn = dev.get("sn") or (info.get("sn", "")
                                   if ip == connected_ip else "")
            label = "-".join(x for x in (model, sn, ip) if x)
            self.ip_combo.addItem(label, ip)
        if connected_ip and connected_ip not in seen:
            label = "-".join(x for x in (info.get("model", ""),
                                         info.get("sn", ""),
                                         connected_ip) if x)
            self.ip_combo.addItem(label, connected_ip)
        labels = {self.ip_combo.itemText(i): self.ip_combo.itemData(i)
                  for i in range(self.ip_combo.count())}
        cur_is_item = bool(current and labels.get(current))
        # ★ 自动填入的默认 IP 不算"用户选择": 工具连上设备后要跟随过去,
        #   否则新用户会一直用 192.168.10.21 去订阅推流(用户 2026-11-27)。
        is_auto = (not current) or (current == self._auto_ip)
        prefer = connected_ip if (connected_ip and (
            is_auto or current == connected_ip or cur_is_item)) else ""
        if not prefer and is_auto:
            # 还没连接时，优先用设备页当前输入/上次使用的 IP，而不是写死 10.21
            try:
                prefer = str(getattr(self.device, "_connect_ip", "") or
                             self.device._selected_ip() or "")
            except Exception:
                prefer = ""
        target = prefer or current
        idx = self.ip_combo.findData(target)
        if idx < 0:
            idx = self.ip_combo.findText(target)
        if idx >= 0:
            self.ip_combo.setCurrentIndex(idx)
        elif target:
            self.ip_combo.setEditText(target)
        elif self.ip_combo.count():
            self.ip_combo.setCurrentIndex(0)
        else:
            self.ip_combo.setEditText("192.168.10.21")
        self._auto_ip = self.ip_combo.currentText().strip()
        self.ip_combo.blockSignals(False)
        self._fit_ip_combo()

    def _fit_ip_combo(self):
        """下拉宽度按最长「型号-SN-IP」自适应，避免显示不全。"""
        fm = self.ip_combo.fontMetrics()
        texts = [self.ip_combo.itemText(i)
                 for i in range(self.ip_combo.count())]
        cur = self.ip_combo.currentText().strip()
        if cur:
            texts.append(cur)
        longest = max((fm.horizontalAdvance(t) for t in texts), default=0)
        width = max(240, min(longest + 56, 520))
        if self.ip_combo.width() != width:
            self.ip_combo.setFixedWidth(width)

    def _ip(self):
        text = self.ip_combo.currentText().strip()
        idx = self.ip_combo.currentIndex()
        if idx >= 0 and text == self.ip_combo.itemText(idx):
            data = self.ip_combo.itemData(idx)
            if data:
                return str(data)
        m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", text)
        return m.group(1) if m else text

    def _connect(self):
        ip = self._ip()
        if not ip or self.device is None:
            return
        if (getattr(self.device, "connected", False)
                and ip == (getattr(self.device, "connected_ip", "") or "")):
            self._append("[连接] 已连接 %s，无需重复连接" % ip)
            self._sync_conn_ui()
            return
        try:
            started = self.device.connect_to(ip)
        except Exception as e:
            self._append("[连接] 失败: %s" % e)
            return
        if not started:
            QMessageBox.warning(self, "连接扫地机失败",
                                "无法连接 %s，请检查 IP 和网络。" % ip)
        else:
            self._append("[连接] 正在连接 %s…" % ip)
        self._sync_conn_ui()

    def _disconnect(self):
        if self.device is None:
            return
        try:
            self.device.disconnect_device()
        except Exception as e:
            self._append("[断开] 失败: %s" % e)
        self._sync_conn_ui()

    def _on_conn_changed(self, connected, label):
        if connected:
            self._refresh_ips()
            self.conn_label.setText("● 已连接" + (" · " + label if label
                                                  else ""))
            self.conn_label.setStyleSheet("color:#16a34a;font-size:11px;")
        else:
            self._refresh_conn()
        self._sync_conn_ui()

    def _on_connecting(self, connecting, ip):
        if connecting:
            self.conn_label.setText("● 连接中" + (" · " + ip if ip else ""))
            self.conn_label.setStyleSheet("color:#d97706;font-size:11px;")
        self._sync_conn_ui()

    def _refresh_conn(self):
        connected = getattr(self.device, "connected", False)
        label = getattr(self.device, "connected_ip", "") if connected else ""
        if connected:
            self.conn_label.setText("● 已连接" + (" · " + label if label
                                                  else ""))
            self.conn_label.setStyleSheet("color:#16a34a;font-size:11px;")
        else:
            self.conn_label.setText("● 未连接")
            self.conn_label.setStyleSheet("color:#94a3b8;font-size:11px;")
        self._sync_conn_ui()

    def _sync_conn_ui(self):
        connecting = bool(getattr(self.device, "connecting", False))
        connected = bool(getattr(self.device, "connected", False))
        same = (connected
                and self._ip() == (getattr(self.device, "connected_ip", "")
                                   or ""))
        text = "连接中…" if connecting else ("已连接" if same else "连接")
        if self.connect_btn.text() != text:
            self.connect_btn.setText(text)
        self.connect_btn.setEnabled(not connecting)
        self.disconnect_btn.setEnabled(connected and not connecting)

    # ── 运行 ──
    def is_running(self):
        return self.worker is not None and self.worker.isRunning()

    def start(self):
        if self.is_running():
            return
        ip = self._ip()
        if not ip:
            QMessageBox.warning(self, "未填写 IP",
                                "请先填写或选择扫地机 IP。")
            return
        clean_battery = self.low_spin.value()
        recharge_battery = self.target_spin.value()
        if clean_battery <= recharge_battery:
            QMessageBox.warning(
                self, "电量参数不合理",
                "清扫电量必须大于回充电量。")
            return
        combos = self._collect_combos()
        if not combos:
            QMessageBox.warning(self, "没有测试组合",
                                "至少启用一个清扫组合。")
            return
        if self.device is not None and not getattr(self.device, "connected",
                                                   False):
            ok = QMessageBox.question(
                self, "未连接扫地机",
                "当前还没有连接扫地机（只填了 IP）。\n"
                "开始后会自行尝试连接该 IP，是否继续？")
            if ok != QMessageBox.Yes:
                return
        if self.device is not None:
            try:
                self.device._remember_ip(ip)   # 下次启动保留本次测试 IP
            except Exception:
                pass
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.state_label.setText("● 启动中…")
        self.state_label.setStyleSheet("color:#2563eb;font-size:12px;")
        self._append("[启动] %s，%d 个组合，清扫电量 %d%%，回充电量 %d%%，"
                     "次数 %d" % (ip, len(combos), clean_battery,
                                  recharge_battery,
                                  self.rounds_spin.value()))
        self.worker = LoopWorker(
            ip, combos, clean_battery, recharge_battery,
            self.rounds_spin.value(),
            float(self.sample_spin.value()), self, peer=self.device)
        self.worker.log_line.connect(self._append)
        self.worker.state.connect(self._on_state)
        self.worker.finished_run.connect(self._on_finished)
        self.worker.start()
        self.runningChanged.emit(True)

    def stop(self):
        if self.is_running():
            self._append("[停止] 正在结束循环清扫…")
            self.stop_btn.setEnabled(False)
            self.state_label.setText("● 停止中…")
            self.state_label.setStyleSheet("color:#d97706;font-size:12px;")
            self.worker.stop()

    def _append(self, text):
        self.log.appendPlainText(
            "[%s] %s" % (time.strftime("%H:%M:%S"), str(text)))

    def _on_state(self, s):
        combo = s.get("combo")
        self.metrics["combo"].setText(
            "%s 吸力%s 水量%s" % (combo[0], combo[1], combo[2])
            if combo else "—")
        b = s.get("battery")
        self.metrics["battery"].setText(
            "%d%%" % b if isinstance(b, int) and b >= 0 else "—")
        status = str(s.get("status") or "")
        self.metrics["status"].setText(
            status.replace("work_status_", "") if status else "—")
        self.metrics["round"].setText(str(s.get("round") or "—"))
        self.metrics["rounds"].setText(str(s.get("rounds") or 0))
        self.metrics["round_time"].setText(
            _fmt_duration(s.get("round_seconds") or 0))
        self.metrics["errors"].setText(str(s.get("errors") or 0))
        if status:
            self.state_label.setText("● " + status.replace("work_status_", ""))
            color = "#16a34a" if "clean" in status else "#2563eb"
            if status == "work_status_error":
                color = "#dc2626"
            if "charging" in status or status in (
                    "work_status_dry_mop", "work_status_idle",
                    "work_status_dormant"):
                color = "#d97706"
            self.state_label.setStyleSheet(
                "color:%s;font-size:12px;" % color)

    def _on_finished(self, ok, message, base):
        self._result_dir = base or ""
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.state_label.setText("● 完成" if ok else "● 已停止")
        self.state_label.setStyleSheet(
            "color:#16a34a;font-size:12px;" if ok
            else "color:#dc2626;font-size:12px;")
        self._append("[结束] %s" % message)
        if base:
            self._append("[结果] %s" % base)
        self.worker = None
        self.runningChanged.emit(False)
        if not ok and not base:
            QMessageBox.critical(self, "循环清扫启动失败", message)

    def _open_dir(self):
        d = self._result_dir or os.path.join(
            BASE_DIR, "reports", REPORT_SUBDIRS["loop"])
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
        if os.path.isdir(d):
            QDesktopServices.openUrl(QUrl.fromLocalFile(d))

    def shutdown(self):
        if self.is_running():
            self.worker.stop()
            self.worker.wait(10000)
        self.runningChanged.emit(False)


def _fmt_duration(seconds):
    seconds = int(max(0, seconds))
    return "%02d:%02d:%02d" % (seconds // 3600, seconds % 3600 // 60,
                               seconds % 60)


class LoopCloseFilter(QObject):
    """关窗时如果循环清扫在跑，先确认并正常停止线程。"""

    def __init__(self, page, parent=None):
        super().__init__(parent)
        self.page = page

    def eventFilter(self, obj, event):
        if (event.type() == QEvent.Close and self.page.is_running()
                and obj is self.page.window()):
            if QMessageBox.question(
                    obj, "循环清扫正在运行",
                    "循环清扫正在运行，停止并退出?") != QMessageBox.Yes:
                event.ignore()
                return True
            self.page.shutdown()
        return False
