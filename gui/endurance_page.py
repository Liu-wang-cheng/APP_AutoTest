# -*- coding: utf-8 -*-
"""续航测试页：设备连接、多组合参数、启停、实时状态、报告。

页面只负责交互与线程调度；控制/采样/报告逻辑仍在 core.endurance 和
tools.endurance_report，命令行与计划任务共用同一套实现。
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

from core.endurance import (DEFAULT_COMBOS, _acquire_lock, _release_lock,
                            run_endurance_suite)
from core.driver import BASE_DIR, REPORT_SUBDIRS
from gui.widgets import NoWheelComboBox


MODE_ITEMS = (
    ("仅扫", "sweepOnly"),
    ("扫拖", "sweepMop"),
    ("单拖", "mopOnly"),
    ("先扫后拖", "sweepThenMop"),
)


class EnduranceWorker(QThread):
    log_line = Signal(str)
    state = Signal(dict)
    finished_run = Signal(bool, str, str, str)   # 完成/原因/结果目录/Excel

    def __init__(self, ip, combos, low_battery, sample_interval,
                 parent=None, peer=None, stream=None):
        super().__init__(parent)
        self._ip = ip
        self._combos = list(combos)
        self._low = low_battery
        self._interval = sample_interval
        self._peer = peer
        self._stream = stream      # 共用设备页的推送会话(可为 None)
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        lock = None
        peer_paused = False
        try:
            lock = _acquire_lock()
            # 同机双推流会互相挤掉订阅：运行期间让设备参数页暂停推送
            if self._peer is not None:
                try:
                    peer_paused = bool(self._peer.pause_param_stream())
                except Exception:
                    peer_paused = False
            entries, base, report = run_endurance_suite(
                self._ip, combos=self._combos, log_fn=self.log_line.emit,
                stop_event=self._stop, on_state=self.state.emit,
                low_battery=self._low,
                sample_interval=self._interval,
                stream=self._stream)
            ok = bool(entries) and all(e.get("completed") for e in entries)
            done = sum(1 for e in entries if e.get("completed"))
            msg = "完成 %d/%d 个组合" % (done, len(entries))
            if not ok:
                msg += "；失败: " + "；".join(
                    "%s(%s)" % (e.get("mode"), e.get("reason") or "未完成")
                    for e in entries if not e.get("completed"))
            self.finished_run.emit(ok, msg, base, report or "")
        except Exception as e:
            self.finished_run.emit(False, "启动失败: %s" % e, "", "")
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


class EnduranceCloseFilter(QObject):
    """关窗时如果续航测试在跑，先确认并正常停止线程。"""

    def __init__(self, page, parent=None):
        super().__init__(parent)
        self.page = page

    def eventFilter(self, obj, event):
        if (event.type() == QEvent.Close and self.page.is_running()
                and obj is self.page.window()):
            if QMessageBox.question(
                    obj, "续航测试正在运行",
                    "续航测试正在运行，停止并退出?") != QMessageBox.Yes:
                event.ignore()
                return True
            self.page.shutdown()
        return False


def _fmt_duration(seconds):
    seconds = int(max(0, seconds))
    return "%02d:%02d:%02d" % (seconds // 3600, seconds % 3600 // 60,
                               seconds % 60)


class EndurancePage(QWidget):
    runningChanged = Signal(bool)      # 续航运行中 → 顶部导航显示「运行中」

    def __init__(self, device=None, parent=None, settings=None):
        super().__init__(parent)
        self.device = device
        self.worker = None
        self._report = ""
        self._select_all_next = True
        # 组合/阈值持久化：记住上一次的勾选与挡位（pytest 下不读写真实配置）
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
        self._refresh_latest()

    # ── UI ──
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 14)
        root.setSpacing(10)
        # 标题与说明同一行（用户 2026-11-26）
        head = QHBoxLayout()
        head.setSpacing(12)
        title = QLabel("续航测试")
        title.setStyleSheet("font-size:16px;font-weight:700;color:#1e293b;")
        head.addWidget(title)
        tip = QLabel("满电开始清扫 → 低电回充；按组合顺序连续测试，"
                     "清扫中保持挡位、记录故障与出水，结束生成 Excel。")
        tip.setStyleSheet("color:#94a3b8;font-size:11px;")
        tip.setWordWrap(False)
        tip.setToolTip(tip.text())
        head.addWidget(tip)
        head.addStretch(1)
        root.addLayout(head)

        # ★ 扫地机IP连接统一在右上角导航条(用户 2026-11-27), 本页不再放 IP 行

        # 低电阈值 / 采样单独一行（用户 2026-11-26）：原来挤在 IP 行右侧会被窗口裁掉
        opt_row = QHBoxLayout()
        opt_row.setSpacing(8)
        self.low_spin = QSpinBox()
        self.low_spin.setRange(5, 40)
        self.low_spin.setValue(20)
        self.low_spin.setSuffix("%")
        self.sample_spin = QSpinBox()
        self.sample_spin.setRange(5, 120)
        self.sample_spin.setValue(15)
        self.sample_spin.setSuffix(" 秒")
        self.low_spin.valueChanged.connect(lambda *_: self._save_settings())
        self.sample_spin.valueChanged.connect(
            lambda *_: self._save_settings())
        opt_row.addWidget(QLabel("低电阈值"))
        opt_row.addWidget(self.low_spin)
        opt_row.addSpacing(18)
        opt_row.addWidget(QLabel("采样"))
        opt_row.addWidget(self.sample_spin)
        opt_row.addStretch(1)
        root.addLayout(opt_row)

        combo_bar = QHBoxLayout()
        combo_bar.addWidget(QLabel("测试组合"))
        # 一个按钮：第一次点全选，再点反选，文字提示下一步动作
        self.all_btn = QPushButton("全选")
        self.all_btn.setObjectName("chipBtn")
        self.all_btn.clicked.connect(self._toggle_select)
        add_btn = QPushButton("添加组合")
        add_btn.setObjectName("chipBtn")
        add_btn.clicked.connect(lambda: self._add_row())
        del_btn = QPushButton("删除勾选")
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
        self.table.verticalHeader().setDefaultSectionSize(36)
        # 与「灯显语音测试」的 PRD 表格一致：不显示蓝色选中背景
        self.table.horizontalHeader().setHighlightSections(False)
        pal = self.table.palette()
        for group in (QPalette.Active, QPalette.Inactive,
                      QPalette.Disabled):
            pal.setColor(group, QPalette.Highlight, QColor("#ffffff"))
            pal.setColor(group, QPalette.HighlightedText,
                         QColor("#1e293b"))
        self.table.setPalette(pal)
        self.table.setStyleSheet(
            "QTableWidget{selection-background-color:#ffffff;outline:0;}"
            "QTableWidget::item:selected{background:#ffffff;"
            "color:#1e293b;}"
            "QTableWidget::item:focus{outline:none;border:none;}")
        self.table.setMinimumHeight(150)
        self.table.setMaximumHeight(230)
        root.addWidget(self.table)

        self.table.itemChanged.connect(lambda *_: self._save_settings())
        btns = QHBoxLayout()
        self.start_btn = QPushButton("开始测试")
        self.start_btn.setObjectName("runBtn")
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setObjectName("stopBtn")
        self.stop_btn.setEnabled(False)
        self.start_btn.clicked.connect(self.start)
        self.stop_btn.clicked.connect(self.stop)
        btns.addWidget(self.start_btn)
        btns.addWidget(self.stop_btn)
        btns.addStretch(1)
        self.state_label = QLabel("● 空闲")
        self.state_label.setStyleSheet("color:#64748b;")
        btns.addWidget(self.state_label)
        root.addLayout(btns)

        card = QFrame()
        card.setObjectName("chipStrip")
        grid = QGridLayout(card)
        grid.setContentsMargins(14, 10, 14, 10)
        grid.setHorizontalSpacing(22)
        grid.setVerticalSpacing(6)
        self.metrics = {}
        for i, (key, label) in enumerate((
                ("battery", "电量"), ("clean", "已清扫"),
                ("fan", "吸力"), ("water", "水量"),
                ("out", "出水"), ("faults", "故障"),
                ("restarts", "重启"))):
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

        bottom = QHBoxLayout()
        self.open_report_btn = QPushButton("打开续航测试报告目录")
        self.open_report_btn.setObjectName("enduranceActionBtn")
        self.open_report_btn.clicked.connect(self._open_report)
        bottom.addWidget(self.open_report_btn)
        bottom.addStretch(1)
        root.addLayout(bottom)

    # ── 组合表 ──
    def _restore_defaults(self):
        self.table.setRowCount(0)
        for mode, fan, water in DEFAULT_COMBOS:
            self._add_row(mode, fan, water, checked=True)
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
        """档位下拉框（只允许从下拉选择，不支持手动输入）。"""
        combo = NoWheelComboBox()
        combo.setMinimumWidth(105)
        for label, value in items:
            combo.addItem(label, value)
        idx = combo.findData(int(current))
        combo.setCurrentIndex(max(0, idx))
        return combo

    @staticmethod
    def _level_value(combo, low, high, label, row):
        value = combo.currentData()
        if not isinstance(value, int):
            raise ValueError("%s 第%d行请选择档位" % (label, row + 1))
        if not low <= value <= high:
            raise ValueError("%s 第%d行超出范围 %d~%d"
                             % (label, row + 1, low, high))
        return value

    def _row_checked(self, row):
        item = self.table.item(row, 0)
        return bool(item is not None and item.checkState() == Qt.Checked)

    def _set_row_checked(self, row, checked):
        item = self.table.item(row, 0)
        if item is not None:
            item.setCheckState(Qt.Checked if checked else Qt.Unchecked)

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
        """删除**勾选**的组合(不是表格选中行), 删除前确认(用户 2026-11-27)。"""
        rows = [r for r in range(self.table.rowCount())
                if self._row_checked(r)]
        if not rows:
            QMessageBox.information(
                self, "没有勾选组合",
                "请先勾选要删除的组合（勾选框在最左侧「启用」列）。")
            return
        if QMessageBox.question(
                self, "确认删除",
                "确定删除已勾选的 %d 个组合？\n删除后不可恢复。"
                % len(rows)) != QMessageBox.Yes:
            return
        for r in reversed(rows):
            self.table.removeRow(r)
        self._save_settings()

    def _combos_snapshot(self):
        rows = []
        for r in range(self.table.rowCount()):
            mode_combo = self.table.cellWidget(r, 1)
            fan_combo = self.table.cellWidget(r, 2)
            water_combo = self.table.cellWidget(r, 3)
            if mode_combo is None or fan_combo is None or water_combo is None:
                continue
            rows.append({"enabled": self._row_checked(r),
                         "mode": mode_combo.currentData(),
                         "fan": int(fan_combo.currentData()),
                         "water": int(water_combo.currentData())})
        return rows

    def _save_settings(self):
        if getattr(self, "_loading", False) or not self._persist:
            return
        try:
            self.settings.setValue(
                "endurance/combos",
                json.dumps(self._combos_snapshot(), ensure_ascii=False))
            self.settings.setValue("endurance/low_battery",
                                   self.low_spin.value())
            self.settings.setValue("endurance/sample_interval",
                                   self.sample_spin.value())
        except Exception:
            pass

    def _load_settings(self):
        try:
            raw = self.settings.value("endurance/combos")
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
        for key, spin in (("endurance/low_battery", self.low_spin),
                          ("endurance/sample_interval", self.sample_spin)):
            try:
                v = int(self.settings.value(key))
            except (TypeError, ValueError):
                continue
            spin.setValue(v)

    def _collect_combos(self):
        combos = []
        for r in range(self.table.rowCount()):
            if not self._row_checked(r):
                continue
            mode_combo = self.table.cellWidget(r, 1)
            fan_combo = self.table.cellWidget(r, 2)
            water_combo = self.table.cellWidget(r, 3)
            if mode_combo is None or fan_combo is None or water_combo is None:
                continue
            mode = mode_combo.currentData()
            fan = self._level_value(fan_combo, 0, 5, "吸力", r)
            water = (self._level_value(water_combo, 0, 4, "水量", r)
                     if water_combo.isEnabled() else 0)
            combos.append((mode, fan, water))
        return combos

    # ── 设备连接(统一走右上角导航条, 用户 2026-11-27) ──
    def _ip(self):
        """当前目标 IP: 设备页已连接的优先, 其次设备页选中/上次使用的。"""
        dev = self.device
        if dev is not None:
            ip = str(getattr(dev, "connected_ip", "") or "").strip()
            if ip:
                return ip
            try:
                ip = str(dev._selected_ip() or "").strip()
            except Exception:
                ip = ""
            if ip:
                return ip
        try:
            return str(self.settings.value("apos/last_ip", "") or "").strip()
        except Exception:
            return ""

    # ── 测试 ──
    def start(self):
        if self.is_running():
            return
        ip = self._ip()
        try:
            combos = self._collect_combos()
        except ValueError as e:
            self._append("[错误] %s" % e)
            return
        if not ip:
            self._append("[错误] 请先填写机器人 IP")
            QMessageBox.warning(self, "未填写 IP",
                                "请先填写或选择扫地机 IP，再开始续航测试。")
            return
        if not combos:
            self._append("[错误] 至少启用一个测试组合")
            return
        if self.device is not None and not getattr(self.device, "connected",
                                                   False):
            ok = QMessageBox.question(
                self, "未连接扫地机",
                "当前还没有连接扫地机（只填了 IP）。\n"
                "开始测试后会自行尝试连接该 IP，是否继续？")
            if ok != QMessageBox.Yes:
                return
        if self.device is not None:
            try:
                self.device._remember_ip(ip)   # 下次启动保留本次测试 IP
            except Exception:
                pass
        self._report = ""
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.state_label.setText("● 启动中…")
        self.state_label.setStyleSheet("color:#2563eb;")
        self._append("[启动] %s，%d 个组合" % (ip, len(combos)))
        self.worker = EnduranceWorker(
            ip, combos, self.low_spin.value(),
            float(self.sample_spin.value()), self, peer=self.device,
            stream=(self.device.shared_stream()
                    if self.device is not None else None))
        self.worker.log_line.connect(self._append)
        self.worker.state.connect(self._on_state)
        self.worker.finished_run.connect(self._on_finished)
        self.worker.start()
        self.runningChanged.emit(True)

    def stop(self):
        if not self.is_running():
            return
        if QMessageBox.question(
                self, "确认停止",
                "确定要停止当前续航测试？\n已产生的数据会保留并生成部分报告。"
                ) != QMessageBox.Yes:
            return
        if True:
            self._append("[停止] 正在结束当前测试…")
            self.stop_btn.setEnabled(False)
            self.state_label.setText("● 停止中…")
            self.state_label.setStyleSheet("color:#d97706;")
            self.worker.stop()

    def _append(self, text):
        # 日志行统一带时间（用户 2026-11-26），便于对着设备日志/报告定位
        self.log.appendPlainText(
            "[%s] %s" % (time.strftime("%H:%M:%S"), str(text)))

    def _on_state(self, s):
        battery = s.get("battery")
        # -1 = 推送未就绪，不要显示成 -1%
        self.metrics["battery"].setText(
            "%d%%" % battery if isinstance(battery, int) and battery >= 0
            else "—")
        self.metrics["clean"].setText(
            _fmt_duration(s.get("clean_seconds") or 0))
        fan = s.get("fan_current")
        self.metrics["fan"].setText(
            "%s / 实际%s" % (s.get("fan_level"),
                             "—" if fan is None else fan))
        water = s.get("water_current")
        self.metrics["water"].setText(
            "%s / 实际%s" % (s.get("water_level"),
                             "—" if water is None else water))
        self.metrics["out"].setText(
            "%s 次 / %s mL" % (s.get("outflows", 0),
                               s.get("water_total_ml", 0)))
        self.metrics["faults"].setText(str(s.get("faults", 0)))
        self.metrics["restarts"].setText(str(s.get("restarts", 0)))
        status = str(s.get("status") or "")
        if status:
            self.state_label.setText("● " + status.replace("work_status_", ""))
            color = "#16a34a" if "clean" in status else "#2563eb"
            if status == "work_status_error":
                color = "#dc2626"
            self.state_label.setStyleSheet("color:%s;" % color)

    def _on_finished(self, ok, message, base, report):
        self._report = self._abs(report)
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.open_report_btn.setToolTip(self._report_dir())
        self.state_label.setText("● 完成" if ok else "● 未完成")
        self.state_label.setStyleSheet(
            "color:#16a34a;" if ok else "color:#dc2626;")
        self._append("[结束] %s %s" % ("完成" if ok else "未完成", message))
        if self._report:
            self._append("[报告] %s" % self._report)
        else:
            self._append("[提示] 本次未生成 Excel 报告，详见运行日志")
        self.worker = None
        self.runningChanged.emit(False)
        if not ok and not base:
            QMessageBox.critical(self, "续航测试启动失败", message)

    @staticmethod
    def _abs(path):
        """结果路径统一绝对化（相对路径按程序根目录解析）。"""
        if not path:
            return ""
        return path if os.path.isabs(path) else os.path.join(BASE_DIR, path)

    def _report_dir(self):
        """续航测试报告所在目录（最新报告的同级目录；还没有则 reports 根）。"""
        if self._report and os.path.isfile(self._report):
            return os.path.dirname(self._report)
        return os.path.join(BASE_DIR, "reports",
                            REPORT_SUBDIRS["endurance"])

    def _refresh_latest(self):
        """定位最近一次续航 Excel，用来给「打开报告目录」定位目录。"""
        root = os.path.join(BASE_DIR, "reports",
                            REPORT_SUBDIRS["endurance"])
        try:
            names = os.listdir(root)
        except OSError:
            names = []
        dirs = [os.path.join(root, n) for n in names
                if n.startswith(("endurance_suite_", "endurance_", "shutdown_"))
                and os.path.isdir(os.path.join(root, n))]
        cands = []
        for d in dirs:
            try:
                cands.extend(os.path.join(d, n) for n in os.listdir(d)
                             if n.lower().endswith(".xlsx"))
            except OSError:
                pass
        cands.extend(os.path.join(root, n) for n in names
                     if n.startswith("续航测试") and n.lower().endswith(".xlsx"))
        report = max(cands, key=os.path.getmtime) if cands else ""
        self._report = report
        self.open_report_btn.setToolTip(self._report_dir())
        if report:
            self._append("[报告] 最近一次续航报告: %s" % report)

    def _open_report(self):
        """打开续航测试报告目录（用户 2026-11-26：只看最终报告所在目录）。"""
        d = self._report_dir()
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
        if os.path.isdir(d):
            QDesktopServices.openUrl(QUrl.fromLocalFile(d))
            return
        self._append("[提示] 报告目录不存在: %s" % d)

    def is_running(self):
        return self.worker is not None and self.worker.isRunning()

    def shutdown(self):
        if self.is_running():
            self.worker.stop()
            self.worker.wait(10000)
        self.runningChanged.emit(False)
