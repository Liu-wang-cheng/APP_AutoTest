# -*- coding: utf-8 -*-
"""设备参数页：扫描/连接机器人，实时参数表与曲线。

页签在 MainWindow 顶部（自动化测试 / 设备参数），本页是独立整页。
"""
import json
import os
import re
import threading
import time

import yaml

from PySide6.QtCore import (QEvent, QPoint, QRect, QSettings, QSize,
                            QThread, QTimer, Signal, Qt)
from PySide6.QtGui import (QAbstractTextDocumentLayout, QColor,
                           QKeySequence, QPalette, QPen, QShortcut,
                           QTextCursor, QTextDocument, QTextOption)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox,
                                QComboBox, QCompleter, QDialog,
                                QDialogButtonBox, QFileDialog, QFormLayout,
                                QFrame, QHBoxLayout, QHeaderView, QLabel,
                                QLineEdit, QListWidget, QListWidgetItem, QMenu,
                                QMessageBox, QPushButton, QScrollArea, QStyle,
                                QStyledItemDelegate, QStyleOptionHeader,
                                QStyleOptionViewItem, QTabWidget,
                                QTableWidget, QTableWidgetItem, QTextEdit,
                                QVBoxLayout, QWidget)

from core.apos import (ai_prd, catalog, embedding, faults,
                       hs4_config, prd_memory, scanner, seed_review,
                       sdk as sdk_mod, voice_package)
from core.apos.client import AposClient, AposError
from core.apos.mcu import McuStream
from core.apos.service import ParamService
from core.apos.stream import UploadStream
from core.logger import get_logger
from gui.ai_review import AiReviewWorker
from gui.curve_view import CurveView
from gui.sdk_setup import ensure_sdk
from gui.widgets import FlowLayout, NoWheelComboBox, WrappedCombo

log = get_logger()


#: 解析结果持久化格式版本; 规则变化时旧结果不再展示(用户 2026-11-12)
PRD_ROWS_VERSION = 3


def _space_duplicate_events(items):
    """相邻事件相同就错开：先触发再下一条，重复的那条往后挪（用户 2026-11-16）。

    同一条事件连着触发时，第二次的设备日志/语音会被第一次的残留干扰，
    容易误判。第一轮把重复项和后面第一个不同的事件互换；若重复项已在
    末尾（没有「下下条」可换），第二轮再和前面能安全对调的位置交换。
    """
    out = list(items or [])

    def no_adjacent_equal(arr):
        return all(arr[i][0] != arr[i + 1][0] for i in range(len(arr) - 1))

    for i in range(len(out) - 1):
        if out[i][0] != out[i + 1][0]:
            continue
        j = next((k for k in range(i + 2, len(out))
                  if out[k][0] != out[i][0]), None)
        if j is not None:
            out[i + 1], out[j] = out[j], out[i + 1]
    for i in range(len(out) - 1):
        if out[i][0] != out[i + 1][0]:
            continue
        for k in reversed(range(len(out))):
            if k in (i, i + 1) or out[k][0] == out[i + 1][0]:
                continue
            probe = out[:]
            probe[i + 1], probe[k] = probe[k], probe[i + 1]
            if no_adjacent_equal(probe):
                out = probe
                break
    return out


class _PrdPrepWorker(QThread):
    """后台做 PRD 解析前准备(读语音表/记忆库/候选事件), 避免点按钮卡顿。"""
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, page, path, parent=None, prefer_memory=True):
        super().__init__(parent)
        self._page = page
        self._path = path
        self._prefer_memory = prefer_memory

    def run(self):
        try:
            page = self._page
            catalog = ai_prd.extract_voice_catalog(self._path)
            page._prd_catalog = catalog
            if self._prefer_memory:
                mem_items, covered = page._memory_prefill()
            else:
                # 「直接用 AI 解析 PRD」: 不读学习库, 全部交给 AI
                mem_items, covered = [], set()
            entries = page._catalog_entries(catalog)
            # 未命中按 Q 号算最终语音: 复用同一语音的兄弟 Q 号没命中时,
            # 也要把该语音纳入 AI 候选, 不能按最终语音一刀切(用户 2026-11-17)
            covered_qids = {str(it.get("qid") or "") for it in mem_items
                            if it.get("qid")}
            missing = sorted({
                int(info.get("final", k))
                for k, info in ((catalog or {}).get("active") or {}).items()
                if str(info.get("qid") or f"Q{int(k):03d}")
                not in covered_qids})
            # 没有目录(PDF/TXT)时退化为“全部交给 AI”
            if not missing and entries and not covered_qids:
                missing = list(entries)
            events = page._ai_event_list()
            if catalog and entries and missing:
                events = page._candidate_events_for(missing) or events
            self.done.emit({
                "catalog": catalog,
                "mem_items": mem_items,
                "covered": covered,
                "entries": entries,
                "missing": missing,
                "events": events,
                "force_ai": not self._prefer_memory,
            })
        except Exception as e:
            self.failed.emit(str(e))


class _VoiceCheckWorker(QThread):
    """批量测试前 SSH 检查 /tmp/music, 不阻塞界面。"""
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, host, rows, parent=None, expect=None):
        super().__init__(parent)
        self._host = host
        self._rows = rows
        self._expect = expect

    def run(self):
        try:
            self.done.emit(voice_package.check_remote(
                self._host, self._rows, expect=self._expect))
        except Exception as e:
            self.failed.emit(str(e))


class _AiPrdWorker(QThread):
    """后台调用 AI 解析 PRD(不卡界面)。"""
    done = Signal(object)
    failed = Signal(str)
    progress = Signal(str)
    #: 部分批次失败但仍有结果时的提示(不能悄悄当成功)
    warning = Signal(str)
    #: 事件多时分批请求: 一次几百个事件很容易被模型截断/格式坏掉
    CHUNK_SIZE = 80

    def __init__(self, path, events, cfg, led_names, parent=None,
                 voice_catalog=None):
        super().__init__(parent)
        self._path = path
        self._events = events
        self._cfg = cfg
        self._led = led_names
        self._catalog = voice_catalog

    @classmethod
    def chunks(cls, events):
        n = cls.CHUNK_SIZE
        for i in range(0, len(events), n):
            yield events[i:i + n]

    def _parse(self, events):
        return ai_prd.parse_prd(
            self._path, events,
            self._cfg["base_url"], self._cfg["api_key"],
            self._cfg["model"], self._led,
            max_chars=self._cfg.get("max_chars", ai_prd.MAX_PRD_CHARS),
            max_tokens=self._cfg.get("max_tokens",
                                     ai_prd.DEFAULT_MAX_TOKENS),
            voice_catalog=self._catalog)

    def run(self):
        try:
            parts = list(self.chunks(self._events))
            merged = {}
            errors = []
            for i, chunk in enumerate(parts, 1):
                self.progress.emit(
                    f"AI 解析第 {i}/{len(parts)} 批"
                    f"（{len(chunk)} 个候选事件）…")
                items = None
                last = None
                for _attempt in (1, 2):      # 模型偶尔输出坏 JSON, 重试一次
                    try:
                        items = self._parse(chunk)
                        break
                    except Exception as e:
                        last = e
                if items is None:
                    errors.append(str(last))
                    continue
                for it in items:
                    merged.setdefault(it.get("event"), it)
            if not merged:
                raise RuntimeError(errors[0] if errors else "AI 未返回结果")
            if errors:
                self.warning.emit(
                    f"AI 解析有 {len(errors)}/{len(parts)} 批失败（"
                    f"{errors[0][:160]}），其余批次结果已保留，"
                    "未覆盖的语音会显示为未找到")
            self.done.emit(list(merged.values()))
        except Exception as e:       # 网络/格式错误都反馈到界面
            self.failed.emit(str(e))


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
    # ★ 刚连上时 /System/FirmwareInfo 偶发返回空(推送流还没就绪), 只读一次
    #   就会永久留下空的固件版本。这里按 version_retries 补读几次(用户 2026-11-13)。
    attempts = max(1, int(version_retries or 1))
    for attempt in range(attempts):
        try:
            data = service.read_once(
                ["/System/CustomSN", "/System/FirmwareInfo"])
        except Exception:
            data = {}
        sn = str((data or {}).get("/System/CustomSN") or "").strip()
        if sn:
            info["sn"] = sn
        fw = (data or {}).get("/System/FirmwareInfo")
        if isinstance(fw, str):
            try:
                fw = json.loads(fw)
            except Exception:
                fw = {}
        if isinstance(fw, dict):
            model = str(fw.get("Model") or "").strip()
            version = str(fw.get("Version") or "").strip()
            if model:
                info["model"] = model
            if version:
                info["firmware_version"] = version
        if info["firmware_version"]:
            break
        if attempt + 1 < attempts:
            time.sleep(0.4)
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
        # ★ 先只读一次快速项, 立刻报告"已连接"; SN/固件偶发读空放到 done
        #   之后后台补读。网络慢/推送流不稳时, 反复补读会把"已连接"拖几十秒
        #   (用户 2026-11-17: 10.63 长时间显示连接中)。
        info = {}
        try:
            info = read_robot_info(self.service, version_retries=1,
                                   include_base_station=False)
        except Exception:
            info = {}
        self.done.emit(True, self.ip, info)
        # done 之后后台补 SN/固件; 每轮只读一次, 拿到就通知界面
        for _ in range(3):
            if self._cancel.is_set():
                break
            if info.get("sn") and info.get("firmware_version"):
                break
            self._sleep(0.8)
            if self._cancel.is_set():
                break
            try:
                more = read_robot_info(self.service, version_retries=1,
                                       include_base_station=False)
            except Exception:
                more = {}
            gained = {k: v for k, v in (more or {}).items()
                      if v and not info.get(k)}
            if gained:
                info.update(gained)
                self.info_ready.emit(self.ip, gained)
        if not info.get("firmware_version"):
            log.info("[连接] 固件版本未读到(/System/FirmwareInfo 为空)")
        # 先把推送流开起来（参数/曲线立刻有数据），再在后台读基站版本。
        # 基站版本是机器人上的文件：ParamService.read_base_station 会在推流
        # 句柄上用 binpack 读（librpc 的文件读在开流后会一直返回空）。
        start_stream = getattr(self.service, "start_stream", None)
        if callable(start_stream):
            try:
                start_stream()
            except Exception as e:
                log.debug(f"[连接] 上传流启动失败(退回轮询): {e}")
        # ★ 开流后再补读 SN/型号/固件：CLA_ZY2L 的 CustomSN 在 librpc 上读不到，
        #   只有开流后从机器人文件(/oem/bin/sys_data/custom.sn)读得到。
        #   不补这一步，设备参数页/设备列表会一直缺 SN（用户 2026-11-26）。
        for _ in range(3):
            if self._cancel.is_set():
                break
            if info.get("sn") and info.get("firmware_version"):
                break
            self._sleep(0.8)
            try:
                more = read_robot_info(self.service, version_retries=1,
                                       include_base_station=False)
            except Exception:
                more = {}
            gained = {k: v for k, v in (more or {}).items()
                      if v and not info.get(k)}
            if gained:
                info.update(gained)
                self.info_ready.emit(self.ip, gained)
        reader = getattr(self.service, "read_base_station", None)
        client = getattr(self.service, "client", None)
        version = ""
        # ★ 先让推送流跑几秒（曲线/参数先出数据），再读基站版本文件；
        #   没有基站的机型要走"触发刷新 + 重试"，最多约 2 秒。
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


class _SendEventWorker(QThread):
    """后台发事件：AposSdkSendEvent 在通道异常/网络抖动时会阻塞数秒。

    放在 UI 线程会表现为"点触发就卡住无响应"（用户 2026-11-16）。
    done 带回【实际开始发送的时间】: 等待 SDK 锁可能耗时几秒, 日志窗口
    必须从真正发出那一刻算, 否则设备响应晚到会被判成"未捕获"。
    """
    done = Signal(bool, float)

    def __init__(self, service, topic, event, key="", parent=None):
        super().__init__(parent)
        self._service = service
        self._topic = topic
        self._event = event
        self._key = key

    def run(self):
        started = time.time()
        try:
            if self._key:
                ok = bool(self._service.simulate_fault(self._key))
            else:
                ok = bool(self._service.post_event(self._topic, self._event))
        except Exception:
            ok = False
        elapsed = time.time() - started
        if elapsed >= 0.5:
            log.info(f"[故障模拟] 发送 {self._event or self._key} "
                     f"耗时 {elapsed:.2f}s")
        self.done.emit(ok, started)


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


class _WrapItemEditor(QTextEdit):
    """事件单元格编辑框: 多行显示, 回车提交而不是换行。"""

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.clearFocus()
            ev.accept()
            return
        super().keyPressEvent(ev)


class _BilingualPopupDelegate(QStyledItemDelegate):
    """下拉弹层显示「中文（英文事件名）」, 输入框只显示中文(用户 2026-11-27)。"""

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        ident = str(index.data(Qt.UserRole) or "")
        if ident and f"（{ident}）" not in option.text:
            option.text = f"{option.text}（{ident}）"


class _WrapAnywhereDelegate(QStyledItemDelegate):
    """事件列专用: 长英文标识符按任意字符换行, 行高按实际行数计算。

    Qt 默认按单词边界算行高, error_xxx 这类没有空格的长串会算成一行,
    单元格于是显示省略号; 这里用 QTextDocument 按
    WrapAtWordBoundaryOrAnywhere 重新量高度(用户 2026-11-13)。
    """

    def sizeHint(self, option, index):
        text = str(index.data(Qt.DisplayRole) or "")
        width = option.rect.width()
        if not text or width <= 0:
            return super().sizeHint(option, index)
        doc = QTextDocument()
        doc.setDefaultFont(option.font)
        doc.setDocumentMargin(0)
        wrap = QTextOption()
        wrap.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        doc.setDefaultTextOption(wrap)
        doc.setPlainText(text)
        doc.setTextWidth(max(40, width - 10))
        return QSize(int(width), int(doc.size().height()) + 8)

    def createEditor(self, parent, option, index):
        if index.column() != 0:
            return super().createEditor(parent, option, index)
        editor = _WrapItemEditor(parent)
        editor.setWordWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        editor.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        editor.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        editor.setStyleSheet(
            "QTextEdit{border:1px solid #2563eb;background:#ffffff;"
            "color:#1e293b;font-size:12px;padding:2px;}")
        return editor

    def setEditorData(self, editor, index):
        if isinstance(editor, QTextEdit):
            editor.setPlainText(str(index.data(Qt.EditRole)
                                    or index.data(Qt.DisplayRole) or ""))
            editor.selectAll()
            return
        super().setEditorData(editor, index)

    def setModelData(self, editor, model, index):
        if isinstance(editor, QTextEdit):
            model.setData(index, editor.toPlainText().strip(), Qt.EditRole)
            return
        super().setModelData(editor, model, index)

    def updateEditorGeometry(self, editor, option, index):
        if isinstance(editor, QTextEdit):
            rect = option.rect
            editor.setGeometry(rect.x(), rect.y(), rect.width(),
                               max(rect.height(), 60))
            return
        super().updateEditorGeometry(editor, option, index)

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text = opt.text
        opt.text = ""
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)
        if not text:
            return
        rect = style.subElementRect(
            QStyle.SE_ItemViewItemText, opt, opt.widget).adjusted(2, 1, -2, -1)
        doc = QTextDocument()
        doc.setDefaultFont(opt.font)
        doc.setDocumentMargin(0)
        wrap = QTextOption()
        wrap.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        align = index.data(Qt.TextAlignmentRole)
        if align is not None:
            wrap.setAlignment(Qt.Alignment(int(align)))
        doc.setDefaultTextOption(wrap)
        doc.setPlainText(text)
        doc.setTextWidth(rect.width())
        ctx = QAbstractTextDocumentLayout.PaintContext()
        ctx.palette = opt.palette
        if opt.state & QStyle.State_Selected:
            ctx.palette.setColor(
                QPalette.Text, opt.palette.color(QPalette.HighlightedText))
        painter.save()
        painter.setClipRect(rect)
        # 文本在行高内上下居中(普通单元格没有下拉框宿器)
        y_off = max(0, int((rect.height() - doc.size().height()) / 2))
        painter.translate(rect.left(), rect.top() + y_off)
        doc.documentLayout().draw(painter, ctx)
        painter.restore()


class _CenteredCell(QWidget):
    """表格单元格宿器: 让固定高度的下拉框在行高内垂直居中。"""

    def __init__(self, child, parent=None):
        super().__init__(parent)
        self.child = child
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addStretch(1)
        lay.addWidget(child)
        lay.addStretch(1)


class _PrdHeader(QHeaderView):
    """「类型」列头做成上下结构: 上面「类型」, 下面是当前筛选值(可点击弹下拉)。

    普通表头是一行文字; 用户要求筛选设置放在类型下面(2026-11-13),
    这里自绘第 2 列: 上面标题, 下面一个小下拉框样式的「全部/通用… ▼」。
    """

    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        self._kind_filter = ""
        self.setSectionsClickable(True)

    def set_kind_filter(self, kind):
        self._kind_filter = str(kind or "")
        self.updateSection(2)

    def paintSection(self, painter, rect, logicalIndex):
        if logicalIndex != 2:
            super().paintSection(painter, rect, logicalIndex)
            return
        opt = QStyleOptionHeader()
        self.initStyleOption(opt)
        opt.rect = rect
        opt.section = logicalIndex
        opt.text = ""
        self.style().drawControl(QStyle.CE_Header, opt, painter, self)
        painter.save()
        # 上面: 类型
        f = painter.font()
        f.setBold(True)
        f.setPixelSize(12)
        painter.setFont(f)
        painter.setPen(QColor("#1e293b"))
        top = rect.adjusted(2, 3, -2, 0)
        top.setHeight(max(16, painter.fontMetrics().lineSpacing()))
        painter.drawText(top, Qt.AlignHCenter | Qt.AlignTop, "类型")
        # 下面: 当前筛选值, 画成小下拉框
        text = f"{self._kind_filter or '全部'} ▼"
        f.setBold(False)
        f.setPixelSize(11)
        painter.setFont(f)
        fm = painter.fontMetrics()
        box_w = min(rect.width() - 8, fm.horizontalAdvance(text) + 18)
        box_h = max(20, fm.height() + 6)
        box = QRect(rect.center().x() - box_w // 2,
                    rect.bottom() - box_h - 4, box_w, box_h)
        painter.setPen(QPen(QColor("#cbd5e1"), 1))
        painter.setBrush(QColor("#ffffff"))
        painter.drawRoundedRect(box, 4, 4)
        painter.setPen(QColor("#2563eb" if self._kind_filter
                              else "#475569"))
        painter.drawText(box, Qt.AlignCenter, text)
        painter.restore()


class DevicePage(QWidget):
    connectionChanged = Signal(bool, str)     # (connected, label)
    connectingChanged = Signal(bool, str)     # (是否连接中, ip) —— 右上角显示"连接中"
    devicesChanged = Signal()                 # 设备列表/型号/SN 更新
    connect_finished = Signal(bool, str, str)  # (ok, ip, 错误信息) 连接结束
    runningChanged = Signal(bool)             # 灯显语音「批量测试」运行状态

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
        # ★ 连接过程中又切换了 IP: 记下目标, 本次连接结束后立即改连它
        self._pending_connect_ip = ""
        self._pending_connect_retries = 0
        #: 后台发事件的线程（不阻塞 UI）
        self._send_worker = None
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
        head.setSpacing(12)                 # 与续航/循环/灯显页一致
        title = QLabel("设备参数")
        title.setStyleSheet("font-size:16px;font-weight:700;color:#1e293b;")
        head.addWidget(title)

        # ★ 顶部控件用流式布局(2026-11-06): 窗口窄时自动换行, 不把页面的
        #   最小宽度顶大 —— 否则切到本页会把整个窗口自动撑宽(用户要求界面
        #   大小只由手动拖拽决定)。
        bar_host = QWidget()
        bar = FlowLayout(bar_host, margin=0, spacing=7)
        bar.addWidget(QLabel("扫地机IP"))
        self.device_combo = NoWheelComboBox()
        self.device_combo.setEditable(True)
        self.device_combo.setMinimumContentsLength(16)
        self.device_combo.setMaximumWidth(560)
        self.device_combo.lineEdit().setPlaceholderText("扫描或输入 IP")
        self.device_combo.activated.connect(self._device_activated)
        self.device_combo.lineEdit().returnPressed.connect(self._device_activated)
        # 型号-SN-IP 比较长：文本框按最长内容自适应宽度（用户 2026-11-06）
        self.device_combo.lineEdit().textChanged.connect(
            lambda *_: (self._fit_ip_combo_width(), self._sync_connect_btn()))
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
        # ★ 扫地机 IP/连接统一在右上角导航条(用户 2026-11-27), 本页不再显示;
        #   控件保留给扫描/连接内部逻辑使用(不加入布局, 不占空间)。
        self._ip_bar_host = bar_host
        bar_host.hide()

        # 说明与扫描/连接状态放在标题右侧(用户 2026-11-27), 不再单独占一行
        sub = QLabel("实时读取扫地机内部属性（只读）")
        sub.setStyleSheet("color:#94a3b8;font-size:11px;")
        sub.setWordWrap(False)
        head.addWidget(sub)
        self.status_label = QLabel("")     # 只放扫描提示; 连接状态统一在右上角导航
        self.status_label.setStyleSheet("color:#94a3b8;font-size:11px;")
        self.status_label.setWordWrap(False)
        head.addWidget(self.status_label)
        head.addStretch(1)
        v.addLayout(head)

        self.sdk_line = QFrame()
        self.sdk_line.setStyleSheet(
            "QFrame{background:#f8fafc;border:1px solid #e4e8ee;border-radius:6px;}")
        sl = QHBoxLayout(self.sdk_line)
        sl.setContentsMargins(8, 5, 8, 5)
        sl.setSpacing(8)
        self.sdk_label = QLabel()
        self.sdk_label.setStyleSheet("color:#475569;font-size:11px;")
        self.sdk_label.setWordWrap(False)
        sl.addWidget(self.sdk_label)
        self.sdk_btn = QPushButton("更换 SDK")
        self.sdk_btn.setObjectName("chipBtn")
        self.sdk_btn.clicked.connect(self._change_sdk)
        sl.addWidget(self.sdk_btn)
        # ★ SDK 与 SN/固件并排一行, 给下方参数表腾高度(用户 2026-11-27)
        sl.addSpacing(12)
        self.info_label = QLabel()
        self.info_label.setStyleSheet("color:#475569;font-size:11px;")
        self.info_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.info_label.setWordWrap(False)
        self.info_label.setToolTip("SN / 固件版本 / 基站版本")
        sl.addWidget(self.info_label, 1)
        self.sdk_line.setToolTip("SDK 目录与设备信息")
        v.addWidget(self.sdk_line)
        self.info_line = self.sdk_line      # 兼容旧引用
        self._update_info_label()

        self.tabs = QTabWidget()
        self.tabs.currentChanged.connect(self._tab_changed)
        self.params_tab = QWidget()
        self._build_params(self.params_tab)
        self.tabs.addTab(self.params_tab, "实时参数")
        self.curve_view = CurveView(settings=self.settings)
        self.curve_view.pathsChanged.connect(self._curve_paths_changed)
        self.tabs.addTab(self.curve_view, "曲线")
        # 故障模拟独立成顶部页面(用户 2026-11-12), 这里只构建内容
        self.fault_page = QScrollArea()
        self.fault_page.setWidgetResizable(True)
        self.fault_page.setFrameShape(QFrame.NoFrame)
        fault_body = QWidget()
        self._build_faults(fault_body)
        if isinstance(self.fault_page, QScrollArea):
            self.fault_page.setWidget(fault_body)
        self.fault_tab = self.fault_page      # 兼容旧引用/测试
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
        self.interval_combo = NoWheelComboBox()
        for sec in ("1", "2", "5", "10"):
            self.interval_combo.addItem(f"刷新 {sec} 秒", int(sec))
        self.interval_combo.currentIndexChanged.connect(self._interval_changed)
        bar.addWidget(self.interval_combo)
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
        # 选中单元格不要蓝底、也不要外围虚线焦点框(用户 2026-11-27)
        pal = self.table.palette()
        for group in (QPalette.Active, QPalette.Inactive, QPalette.Disabled):
            pal.setColor(group, QPalette.Highlight, QColor("#ffffff"))
            pal.setColor(group, QPalette.HighlightedText, QColor("#1e293b"))
        self.table.setPalette(pal)
        self.table.setStyleSheet(
            "QTableWidget{outline:0;}"
            "QTableWidget::item:selected{background:#ffffff;color:#1e293b;}"
            "QTableWidget::item:focus{outline:none;border:none;}")
        body.addWidget(self.table, 1)
        v.addLayout(body, 1)
        self._init_categories()

    # ── 故障模拟(用户 2026-11-11): 只验证设备日志里的语音/灯显 ──
    def _fit_line_edit(self, edit, min_w=160, max_w=420):
        """输入框宽度随内容自适应(有上限, 超长路径用 tooltip/横向滚动)。"""
        fm = edit.fontMetrics()
        text = edit.text() or edit.placeholderText()
        width = fm.horizontalAdvance(text) + 24
        # 只限制上限, 窗口变窄时输入框可以收缩(内容用 tooltip/滚动查看),
        # 而不是整行换到下一行(用户 2026-11-12)
        edit.setMinimumWidth(90)
        edit.setMaximumWidth(max(min_w, min(width, max_w)))

    @staticmethod
    def _fit_editable_combo_width(combo, min_w=220, max_w=680):
        """可编辑下拉框按内容自适应宽度(输入框本身, 不是弹出列表)。"""
        fm = combo.fontMetrics()
        texts = [combo.itemText(i) for i in range(combo.count())]
        current = combo.currentText().strip()
        if current:
            texts.append(current)
        longest = max((fm.horizontalAdvance(t) for t in texts), default=0)
        width = max(min_w, min(longest + 72, max_w))
        combo.setFixedWidth(width)

    def _fit_combo_width(self, combo, min_w=160, max_w=560):
        """下拉框宽度按最长选项名自适应(长事件名不再被截断)。"""
        fm = combo.fontMetrics()
        width = 0
        for i in range(combo.count()):
            width = max(width, fm.horizontalAdvance(combo.itemText(i)))
        if combo.isEditable():
            width = max(width, fm.horizontalAdvance(combo.currentText()))
        need = max(min_w, min(width + 40, max_w))
        combo.setMinimumWidth(100)
        combo.setMaximumWidth(need)
        try:
            # 即使框体受窗口限制, 展开的候选列表也按完整名称显示
            combo.view().setMinimumWidth(min(width + 40, 720))
        except Exception:
            pass

    def _build_faults(self, parent):
        v = QVBoxLayout(parent)
        # 与设备参数页保持同样的左右留白, 否则文字/表格贴在窗口边缘像被裁掉
        v.setContentsMargins(14, 8, 14, 0)
        v.setSpacing(7)
        # 与续航/循环清扫页一致的大标题(用户 2026-11-27)
        head = QHBoxLayout()
        head.setSpacing(12)
        title = QLabel("灯显语音测试")
        title.setStyleSheet("font-size:16px;font-weight:700;color:#1e293b;")
        head.addWidget(title)
        tip = QLabel("触发故障事件后，等约 3 秒从设备日志解析语音序号、灯显和故障码。")
        tip.setStyleSheet("color:#94a3b8;font-size:11px;")
        tip.setWordWrap(False)          # 右侧还有空间, 一行显示(用户 2026-11-27)
        tip.setToolTip(tip.text())
        head.addWidget(tip)
        head.addStretch(1)
        v.addLayout(head)
        # ★ 扫地机IP连接统一在右上角导航条(用户 2026-11-27), 本页不再放 IP 行;
        #   连接状态只看导航条, fault_status 只放触发/业务提示。
        # 常用故障 + 触发故障(原与 IP 同一行, IP 上移后单独一行)
        pick_host = QWidget()
        pick_row = FlowLayout(pick_host, margin=0, spacing=6)
        pick_row.addWidget(QLabel("常用故障"))
        # 常用故障也支持手动输入, 长名称在框内换行显示
        self.fault_pick_combo = WrappedCombo()
        self.fault_pick_combo.setInsertPolicy(QComboBox.NoInsert)
        if self.fault_pick_combo.lineEdit() is not None:
            self.fault_pick_combo.lineEdit().setPlaceholderText(
                "输入关键字搜索故障…")
        self.fault_pick_combo.setCompleter(
            self._make_event_completer(self.fault_pick_combo, []))
        self._fit_combo_width(self.fault_pick_combo, 220, 480)
        pick_row.addWidget(self.fault_pick_combo)
        pick_btn = QPushButton("触发故障")
        pick_btn.setObjectName("chipBtn")
        pick_btn.clicked.connect(self._simulate_picked_fault)
        pick_row.addWidget(pick_btn)
        pick_btn.setMinimumWidth(pick_btn.sizeHint().width())
        v.addWidget(pick_host)

        # AP 方案配置 + 触发所选事件
        top_host = QWidget()
        top_row = QHBoxLayout(top_host)
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.setSpacing(6)
        top_row.addWidget(QLabel("AP 方案"))
        cfg_btn = QPushButton("选择 AP 配置…")
        cfg_btn.setObjectName("chipBtn")
        cfg_btn.clicked.connect(self._choose_fault_config)
        top_row.addWidget(cfg_btn)
        # 只显示文件名(完整路径放 tooltip), 重开程序沿用上次选择(用户 2026-11-27)
        self.fault_cfg_label = QLabel("未选择")
        self.fault_cfg_label.setStyleSheet(
            "color:#64748b;font-size:11px;padding:0 6px;")
        self.fault_cfg_label.setMinimumWidth(80)
        self.fault_cfg_label.setMaximumWidth(300)
        self.fault_cfg_label.setToolTip("AP 平台方案配置文件")
        top_row.addWidget(self.fault_cfg_label)
        # 「事件」文字标签已去掉, 下拉框直接跟在 AP 配置后(用户 2026-11-27)
        self.fault_event_combo = WrappedCombo()
        self.fault_event_combo.setMinimumWidth(120)
        self.fault_event_combo.setInsertPolicy(QComboBox.NoInsert)
        self.fault_event_combo.setCompleter(
            self._make_event_completer(self.fault_event_combo, []))
        # 弹层中英对照, 关掉后输入框只显示中文(用户 2026-11-27)
        self.fault_event_combo.view().setItemDelegate(
            _BilingualPopupDelegate(self.fault_event_combo.view()))
        if self.fault_event_combo.lineEdit() is not None:
            self.fault_event_combo.lineEdit().setPlaceholderText(
                "搜索事件…")
        self._fit_combo_width(self.fault_event_combo, 90, 200)
        top_row.addWidget(self.fault_event_combo)
        trig_btn = QPushButton("触发所选事件")
        trig_btn.setObjectName("chipBtn")
        trig_btn.setToolTip("工具自动判断事件的触发方式，无需选择主题")
        trig_btn.clicked.connect(self._trigger_custom_event)
        top_row.addWidget(trig_btn)
        for b in (cfg_btn, trig_btn):
            b.setMinimumWidth(b.sizeHint().width())
        top_row.addStretch(1)      # 全部靠左(用户 2026-11-27)
        v.addWidget(top_host)

        # PRD 解析单独一行(用户 2026-11-12)
        prd_host = QWidget()
        prd_row = QHBoxLayout(prd_host)
        prd_row.setContentsMargins(0, 0, 0, 0)
        prd_row.setSpacing(6)
        prd_row.addWidget(QLabel("PRD"))
        prd_btn = QPushButton("选择 PRD…")
        prd_btn.setObjectName("chipBtn")
        prd_btn.clicked.connect(self._choose_prd)
        prd_row.addWidget(prd_btn)
        # 只显示文件名(完整路径放 tooltip), 重开程序沿用上次选择(用户 2026-11-27)
        self.fault_prd_label = QLabel("未选择")
        self.fault_prd_label.setStyleSheet(
            "color:#64748b;font-size:11px;padding:0 6px;")
        self.fault_prd_label.setMinimumWidth(80)
        self.fault_prd_label.setMaximumWidth(300)
        self.fault_prd_label.setToolTip("PRD 文档")
        prd_row.addWidget(self.fault_prd_label)
        self.fault_prd_parse_btn = QPushButton("解析")
        self.fault_prd_parse_btn.setObjectName("chipBtn")
        self.fault_prd_parse_btn.setToolTip(
            "优先用学习库解析；学习库没有内容或首次解析时自动改用 AI")
        self.fault_prd_parse_btn.clicked.connect(self._parse_prd)
        prd_row.addWidget(self.fault_prd_parse_btn)
        self.fault_prd_ai_btn = QPushButton("直接用 AI 解析 PRD")
        self.fault_prd_ai_btn.setObjectName("chipBtn")
        self.fault_prd_ai_btn.setToolTip(
            "忽略学习库，把 PRD 全部事件直接交给 AI 解析")
        self.fault_prd_ai_btn.clicked.connect(self._parse_prd_with_ai)
        prd_row.addWidget(self.fault_prd_ai_btn)
        for b in (prd_btn, self.fault_prd_parse_btn,
                  self.fault_prd_ai_btn):
            b.setMinimumWidth(b.sizeHint().width())
        prd_row.addStretch(1)      # 全部靠左(用户 2026-11-27)
        v.addWidget(prd_host)

        # 操作行: 批量测试 / 导出报告放在一起(用户 2026-11-12)
        act_host = QWidget()
        act_row = FlowLayout(act_host, margin=0, spacing=6)
        prd_title = QLabel("PRD事件解析列表")
        prd_title.setStyleSheet(
            "font-size:13px;font-weight:700;color:#1e293b;padding-right:6px;")
        act_row.addWidget(prd_title)
        self.fault_batch_btn = QPushButton("批量测试")
        self.fault_batch_btn.setToolTip(
            "依次触发清单里的事件并自动对照；常用故障框里输入关键字可只跑匹配项")
        self.fault_batch_btn.clicked.connect(self._toggle_batch)
        act_row.addWidget(self.fault_batch_btn)
        self.fault_report_btn = QPushButton("导出测试报告…")
        self.fault_report_btn.setEnabled(False)
        self.fault_report_btn.clicked.connect(self._export_fault_report)
        act_row.addWidget(self.fault_report_btn)
        self.fault_save_cfg_btn = QPushButton("保存配置…")
        self.fault_save_cfg_btn.setToolTip("把解析事件/语音/灯显/故障码保存为 YAML")
        self.fault_save_cfg_btn.clicked.connect(self._save_prd_config_file)
        act_row.addWidget(self.fault_save_cfg_btn)
        self.fault_load_cfg_btn = QPushButton("读取配置…")
        self.fault_load_cfg_btn.setToolTip("从 YAML 读取之前保存的事件配置")
        self.fault_load_cfg_btn.clicked.connect(self._load_prd_config_file)
        act_row.addWidget(self.fault_load_cfg_btn)
        # 最右侧搜索框: 按事件/中文名/Q号搜索并跳到对应解析行(用户 2026-11-13)
        act_row.addStretch(1)
        self.fault_search = QLineEdit()
        self.fault_search.setPlaceholderText("搜索事件 / 中文名 / Q号…")
        self.fault_search.setClearButtonEnabled(True)
        self.fault_search.setFixedWidth(220)
        self.fault_search.setToolTip("输入关键字，列表只显示匹配的解析事件")
        self.fault_search.textChanged.connect(self._search_prd_rows)
        act_row.addWidget(self.fault_search)
        for b in (self.fault_batch_btn, self.fault_report_btn,
                  self.fault_save_cfg_btn, self.fault_load_cfg_btn):
            b.setMinimumWidth(b.sizeHint().width())
        v.addWidget(act_host)
        self._fault_expect = {}
        self._fault_cfg_path = ""
        self._prd_path = ""
        self._hs4 = None
        self._fault_rows = []
        self._fault_report_started = ""
        self._fault_groups = {}
        self._batch = None
        self._prd_rows = []
        self._prd_fill_pending = False
        self._prd_filling = False
        self._prd_worker = None
        self._prd_prep_worker = None
        self._prd_force_ai = False
        self._prd_parse_used_ai = False
        self._prd_parse_cfg = None
        self._prd_audit_worker = None
        self._prd_audit_threads = set()
        self._prd_audit_run_id = 0
        self._voice_check_worker = None
        self._voice_check_result = None
        self._prd_catalog = None
        mem_path = None
        try:
            fn = str(self.settings.fileName() or "")
            if fn.lower().endswith(".ini"):
                mem_path = os.path.join(os.path.dirname(fn),
                                        "prd_memory.json")
        except Exception:
            mem_path = None
        self._prd_memory = prd_memory.PrdMemory(mem_path)
        self._verified_pending = {}
        # 新用户先继承随工具发布的种子库(最低优先级), 再叠加本机学习；
        # 单测不加载真实种子, 避免影响内存覆盖用例
        if not _is_pytest():
            try:
                self._prd_memory.load_seed_file(
                    prd_memory.default_seed_path())
            except Exception:
                pass
        self._prd_memory_items = []
        self._prd_memory_covered = set()
        self._fault_log_hist = []
        # AI 解析结果表格(按 PRD 验证用)；没有结果时隐藏, 避免撑高窗口
        self._prd_kind_filter = ""
        self._prd_search_text = ""
        self.fault_prd_table = QTableWidget(0, 8)
        # 类型列头: 上面「类型」, 下面筛选值(用户 2026-11-13)
        self.fault_prd_table.setHorizontalHeader(
            _PrdHeader(Qt.Horizontal, self.fault_prd_table))
        self.fault_prd_table.setHorizontalHeaderLabels(
            ["事件", "中文名", "类型", "语音序号", "灯显-电源灯",
             "灯显-回充灯", "故障码", "备注"])
        hdr = self.fault_prd_table.horizontalHeader()
        hdr.setFixedHeight(50)
        # 点表头只做筛选, 不出现整列/全选的高亮(用户 2026-11-13)
        hdr.setHighlightSections(False)
        hdr.setToolTip("点击「类型」列头可按事件类型筛选")
        self.fault_prd_table.horizontalHeaderItem(2).setToolTip(
            "点击按事件类型筛选")
        hdr.sectionClicked.connect(self._prd_header_clicked)
        for i in range(7):
            hdr.setSectionResizeMode(i, QHeaderView.Interactive)
        hdr.setSectionResizeMode(7, QHeaderView.Stretch)
        # 类型/语音/故障码固定宽度, 保证内容与表头完整显示(用户 2026-11-13)
        for i, width in enumerate((200, 140, 72, 66, 170, 170, 76)):
            self.fault_prd_table.setColumnWidth(i, width)
        self.fault_prd_table.setWordWrap(True)
        # 点单元格不显示蓝色选中背景(用户 2026-11-13)。用调色板而不是
        # 给整个表格设 QSS: 后者会让 300+ 个单元格控件重新 polish, 拖慢建表。
        pal = self.fault_prd_table.palette()
        pal.setColor(QPalette.Highlight, QColor(0, 0, 0, 0))
        pal.setColor(QPalette.HighlightedText, QColor("#1e293b"))
        self.fault_prd_table.setPalette(pal)
        self.fault_prd_table.setItemDelegateForColumn(
            0, _WrapAnywhereDelegate(self.fault_prd_table))
        # 事件名支持右键手动触发; 单元格(含类型)可双击修改
        self.fault_prd_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.fault_prd_table.customContextMenuRequested.connect(
            self._prd_context_menu)
        self.fault_prd_table.setEditTriggers(
            QAbstractItemView.DoubleClicked
            | QAbstractItemView.EditKeyPressed
            | QAbstractItemView.SelectedClicked)
        self.fault_prd_table.verticalHeader().setVisible(False)
        # 按整行滚动, 避免底部露出半截行(用户 2026-11-12)
        self.fault_prd_table.setVerticalScrollMode(
            QAbstractItemView.ScrollPerItem)
        # 备注可能多行: 行高按内容自适应(用户 2026-11-12)
        self.fault_prd_table.verticalHeader().setSectionResizeMode(
            QHeaderView.ResizeToContents)
        self.fault_prd_table.setMinimumHeight(140)
        self.fault_prd_table.setVisible(False)
        self.fault_prd_table.itemChanged.connect(self._prd_row_edited)
        self._set_prd_kind_filter(str(
            self.settings.value("apos/prd_kind_filter", "") or ""))
        # 页面显示/表格宽度变化时重算列宽, 保证故障码/备注不用横向拖动才能看到
        self.fault_prd_table.installEventFilter(self)
        self.fault_page.installEventFilter(self)
        # 表格占满剩余高度(用户 2026-11-12)
        v.addWidget(self.fault_prd_table, 1)
        # 结果信息与日志一起放在最下方(用户 2026-11-12)
        self.fault_verdict = QLabel("未加载事件配置：只显示实测值")
        self.fault_verdict.setStyleSheet("color:#94a3b8;font-size:12px;")
        self.fault_verdict.setWordWrap(True)
        v.addWidget(self.fault_verdict)
        self.fault_hs4_note = QLabel("")
        self.fault_hs4_note.setWordWrap(True)
        self.fault_hs4_note.setStyleSheet("color:#64748b;font-size:11px;")
        v.addWidget(self.fault_hs4_note)
        self.fault_status = QLabel("未触发")
        self.fault_status.setWordWrap(True)
        self.fault_status.setStyleSheet("color:#475569;font-size:12px;")
        v.addWidget(self.fault_status)
        self.fault_result = QLabel("—")
        self.fault_result.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.fault_result.setWordWrap(True)
        self.fault_result.setStyleSheet(
            "font-size:13px;font-weight:600;color:#1e293b;")
        v.addWidget(self.fault_result)
        # 日志放最下方(用户 2026-11-12), 表格占满中间剩余空间
        self.fault_log = QTextEdit()
        self.fault_log.setReadOnly(True)
        self.fault_log.setFixedHeight(128)
        self.fault_log.setLineWrapMode(QTextEdit.WidgetWidth)
        self.fault_log.setStyleSheet(
            "QTextEdit{color:#64748b;font-size:11px;background:#f8fafc;"
            "border:1px solid #e4e8ee;border-radius:4px;padding:4px;}")
        v.addWidget(self.fault_log)
        # 日志默认跟随最新内容; 用户手动滚动后暂停, 20 秒无操作再恢复(2026-11-13)
        self._log_auto_scroll = True
        self._log_idle_timer = QTimer(self)
        self._log_idle_timer.setSingleShot(True)
        self._log_idle_timer.setInterval(20000)
        self._log_idle_timer.timeout.connect(self._resume_log_autoscroll)
        self.fault_log.viewport().installEventFilter(self)
        self.fault_log.installEventFilter(self)
        log_bar = self.fault_log.verticalScrollBar()
        log_bar.sliderPressed.connect(self._log_user_scrolled)
        log_bar.sliderMoved.connect(self._log_user_scrolled)
        log_bar.actionTriggered.connect(self._log_user_scrolled)
        path = str(self.settings.value("apos/fault_event_config", "") or "")
        if path:
            self._load_fault_config(path, quiet=True)
        self._load_prd_rows()
        self._refresh_fault_picks()
        self.connectionChanged.connect(self._on_connection_changed)
        self.connectingChanged.connect(self._on_connecting_changed)

    def _on_connection_changed(self, connected, _label=""):
        """连接状态变了: 刷新 IP 列表, 并把过期的连接提示改成当前状态。

        ★ 用户 2026-11-16: 「无法连接…（正在切换或未就绪）」在连上后不消失。
        只覆盖连接类提示, 不能把「PRD 解析中…」等业务状态冲掉。
        """
        if not connected:
            return
        text = self.fault_status.text().strip()
        if not text or not self._is_connect_status(text):
            return
        ip = self._connected_ip or ""
        self.fault_status.setText(f"已连接 {ip}".strip())
        self.fault_status.setStyleSheet("color:#16a34a;font-size:12px;")

    def _on_connecting_changed(self, connecting, ip):
        if not (connecting and ip):
            return
        text = self.fault_status.text().strip()
        if text and self._is_connect_status(text):
            self.fault_status.setText(f"正在连接 {ip}…")
            self.fault_status.setStyleSheet(
                "color:#b45309;font-size:12px;")

    @staticmethod
    def _is_connect_status(text):
        return (text.startswith(("已连接", "正在连接 ", "正在切换：",
                                 "无法连接 ", "请选择或输入扫地机 IP")))

    def _choose_fault_config(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 AP 平台方案配置",
            getattr(self, "_fault_cfg_path", "") or "",
            "AP 平台方案配置 (*.json);;所有文件 (*)")
        if path:
            self._load_fault_config(path)

    def _load_hs4_project(self, path, quiet=True):
        """PRD 是 xlsx 时顺带解析语音/灯显/故障码词表，用于文案和覆盖判定。"""
        if not str(path or "").lower().endswith((".xlsx", ".xlsm")):
            self._hs4 = None
            return
        try:
            project = hs4_config.load_project(path)
        except Exception as e:
            self._hs4 = None
            if not quiet:
                self.fault_status.setText(f"清单加载失败：{e}")
                self.fault_status.setStyleSheet(
                    "color:#dc2626;font-size:12px;")
            return
        self._hs4 = project

    # ── PRD: AI 解析 事件→语音/灯显/故障码, 表格展示并按它验证 ──
    @staticmethod
    def _show_file_hint(label, path):
        """文件名提示: 只显示 basename, 完整路径放 tooltip(用户 2026-11-27)。"""
        path = str(path or "").strip()
        name = os.path.basename(path) if path else "未选择"
        label.setText(name)
        label.setToolTip(path or "未选择文件")

    def _choose_prd(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 PRD", getattr(self, "_prd_path", "") or "",
            "PRD 文档 (*.xlsx *.xlsm *.docx *.pdf *.txt *.md *.csv);;"
            "所有文件 (*)")
        if not path:
            return
        self._prd_path = path
        self._show_file_hint(self.fault_prd_label, path)
        self.settings.setValue("apos/prd_path", path)
        self._load_hs4_project(path)

    def _open_ai_settings(self):
        """跳到顶部「AI 配置」页(用户 2026-11-12: 配置独立成页)。"""
        show = getattr(self.window(), "_show_page", None)
        if not show:
            return False
        show("ai")
        self.fault_status.setText("请在「AI 配置」页填写解析参数后返回")
        self.fault_status.setStyleSheet("color:#b45309;font-size:12px;")
        return True

    def _ai_config(self):
        one_m = bool(self.settings.value("ai/prd_model_1m", False, type=bool))
        return {
            "base_url": str(self.settings.value("ai/prd_base_url", "") or "")
            or ai_prd.DEFAULT_BASE_URL,
            "api_key": str(self.settings.value("ai/prd_api_key", "") or ""),
            "model": str(self.settings.value("ai/prd_model", "") or ""),
            "max_chars": (ai_prd.MAX_PRD_CHARS_1M if one_m
                          else ai_prd.MAX_PRD_CHARS),
            "max_tokens": int(self.settings.value(
                "ai/prd_max_tokens", ai_prd.DEFAULT_MAX_TOKENS,
                type=int) or ai_prd.DEFAULT_MAX_TOKENS),
        }

    def _ai_event_list(self):
        """给 AI 的事件清单: 有 AP 方案配置时只用配置里的事件。

        内置可模拟事件里有别的项目才有的 identifier, 让 AI 选到它们会导致
        「事件不在 AP 配置」的无效映射(用户 2026-11-17 的 ZY2L 项目)。
        """
        expect = self._fault_expect or {}
        if expect:
            return [{"event": ident,
                     "name": faults.event_name(ident, expect)}
                    for ident in sorted(expect)]
        out = []
        seen = set()
        for p in faults.presets(None):
            out.append({"event": p["event"], "name": p["name"]})
            seen.add(p["event"])
        return out

    def _catalog_entries(self, catalog):
        entries = {}
        for qid, info in (catalog or {}).get("active", {}).items():
            entries.setdefault(int(info.get("final", qid)), []).append(
                (qid, info))
        return entries

    def _fmt_voice(self, qid):
        """语音号按 PRD 实际位数格式化(HS4 三位 001 / ZY2L 四位 0001)."""
        n = int(qid)
        if n >= 1000:
            return str(n)
        width = int((self._prd_catalog or {}).get("voice_width") or 3)
        return f"{n:0{width}d}"

    def _embedder(self):
        """本地向量模型(可被测试注入); 单元测试环境不加载 ONNX 模型。"""
        override = getattr(self, "_embedder_override", None)
        if override is not None:
            return override or None
        if _is_pytest():
            return None
        return embedding.get_embedder()

    def _resolve_memory_event(self, event, hit, text):
        """把旧项目学习库的事件映射到当前 AP 平台事件。

        不同项目 AP 事件 identifier 可能变化; 先按旧事件的 event_name 和
        当前 PRD 关键字/中文名匹配, 找不到再交给 AI(用户 2026-11-16)。
        """
        expect = self._fault_expect or {}
        if not expect or event in expect:
            return event
        # 事件 identifier 不同但中文名相同的, 可以安全迁移; 名称不一致时
        # 不猜, 交给 AI 按 PRD 关键字重新匹配(避免把“拖布机械异常”猜成
        # “开始拖地”这类负优化)。
        old_name = prd_memory.norm((hit or {}).get("event_name"))
        if old_name:
            same = [ev for ev, info in expect.items()
                    if prd_memory.norm((info or {}).get("name") or ev)
                    == old_name]
            if len(same) == 1:
                return same[0]
        return None

    def _memory_prefill(self):
        """按 PRD 的每个 Q 号查记忆库, 返回 (items, 已覆盖的最终语音集合)。

        ★ 不能只按最终语音查: Q024/Q033 复用 Q020 的语音, 但各自的记忆
          映射不同, 只按 voice 取会把它们的映射丢掉(用户 2026-11-13)。
        """
        active = (self._prd_catalog or {}).get("active") or {}
        items = []
        covered = set()
        for qid, info in sorted(active.items(), key=lambda x: int(x[0])):
            try:
                qid_int = int(qid)
            except (TypeError, ValueError):
                continue
            final = int(info.get("final", qid_int))
            key = prd_memory.voice_key(info.get("condition"),
                                       info.get("chinese"),
                                       info.get("text"))
            hit, score = self._prd_memory.best(
                key, threshold=0.82, embedder=self._embedder())
            if not hit or not hit.get("event"):
                continue
            resolved = self._resolve_memory_event(hit["event"], hit, key)
            if not resolved:
                continue
            covered.add(final)
            # 学习库只复用「PRD 关键词 → AP 事件」；语音号/故障码/灯显
            # 随项目变化, 不能从别的项目带过来(用户 2026-11-16)。
            items.append({
                "event": resolved,
                "qid": str(info.get("qid") or f"Q{qid_int:03d}"),
                "voice_key": key,
                "voice": self._fmt_voice(qid_int),
                "reuse_voice": (self._fmt_voice(final)
                                if final != qid_int else ""),
                "voice_text": ai_prd.voice_display_text(
                    info.get("condition"), info.get("chinese"),
                    info.get("text")),
                "kind": ai_prd.normalize_kind(
                    hit.get("kind") or info.get("kind"),
                    hit["event"], info.get("condition"),
                    info.get("chinese")),
                "fault_code": "",
                "fault_push": "",
                # 只有精确命中才继承 user/verified 锁定; 近似命中只是建议,
                # 否则跨项目会把错的映射锁死(用户 2026-11-17)
                "source": (str(hit.get("source") or "memory")
                           if score >= 0.999 else "memory"),
                "note": f"记忆库命中（相似度 {score:.2f}）",
            })
        return items, covered

    def _candidate_events_for(self, missing, top=12):
        """只为未命中的 PRD 语音挑候选事件, 减少 AI 请求量。"""
        all_events = self._ai_event_list()
        emb = self._embedder()
        if not missing or emb is None:
            return all_events
        entries = self._catalog_entries(self._prd_catalog)
        voices = []
        for final in missing:
            infos = entries.get(final)
            if not infos:
                continue
            primary = next((info for qid, info in infos if qid == final),
                           infos[0][1])
            voices.append(" ".join(filter(None, (
                primary.get("condition"), primary.get("chinese"),
                primary.get("text")))))
        if not voices:
            return all_events
        ev_mat = emb.encode([str(e.get("name") or e.get("event"))
                             for e in all_events])
        q_mat = emb.encode(voices)
        if ev_mat is None or q_mat is None:
            return all_events
        picked = set()
        for row in range(len(voices)):
            for idx in (ev_mat @ q_mat[row]).argsort()[::-1][:top]:
                picked.add(int(idx))
        # 向量候选之外, 再补中文关键字/名称最接近的事件; 避免语音号不同的
        # 正确事件被向量召回漏掉(用户 2026-11-16: AI 也按 PRD 关键字匹配)。
        for voice in voices:
            ranked = sorted(
                range(len(all_events)),
                key=lambda i: (
                    self._domain_hits(
                        voice, str(all_events[i].get("event") or ""),
                        str(all_events[i].get("name") or "")),
                    self._name_score(
                        voice, str(all_events[i].get("name") or "")),
                ),
                reverse=True)
            for idx in ranked[:8]:
                ev = all_events[idx]
                if (self._domain_hits(
                        voice, str(ev.get("event") or ""),
                        str(ev.get("name") or "")) > 0
                        or self._name_score(
                            voice, str(ev.get("name") or "")) >= 0.30):
                    picked.add(int(idx))
        return [all_events[i] for i in sorted(picked)]

    def _name_match_items(self, finals):
        """按 AP 事件中文名与 PRD 语音描述匹配, 名称匹配优先于 AI 猜测。"""
        if not finals:
            return []
        entries = self._catalog_entries(self._prd_catalog)
        events = self._ai_event_list()
        items = []
        for final in finals:
            infos = entries.get(final)
            if not infos:
                continue
            primary = next((info for qid, info in infos if qid == final),
                           infos[0][1])
            text = prd_memory.norm(" ".join(filter(None, (
                primary.get("condition"), primary.get("chinese"),
                primary.get("text")))))
            if not text:
                continue
            candidates = []
            for ev in events:
                name = prd_memory.norm(ev.get("name"))
                if len(name) < 3:
                    continue
                if name in text or (len(text) >= 3 and text in name):
                    score = 1.0
                else:
                    import difflib
                    score = difflib.SequenceMatcher(
                        None, name, text).ratio()
                if score >= 0.62:
                    candidates.append((score, ev))
            candidates.sort(key=lambda x: x[0], reverse=True)
            for score, ev in candidates[:3]:
                items.append({
                    "event": ev["event"],
                    "voice": self._fmt_voice(final),
                    "voice_text": ai_prd.voice_display_text(
                        primary.get("condition"), primary.get("chinese"),
                        primary.get("text")),
                    "kind": ai_prd.normalize_kind(
                        primary.get("kind"), ev["event"],
                        ev.get("name"), primary.get("chinese")),
                    "fault_code": "", "fault_push": "",
                    "source": "local",
                    "note": f"事件名称匹配（{score:.2f}）",
                })
        return items

    def _local_event_items(self, missing):
        """本地 bge-small 向量给没有 AI 候选的语音补事件候选。"""
        emb = self._embedder()
        if emb is None or not missing:
            return []
        entries = self._catalog_entries(self._prd_catalog)
        events = self._ai_event_list()
        ev_mat = emb.encode([str(e.get("name") or e.get("event"))
                             for e in events])
        finals, voices = [], []
        for final in missing:
            infos = entries.get(final)
            if not infos:
                continue
            primary = next((info for qid, info in infos if qid == final),
                           infos[0][1])
            voices.append(" ".join(filter(None, (
                primary.get("condition"), primary.get("chinese"),
                primary.get("text")))))
            finals.append(final)
        q_mat = emb.encode(voices)
        if ev_mat is None or q_mat is None:
            return []
        items = []
        for row, final in enumerate(finals):
            scores = ev_mat @ q_mat[row]
            order = scores.argsort()[::-1]
            infos = entries.get(final) or []
            primary = (next((info for qid, info in infos
                             if qid == final), infos[0][1])
                       if infos else {})
            for rank, idx in enumerate(order[:2]):
                score = float(scores[idx])
                if score < (0.55 if rank == 0 else 0.50):
                    break
                ev = events[int(idx)]
                items.append({
                    "event": ev["event"],
                    "voice": self._fmt_voice(final),
                    "voice_text": ai_prd.voice_display_text(
                        primary.get("condition"), primary.get("chinese"),
                        primary.get("text")),
                    "kind": ai_prd.normalize_kind(
                        primary.get("kind"), ev["event"],
                        ev.get("name"), primary.get("chinese")),
                    "fault_code": "", "fault_push": "",
                    "source": "local",
                    "note": f"本地模型候选（{score:.2f}）",
                })
        return items

    def _remember_rows(self, rows, source=None):
        changed = False
        for r in rows:
            key = (r.get("voice_key")
                   or prd_memory.voice_key(r.get("voice_text")))
            event = r.get("event") or ""
            if not key or not event:
                continue
            r["voice_key"] = key
            ev_name = (getattr(self, "_fault_presets", {}).get(event)
                       or faults.event_name(event, self._fault_expect))
            self._prd_memory.put(key, {
                "event": event,
                "event_name": ev_name,
                "voice": r.get("voice") or "",
                "voice_text": r.get("voice_text") or "",
                "kind": r.get("kind") or "",
                "fault_code": r.get("fault_code") or "",
                "fault_push": r.get("fault_push") or "",
            }, source=source or r.get("source") or "ai", save=False)
            changed = True
        if changed:
            self._prd_memory.save()

    def _prd_buttons_reset(self):
        self.fault_prd_parse_btn.setEnabled(True)
        self.fault_prd_parse_btn.setText("解析")
        self.fault_prd_ai_btn.setEnabled(True)
        self.fault_prd_ai_btn.setText("直接用 AI 解析 PRD")

    def _ai_config_or_prompt(self):
        """取 AI 配置; 未配置时跳到 AI 配置页并返回 None。"""
        cfg = self._ai_config()
        if cfg["base_url"] and cfg["model"]:
            return cfg
        if not self._open_ai_settings():
            return None
        cfg = self._ai_config()
        if cfg["base_url"] and cfg["model"]:
            return cfg
        self.fault_status.setText("请先在「AI 配置」里填写解析地址和模型")
        self.fault_status.setStyleSheet("color:#dc2626;font-size:12px;")
        return None

    def _parse_prd(self, force_ai=False):
        """解析 PRD。默认优先学习库, 缺的/首次才用 AI; force_ai 直接全用 AI。"""
        path = str(getattr(self, "_prd_path", "") or "").strip()
        if not path or not os.path.exists(path):
            self.fault_status.setText("请先选择 PRD 文件")
            self.fault_status.setStyleSheet("color:#dc2626;font-size:12px;")
            return
        # 「直接用 AI」需要 AI 配置; 优先学习库时先查库, 全部命中就不需要 AI
        if force_ai and self._ai_config_or_prompt() is None:
            return
        self._prd_force_ai = bool(force_ai)
        self._prd_parse_used_ai = False
        self._prd_parse_cfg = None
        # 新的一次解析会让旧的自动审核结果失效
        self._prd_audit_run_id = int(getattr(
            self, "_prd_audit_run_id", 0) or 0) + 1
        self.fault_prd_parse_btn.setEnabled(False)
        self.fault_prd_ai_btn.setEnabled(False)
        self.fault_prd_parse_btn.setText(
            "AI 解析中…" if force_ai else "准备解析…")
        self.fault_status.setText(
            "正在准备 AI 解析 PRD…" if force_ai else "正在查询学习库…")
        self.fault_status.setStyleSheet("color:#b45309;font-size:12px;")
        self._prd_prep_worker = _PrdPrepWorker(
            self, path, self, prefer_memory=not force_ai)
        self._prd_prep_worker.done.connect(
            lambda data: self._on_prd_prepared(path, data))
        self._prd_prep_worker.failed.connect(self._on_prep_failed)
        self._prd_prep_worker.start()

    def _parse_prd_with_ai(self):
        self._parse_prd(True)

    def _on_prep_failed(self, message):
        self._prd_buttons_reset()
        self._prd_prep_worker = None
        self._prd_parse_used_ai = False
        self.fault_status.setText(f"PRD 准备失败：{str(message)[:200]}")
        self.fault_status.setStyleSheet("color:#dc2626;font-size:12px;")

    def _on_prd_prepared(self, path, data):
        self._prd_prep_worker = None
        catalog = data.get("catalog")
        mem_items = data.get("mem_items") or []
        covered = data.get("covered") or set()
        missing = data.get("missing") or []
        events = data.get("events") or []
        force_ai = bool(data.get("force_ai")
                        or getattr(self, "_prd_force_ai", False))
        self._prd_memory_items = mem_items
        self._prd_memory_covered = covered
        # 学习库命中按 Q 号判断, 不能按最终语音号: Q066 与复用它的 Q138
        # 共用一个语音, 只命中 Q066 时不能当成整库命中跳过 AI(用户 2026-11-17)
        active = (catalog or {}).get("active") or {}
        all_qids = {str(info.get("qid") or f"Q{int(k):03d}")
                    for k, info in active.items()}
        covered_qids = {str(it.get("qid") or "") for it in mem_items
                        if it.get("qid")}
        fully_covered = bool(all_qids) and covered_qids >= all_qids
        if catalog:
            self._save_prd_catalog()
        if not force_ai and catalog and fully_covered:
            self._prd_buttons_reset()
            self._fault_log_line(
                f"学习库已命中全部 {len(all_qids)} 条 PRD 语音，跳过 AI 调用",
                "#16a34a")
            self._on_prd_parsed(mem_items)
            return
        if not events:
            self._prd_buttons_reset()
            self.fault_status.setText("没有可解析的事件")
            self.fault_status.setStyleSheet("color:#dc2626;font-size:12px;")
            return
        cfg = self._ai_config_or_prompt()
        if cfg is None:
            self._prd_buttons_reset()
            return
        self._prd_parse_cfg = cfg
        self._prd_parse_used_ai = True
        self.fault_prd_parse_btn.setText("解析中…")
        prd_n = (len({int(v.get("final", k))
                      for k, v in (catalog or {}).get("active", {}).items()})
                 if catalog else 0)
        prd_text = (f"PRD 语音 {prd_n} 条" if prd_n
                    else f"{len(events)} 个事件")
        self.fault_status.setText("AI 解析中…")
        self.fault_status.setStyleSheet("color:#b45309;font-size:12px;")
        # 解析方式只显示「学习库 / AI解析」(用户 2026-11-17: 不写中转站)
        mode = "AI解析" if force_ai else "优先学习库"
        self._fault_log_line(
            f"PRD 解析开始（{mode}）：{os.path.basename(path)}，"
            f"{prd_text}，未命中 {len(missing) if catalog else len(events)} 条，"
            f"候选事件 {len(events)} 个"
            + (f"，学习库已命中 {len(covered)} 条" if covered else ""),
            "#b45309")
        self._prd_worker = _AiPrdWorker(path, events, cfg, None, self,
                                        voice_catalog=catalog)
        self._prd_worker.done.connect(self._on_prd_parsed)
        self._prd_worker.failed.connect(self._on_prd_failed)
        self._prd_worker.warning.connect(
            lambda msg: self._fault_log_line(msg, "#b45309"))
        self._prd_worker.progress.connect(
            lambda text: self._fault_log_line(text, "#b45309"))
        self._prd_worker.start()

    def _changed_learning_entries(self, before):
        """本次解析真正新增/改动的学习库映射(不含人工/真机/已审核)."""
        out, seen = [], set()
        for row in self._prd_rows:
            key = str(row.get("voice_key")
                      or prd_memory.voice_key(row.get("voice_text")))
            event = str(row.get("event") or "")
            if not key or not event or key in seen:
                continue
            hit = self._prd_memory.exact(key) or {}
            if str(hit.get("source") or "") in (
                    "user", "verified", "ai_review"):
                continue
            if before.get(key) == str(hit.get("event") or event):
                continue
            seen.add(key)
            out.append({
                "key": key,
                "event": event,
                "event_name": str(hit.get("event_name")
                                  or row.get("name") or event),
                "voice": str(row.get("voice") or ""),
                "voice_text": str(row.get("voice_text") or key),
            })
        return out

    def _start_prd_learning_audit(self, entries):
        """AI 解析后自动审核刚整合的映射, 防止错误映射负优化。"""
        if not entries:
            self._fault_log_line(
                "学习库无新增映射，无需自动 AI 审核", "#16a34a")
            return
        cfg = getattr(self, "_prd_parse_cfg", None) or self._ai_config()
        if not cfg.get("base_url") or not cfg.get("model"):
            self._fault_log_line(
                f"学习库已整合 {len(entries)} 条映射；"
                "未配置 AI，跳过自动审核", "#b45309")
            return
        allowed = seed_review.event_catalog(self._fault_expect)
        run_id = int(getattr(self, "_prd_audit_run_id", 0) or 0)
        worker = AiReviewWorker(entries, allowed, cfg, self)
        self._prd_audit_worker = worker
        self._prd_audit_threads.add(worker)
        worker.finished.connect(
            lambda w=worker: self._prd_audit_threads.discard(w))
        worker.done.connect(
            lambda result, rid=run_id: self._on_prd_learning_audited(
                rid, result))
        worker.failed.connect(
            lambda msg, rid=run_id: self._on_prd_learning_audit_failed(
                rid, msg))
        worker.progress.connect(
            lambda text: self.fault_status.setText(text))
        worker.start()
        self._fault_log_line(
            f"学习库已整合 {len(entries)} 条新映射，自动 AI 审核中…",
            "#b45309")
        self.fault_status.setText(
            f"解析完成；学习库 AI 审核中（{len(entries)} 条）…")
        self.fault_status.setStyleSheet("color:#b45309;font-size:12px;")

    def _on_prd_learning_audit_failed(self, run_id, message):
        if run_id != getattr(self, "_prd_audit_run_id", 0):
            return
        self._prd_audit_worker = None
        self._fault_log_line(
            f"学习库自动 AI 审核失败（保留已整合映射）："
            f"{str(message or '未知错误')[:200]}", "#b45309")
        self.fault_status.setText("解析完成；学习库自动审核失败（见日志）")
        self.fault_status.setStyleSheet("color:#b45309;font-size:12px;")

    def _on_prd_learning_audited(self, run_id, result):
        if run_id != getattr(self, "_prd_audit_run_id", 0):
            return
        self._prd_audit_worker = None
        result = result if isinstance(result, dict) else {}
        corrections = result.get("corrections") or []
        checked = int(result.get("checked") or 0)
        errors = result.get("errors") or []
        allowed = seed_review.event_catalog(self._fault_expect)
        corrected = {}
        for corr in corrections:
            key = str(corr.get("key") or "")
            event = str(corr.get("event") or "")
            old = self._prd_memory.exact(key) or {}
            if not key or not event or not old:
                continue
            if str(old.get("source") or "") in ("user", "verified"):
                continue
            if str(old.get("event") or "") == event:
                continue
            val = dict(old)
            val["event"] = event
            val["event_name"] = str(allowed.get(event) or event)
            old_event = str(old.get("event") or "")
            self._prd_memory.put(key, val, source="ai_review",
                                 force=True, save=False)
            corrected[key] = (event, old_event)
        if corrected:
            self._prd_memory.save()
        for row in self._prd_rows:
            corr = corrected.get(str(row.get("voice_key") or ""))
            if not corr:
                continue
            event, old_event = corr
            # 审核期间用户手动改过事件: 不覆盖用户选择
            if old_event and str(row.get("event") or "") != old_event:
                continue
            row["event"] = event
            row["source"] = "ai_review"
            row["fault_code"] = ""
            row["fault_push"] = ""
            row.pop("code_source", None)
            opts = [dict(o) for o in (row.get("event_options") or [])
                    if isinstance(o, dict) and o.get("event") != event]
            opts.insert(0, {
                "event": event,
                "name": str(allowed.get(event) or event),
                "fault_code": "", "fault_push": "",
                "kind": str(row.get("kind") or ""),
                "source": "ai_review",
            })
            row["event_options"] = opts
        if corrected:
            self._enrich_prd_event_options()
            self._apply_prd_names()
            self._fill_prd_table()
            self._save_prd_rows()
        msg = (f"学习库自动 AI 审核完成：检查 {checked} 条，"
               f"修正 {len(corrected)} 条")
        if errors:
            msg += f"；{len(errors)} 批失败：{str(errors[0])[:120]}"
        self._fault_log_line(msg, "#b45309" if errors else "#16a34a")
        self.fault_status.setText(
            "解析完成；学习库已自动审核"
            + ("（部分批次失败，见日志）" if errors else ""))
        self.fault_status.setStyleSheet(
            "color:#b45309;font-size:12px;" if errors
            else "color:#16a34a;font-size:12px;")

    def _on_prd_parsed(self, items):
        self._prd_buttons_reset()
        self._prd_worker = None
        force_ai = bool(getattr(self, "_prd_force_ai", False))
        if not items:
            self._prd_parse_used_ai = False
            self.fault_status.setText("AI 没有解析出可映射的故障事件")
            self.fault_status.setStyleSheet("color:#dc2626;font-size:12px;")
            return
        if getattr(self, "_prd_catalog", None) and not force_ai:
            # 学习库已覆盖的语音不再用本次 AI 结果覆盖
            covered = getattr(self, "_prd_memory_covered", set())
            mapping = {int(k): int(v) for k, v in
                       (self._prd_catalog.get("map") or {}).items()}

            def final_of(it):
                try:
                    q = int(re.sub(r"\D", "", str(it.get("voice") or ""))
                            or -1)
                except ValueError:
                    return -1
                return mapping.get(q, -1)

            covered_qids = {str(m.get("qid") or "")
                            for m in getattr(self, "_prd_memory_items", [])
                            if m.get("qid")}

            def ai_keep(it):
                qid = str(it.get("qid") or "")
                if qid:
                    return qid not in covered_qids
                return final_of(it) not in covered

            ai_items = [it for it in items if ai_keep(it)]
            all_finals = list(self._catalog_entries(self._prd_catalog))
            # 学习库已覆盖的语音直接用学习结果; 只有未覆盖的才做名称匹配,
            # 避免 AP 名称候选覆盖人工确认/已学习的事件(用户 2026-11-13)
            name_finals = [f for f in all_finals if f not in covered]
            name_items = self._name_match_items(name_finals)
            items = (list(getattr(self, "_prd_memory_items", []))
                     + name_items + ai_items)
            have = {final_of(it) for it in items}
            missing = [f for f in all_finals if f not in have]
            items += self._local_event_items(missing)
        if getattr(self, "_prd_catalog", None):
            # 按 PRD 实际语音条目出行(一条最终语音一行, 不出现多余事件)
            self._prd_rows = ai_prd.rows_from_catalog(
                items, self._prd_catalog)
        else:
            self._prd_rows = [dict(it) for it in items]
        self._enrich_prd_event_options()
        for row in self._prd_rows:
            row.setdefault("name", "")
            row["name"] = (row.get("name")
                           or getattr(self, "_fault_presets", {}).get(
                               row.get("event"))
                           or faults.event_name(row.get("event"),
                                                self._fault_expect))
            row["led_home"] = row["led_home_text"] = ""
            row["led_start"] = row["led_start_text"] = ""
        self._dedupe_prd_rows()
        self._apply_prd_names()
        self._apply_note_translations()
        self._sort_prd_rows()
        self._fill_prd_table()
        before = {key: str((val or {}).get("event") or "")
                  for key, val in self._prd_memory.items.items()
                  if isinstance(val, dict)}
        self._remember_rows(self._prd_rows)
        audit_entries = (self._changed_learning_entries(before)
                         if getattr(self, "_prd_parse_used_ai", False)
                         else [])
        self._save_prd_rows()
        self._fault_log_line(
            f"AI 解析完成：{len(self._prd_rows)} 个 PRD 语音事件，"
            f"已整合进学习库 {len(audit_entries)} 条新映射，"
            "灯显请手动选择", "#16a34a")
        self.fault_status.setText("解析完成")
        if not self._led_options():
            self.fault_status.setText("解析完成；灯显下拉为空，请加载 AP 配置")
        self.fault_status.setStyleSheet("color:#16a34a;font-size:12px;")
        used_ai = bool(getattr(self, "_prd_parse_used_ai", False))
        self._prd_parse_used_ai = False
        if used_ai:
            self._start_prd_learning_audit(audit_entries)

    def _on_prd_failed(self, message):
        self._prd_buttons_reset()
        self._prd_worker = None
        self._prd_parse_used_ai = False
        message = str(message or "未知错误")[:300]
        self._fault_log_line(f"AI 解析失败：{message}", "#dc2626")
        self.fault_status.setText("AI 解析失败（详见日志）")
        self.fault_status.setStyleSheet("color:#dc2626;font-size:12px;")

    @staticmethod
    def _event_tip(ev, name=""):
        """候选项悬停提示: 中文名（事件标识），没有中文名时只显示标识。"""
        ev = str(ev or "")
        name = str(name or "").strip()
        if not name or name == ev:
            return ev
        return f"{name}（{ev}）"

    def _fill_prd_row(self, table, i, row, options):
        for j, val in ((1, row.get("name", "")),
                       (3, row.get("voice", "")),
                       (6, row.get("fault_code", "")),
                       (7, row.get("note", ""))):
            item = QTableWidgetItem(str(val))
            item.setToolTip(str(val))
            table.setItem(i, j, item)
        opts = row.get("event_options") or []
        if len(opts) > 1:
            # 事件名太长时在单元格内换行(与中文名一致, 用户 2026-11-13)
            ev_combo = WrappedCombo()
            for opt in opts:
                ev = str(opt.get("event") or "")
                if not ev:
                    continue
                ev_combo.addItem(ev, ev)
                ev_combo.setItemData(
                    ev_combo.count() - 1,
                    self._event_tip(ev, opt.get("name")),
                    Qt.ToolTipRole)
            idx = ev_combo.findData(str(row.get("event") or ""))
            ev_combo.setCurrentIndex(idx if idx >= 0 else 0)
            ev_combo.set_text_alignment(Qt.AlignCenter)
            # 列宽有限时单元格会截断, 悬停看中文名 + 完整事件名
            ev_combo.setToolTip(str(
                ev_combo.itemData(ev_combo.currentIndex(), Qt.ToolTipRole)
                or ev_combo.currentText()))
            ev_combo.currentIndexChanged.connect(
                lambda _i, c=ev_combo: c.setToolTip(str(
                    c.itemData(c.currentIndex(), Qt.ToolTipRole)
                    or c.currentText())))
            ev_combo.currentIndexChanged.connect(
                lambda _i, r=i: self._prd_row_edited(r))
            ev_combo.wrapHeightChanged.connect(
                lambda r=i: QTimer.singleShot(
                    0, lambda: table.resizeRowToContents(r)))
            # 下拉框会吃掉右键, 单独给它挂右键触发菜单
            fm = ev_combo.fontMetrics()
            popup_w = max((fm.horizontalAdvance(ev_combo.itemText(k))
                           for k in range(ev_combo.count())), default=0)
            ev_combo.view().setMinimumWidth(min(popup_w + 40, 720))
            ev_combo.setContextMenuPolicy(Qt.CustomContextMenu)
            ev_combo.customContextMenuRequested.connect(
                lambda pos, c=ev_combo: self._prd_event_combo_menu(c, pos))
            # 宿器负责在行高内垂直居中(表格直接放控件会贴顶)
            host = _CenteredCell(ev_combo, table)
            host.setContextMenuPolicy(Qt.CustomContextMenu)
            host.customContextMenuRequested.connect(
                lambda pos, c=ev_combo: self._prd_event_combo_menu(c, pos))
            table.setCellWidget(i, 0, host)
        else:
            ev = str(row.get("event") or "")
            info = (self._fault_expect or {}).get(ev) or {}
            item = QTableWidgetItem(ev)
            item.setToolTip(self._event_tip(
                ev, info.get("name") or row.get("name")))
            item.setTextAlignment(Qt.AlignCenter)
            table.setItem(i, 0, item)
        type_combo = NoWheelComboBox()
        type_combo.addItems(list(ai_prd.KIND_OPTIONS))
        kind = ai_prd.normalize_kind(row.get("kind"), row.get("event"),
                                     row.get("name"), row.get("voice_text"))
        row["kind"] = kind
        idx = type_combo.findText(kind)
        type_combo.setCurrentIndex(idx if idx >= 0 else 0)
        type_combo.currentIndexChanged.connect(
            lambda _i, r=i: self._prd_row_edited(r))
        table.setCellWidget(i, 2, type_combo)
        # 列顺序: 电源灯在左, 回充灯在右(用户 2026-11-13)
        for col, key in ((4, "led_start"), (5, "led_home")):
            combo = NoWheelComboBox()
            combo.addItem("未配置", "")
            combo.setItemData(0, "未配置", Qt.ToolTipRole)
            for eid, cn in options:
                combo.addItem(cn or eid, eid)
                combo.setItemData(combo.count() - 1,
                                  f"{cn or eid}（{eid}）",
                                  Qt.ToolTipRole)
            idx = combo.findData(str(row.get(key) or ""))
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            combo.setToolTip(str(
                combo.itemData(combo.currentIndex(), Qt.ToolTipRole)
                or combo.currentText()))
            popup_w = max((combo.fontMetrics().horizontalAdvance(
                combo.itemText(k)) for k in range(combo.count())),
                default=0)
            combo.view().setMinimumWidth(min(popup_w + 40, 640))
            combo.currentIndexChanged.connect(
                lambda _i, r=i: self._prd_row_edited(r))
            combo.currentIndexChanged.connect(
                lambda _i, c=combo: c.setToolTip(str(
                    c.itemData(c.currentIndex(), Qt.ToolTipRole)
                    or c.currentText())))
            table.setCellWidget(i, col, combo)

    def _fill_prd_table(self):
        table = self.fault_prd_table
        self._prd_chunk_token = getattr(self, "_prd_chunk_token", 0) + 1
        table.setUpdatesEnabled(False)
        self._prd_filling = True
        table.blockSignals(True)
        table.setRowCount(len(self._prd_rows))
        options = self._led_options()
        for i, row in enumerate(self._prd_rows):
            self._fill_prd_row(table, i, row, options)
        table.blockSignals(False)
        self._prd_filling = False
        self._prd_fill_pending = False
        self._fit_prd_columns()
        table.setVisible(bool(self._prd_rows))
        if self._prd_rows:
            QTimer.singleShot(0, table.resizeRowsToContents)
        self._apply_prd_filter()
        table.setUpdatesEnabled(True)
        table.viewport().update()

    def _fill_prd_table_chunked(self):
        """首次进入故障页: 分批建行, 避免 120 行下拉框一次性卡住几秒。"""
        rows = list(self._prd_rows)
        table = self.fault_prd_table
        if not rows:
            self._fill_prd_table()
            return
        self._prd_chunk_token = getattr(self, "_prd_chunk_token", 0) + 1
        token = self._prd_chunk_token
        self._prd_filling = True
        table.blockSignals(True)
        table.setRowCount(len(rows))
        table.setVisible(True)
        options = self._led_options()
        pos = {"i": 0}
        chunk = 20

        def step():
            if token != getattr(self, "_prd_chunk_token", 0):
                return
            end = min(pos["i"] + chunk, len(rows))
            table.setUpdatesEnabled(False)
            for i in range(pos["i"], end):
                self._fill_prd_row(table, i, rows[i], options)
            table.setUpdatesEnabled(True)
            table.viewport().update()
            pos["i"] = end
            if end < len(rows):
                QTimer.singleShot(0, step)
                return
            table.blockSignals(False)
            self._prd_filling = False
            self._prd_fill_pending = False
            self._fit_prd_columns()
            QTimer.singleShot(0, table.resizeRowsToContents)
            self._apply_prd_filter()
            table.viewport().update()

        QTimer.singleShot(0, step)

    def _prd_kind_menu(self):
        """「类型」列头的筛选下拉(动作可直接测试)。"""
        menu = QMenu(self.fault_prd_table)
        for label, kind in (("全部", ""),
                            *[(k, k) for k in ai_prd.KIND_OPTIONS]):
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(kind == getattr(self, "_prd_kind_filter", ""))
            act.triggered.connect(
                lambda _checked=False, k=kind: self._set_prd_kind_filter(k))
        return menu

    def _prd_header_clicked(self, section):
        """点「类型」列头弹出筛选下拉(用户 2026-11-13)。"""
        table = getattr(self, "fault_prd_table", None)
        if table is None:
            return
        # 表头默认点击会选中整列; 这里统一清掉, 只保留筛选行为
        table.clearSelection()
        sel = table.selectionModel()
        if sel is not None:
            sel.clearCurrentIndex()
        if section != 2:
            return
        hdr = table.horizontalHeader()
        pos = hdr.mapToGlobal(
            QPoint(hdr.sectionViewportPosition(2), hdr.height()))
        self._prd_kind_menu().exec(pos)

    def _set_prd_kind_filter(self, kind):
        kind = str(kind or "")
        if kind not in ("", *ai_prd.KIND_OPTIONS):
            kind = ""
        self._prd_kind_filter = kind
        try:
            self.settings.setValue("apos/prd_kind_filter", kind)
        except Exception:
            pass
        table = getattr(self, "fault_prd_table", None)
        if table is not None:
            hdr = table.horizontalHeader()
            if isinstance(hdr, _PrdHeader):
                hdr.set_kind_filter(kind)
            item = table.horizontalHeaderItem(2)
            if item is not None:
                item.setToolTip("点击按事件类型筛选")
        self._apply_prd_filter()
        table.setUpdatesEnabled(True)

    def _apply_prd_filter(self, *_):
        """按「类型」列头筛选解析列表: 只隐藏行, 不删数据。

        一次性关掉重绘再批量 setRowHidden, 否则 100+ 行逐行触发布局,
        选「全部」取消筛选时会明显卡顿(用户 2026-11-13)。
        """
        table = getattr(self, "fault_prd_table", None)
        if table is None:
            return
        kind = getattr(self, "_prd_kind_filter", "")
        term = str(getattr(self, "_prd_search_text", "") or "").strip().lower()
        table.setUpdatesEnabled(False)
        try:
            for i in range(table.rowCount()):
                w = table.cellWidget(i, 2)
                if isinstance(w, QComboBox):
                    row_kind = w.currentText().strip()
                elif i < len(self._prd_rows):
                    row_kind = str(self._prd_rows[i].get("kind") or "")
                else:
                    row_kind = ""
                matched = (not term
                           or term in self._prd_row_search_text(i))
                hide = (bool(kind) and row_kind != kind) or not matched
                if table.isRowHidden(i) != hide:
                    table.setRowHidden(i, hide)
        finally:
            table.setUpdatesEnabled(True)
        table.viewport().update()

    def _prd_row_search_text(self, row_idx):
        """一行的可搜索文本: 事件/中文名/Q号/语音/备注。"""
        row = (self._prd_rows[row_idx]
               if 0 <= row_idx < len(self._prd_rows) else {})
        parts = [row.get("qid"), row.get("event"), row.get("name"),
                 row.get("voice"), row.get("voice_text"), row.get("note")]
        table = self.fault_prd_table
        w = self._event_combo_at(row_idx)
        if w is not None:
            parts += [w.currentData(), w.currentText()]
        w = table.cellWidget(row_idx, 2)
        if isinstance(w, QComboBox):
            parts.append(w.currentText())
        for col in (1, 3, 6, 7):
            item = table.item(row_idx, col)
            if item is not None:
                parts.append(item.text())
        return " ".join(str(p) for p in parts if p).lower()

    def _search_prd_rows(self, text):
        self._prd_search_text = str(text or "").strip()
        self._apply_prd_filter()
        table = self.fault_prd_table
        term = self._prd_search_text.lower()
        if not term:
            self.fault_search.setToolTip("输入关键字，列表只显示匹配的解析事件")
            return
        first = None
        count = 0
        for i in range(table.rowCount()):
            if not table.isRowHidden(i):
                count += 1
                if first is None:
                    first = i
        self.fault_search.setToolTip(f"匹配 {count} 条")
        if first is not None:
            item = table.item(first, 1) or table.item(first, 0)
            if item is not None:
                table.scrollToItem(item, QAbstractItemView.PositionAtCenter)

    def _fit_prd_columns(self):
        """按可用宽度分配列宽: 8 列尽量在一屏内显示。

        事件/中文名按内容自适应但设上限, 备注列保持 Stretch 吃剩余宽度;
        窗口变窄时先压事件/中文名, 再压灯显列, 避免故障码/备注被挤出屏幕。
        完整的长事件名仍可通过下拉框和 tooltip 查看。
        """
        table = self.fault_prd_table
        if not table.rowCount():
            return
        avail = table.viewport().width()
        if avail < 200:
            avail = table.width() - 2
        if avail < 200:
            return
        fm = table.fontMetrics()
        ev_des = name_des = 0
        for row in self._prd_rows:
            ev_des = max(ev_des, fm.horizontalAdvance(
                str(row.get("event") or "")))
            for opt in (row.get("event_options") or []):
                ev_des = max(ev_des, fm.horizontalAdvance(
                    str(opt.get("event") or "")))
            name_des = max(name_des, fm.horizontalAdvance(
                str(row.get("name") or "")))
        # 事件列收窄, 把更多宽度留给备注(用户 2026-11-13)
        ev_des = max(170, min(ev_des + 34, 320))
        name_des = max(100, min(name_des + 24, 240))
        led_des = 0
        for _eid, cn in self._led_options():
            led_des = max(led_des, fm.horizontalAdvance(cn or _eid))
        if not table.isVisible():
            # 页面还没显示时没有真实视口宽度: 先按内容给足列宽(长事件名
            # 完整显示), 页面真正显示后再按窗口宽度压缩到一屏。
            table.setColumnWidth(0, max(170, min(ev_des + 28, 348)))
            table.setColumnWidth(1, max(100, min(name_des + 24, 264)))
            if led_des:
                w = max(170, min(led_des + 44, 300))
                table.setColumnWidth(4, w)
                table.setColumnWidth(5, w)
            return
        led_w = max(112, min(led_des + 44, 210))
        fixed = 72 + 66 + 76          # 类型 / 语音 / 故障码(固定宽度)
        # 备注至少占 20%（且不低于 180），事件列不再把宽度吃满
        note_min = max(180, int(avail * 0.20))
        left = avail - fixed - 2 * led_w - note_min
        if left < 250:                 # 窄窗口: 灯显/备注再让一点
            led_w = max(96, led_w - 24)
            left = max(220, avail - fixed - 2 * led_w - 96)
        if ev_des + name_des <= left:
            ev_w, name_w = ev_des, name_des
        else:
            ev_w = max(140, int(left * 0.55))
            name_w = max(100, left - ev_w)
            if name_w < 100:
                ev_w, name_w = max(130, left - 100), 100
        table.setColumnWidth(0, ev_w)
        table.setColumnWidth(1, name_w)
        table.setColumnWidth(2, 72)
        table.setColumnWidth(3, 66)
        table.setColumnWidth(4, led_w)
        table.setColumnWidth(5, led_w)
        table.setColumnWidth(6, 76)
        # 第 7 列(备注)是 Stretch, 自动占用剩余宽度

    def eventFilter(self, obj, event):
        # 页面显示或表格宽度变化后重算一次列宽(隐藏时只按内容给宽度)
        if event.type() in (QEvent.Resize, QEvent.Show) and obj in (
                getattr(self, "fault_prd_table", None),
                getattr(self, "fault_page", None)):
            if (obj is getattr(self, "fault_page", None)
                    and event.type() == QEvent.Show
                    and getattr(self, "_prd_fill_pending", False)):
                self._prd_fill_pending = False
                QTimer.singleShot(0, self._fill_prd_table_chunked)
            QTimer.singleShot(0, self._fit_prd_columns)
        # 日志手动滚动/翻页: 暂停自动跟随, 20 秒后恢复。
        # 关窗时控件可能已析构, 这里必须吞掉 RuntimeError(否则事件过滤器
        # 会在已删除的 QTextEdit 上反复报错)。
        try:
            log = getattr(self, "fault_log", None)
            if log is not None:
                if obj is log.viewport() and event.type() == QEvent.Wheel:
                    self._log_user_scrolled()
                elif (obj is log and event.type() == QEvent.KeyPress
                      and event.key() in (Qt.Key_Up, Qt.Key_Down,
                                          Qt.Key_PageUp, Qt.Key_PageDown,
                                          Qt.Key_Home, Qt.Key_End)):
                    self._log_user_scrolled()
        except RuntimeError:
            pass
        try:
            return super().eventFilter(obj, event)
        except RuntimeError:
            return False

    def _led_options(self):
        """灯显下拉：AP 方案事件配置里的灯效标识 + 中文名。"""
        out = []
        seen = set()
        for exp in (self._fault_expect or {}).values():
            for meta in (exp.get("led_names") or {}).values():
                eid = meta.get("effect_id")
                if not eid or eid in seen:
                    continue
                seen.add(eid)
                out.append((eid, meta.get("effect") or ""))
        out.sort(key=lambda x: (x[1] or x[0]))
        return out

    def _event_combo_at(self, row_idx):
        """事件列可能放在垂直居中宿器里, 统一取出真正的 QComboBox。"""
        w = self.fault_prd_table.cellWidget(row_idx, 0)
        if isinstance(w, _CenteredCell):
            return w.child if isinstance(w.child, QComboBox) else None
        return w if isinstance(w, QComboBox) else None

    def _rows_from_table(self):
        table = self.fault_prd_table
        rows = []
        for i in range(table.rowCount()):
            def cell(j):
                item = table.item(i, j)
                return item.text().strip() if item else ""
            ev_w = self._event_combo_at(i)
            event = (str(ev_w.currentData() or "")
                     if ev_w is not None else cell(0))
            # 120 行里多个 PRD 事件可能共用同一语音, 必须按行号取上一版,
            # 不能按 voice 建字典(会把别的 Q 号/事件选项串过来, 用户 2026-11-13)
            prev = self._prd_rows[i] if i < len(self._prd_rows) else {}
            # 未匹配到事件的行也要保留, 不能因为编辑别的单元格被删掉
            if not event and not (prev.get("qid") or prev.get("note")):
                continue
            led = {}
            for col, key in ((4, "led_start"), (5, "led_home")):
                w = table.cellWidget(i, col)
                eid = ""
                text = ""
                if isinstance(w, QComboBox):
                    eid = str(w.currentData() or "")
                    text = w.currentText().strip() if eid else ""
                    if "（" in text:
                        text = text.split("（", 1)[0]
                led[key] = eid
                led[key + "_text"] = text
            voice = cell(3)
            code = cell(6)
            code_source = str(prev.get("code_source") or "")
            if code != str(prev.get("fault_code") or ""):
                code_source = "user" if code else ""
            kind = self._table_kind(i)
            push = prev.get("fault_push", "")
            # 灯显来源: 用户改过下拉 → user; 没改则沿用(实测回填是 actual)
            led_source = str(prev.get("led_source") or "")
            if (led["led_home"] != str(prev.get("led_home") or "")
                    or led["led_start"]
                    != str(prev.get("led_start") or "")):
                led_source = ("user" if (led["led_home"]
                                         or led["led_start"]) else "")
            opts = prev.get("event_options") or []
            # ★ 切换事件只让 AP 中文名跟随变化; 故障码/推送/类型保持原值,
            #   不能因为新选项没带故障码就把已有故障码清空(用户 2026-11-13)
            rows.append({
                "qid": prev.get("qid", ""),
                "event": event,
                "name": (cell(1) or prev.get("name")
                         or self._fault_presets.get(event, event)),
                "voice": voice,
                "voice_key": prev.get("voice_key", ""),
                # 未编辑的事件映射保持原来源; 编辑行由 _prd_row_edited 标 user
                "source": prev.get("source") or "ai",
                "led_home": led["led_home"],
                "led_home_text": led["led_home_text"],
                "led_start": led["led_start"],
                "led_start_text": led["led_start_text"],
                "led_source": led_source,
                "fault_code": code,
                "code_source": code_source,
                "fault_push": push,
                "kind": kind,
                "event_options": opts,
                "note": cell(7),
                # 表格里没有的字段从上一版保留, 不能被一次编辑清掉:
                # 复用语音/PRD 语音文案/Not apply 标记/中文翻译
                **{k: prev[k] for k in (
                    "reuse_voice", "voice_text", "skip_voice", "comment",
                    "prd_content_zh", "prd_content", "prd_voice")
                   if k in prev},
            })
        return rows

    def _prd_context_menu(self, pos):
        """右键事件名称 → 手动触发该事件(用户 2026-11-12)。"""
        table = self.fault_prd_table
        idx = table.indexAt(pos)
        if not idx.isValid() or idx.column() != 0:
            return
        item = table.item(idx.row(), 0)
        w = self._event_combo_at(idx.row())
        event = (str(w.currentData() or "")
                 if w is not None else
                 (item.text().strip() if item else ""))
        if not event:
            return
        menu = QMenu(self)
        act = menu.addAction(f"触发事件：{event}")
        act.triggered.connect(lambda: self._trigger_prd_event(event))
        menu.exec(table.viewport().mapToGlobal(pos))

    def _prd_event_combo_menu(self, combo, pos):
        """事件下拉框上的右键菜单(和事件名称单元格一致): 手动触发。"""
        event = str(combo.currentData() or "")
        if not event:
            return
        menu = QMenu(self)
        act = menu.addAction(f"触发事件：{event}")
        act.triggered.connect(lambda: self._trigger_prd_event(event))
        menu.exec(combo.mapToGlobal(pos))

    def _trigger_prd_event(self, event):
        label = event
        for r in self._prd_rows:
            if r.get("event") == event:
                label = r.get("name") or event
                break
        self._trigger_event(event, label)

    def _effect_cn_text(self, effect_id):
        for exp in (self._fault_expect or {}).values():
            for meta in (exp.get("led_names") or {}).values():
                if meta.get("effect_id") == effect_id:
                    return meta.get("effect") or effect_id
        return effect_id

    def _prd_row_index(self, target):
        """按对象身份找到 PRD 行号(同行内容可能完全相同, 不能用 ==)."""
        for i, row in enumerate(self._prd_rows):
            if row is target:
                return i
        return -1

    def _fill_prd_led_from_actual(self, event, led, prd_row=None):
        """灯显没配置时回填设备实测值供查看/编辑, 但不作为期望值参与判定。

        标记 led_source="actual", _prd_expected 会忽略它并改用 PRD 场景定义,
        避免“期望=实际”永远通过(用户 2026-11-17 代码审查)。
        """
        if not led:
            return
        targets = ([prd_row] if prd_row is not None and any(
            r is prd_row for r in self._prd_rows)
            else [r for r in self._prd_rows if r.get("event") == event])
        filled = False
        for row in targets:
            for dev, key in (("rgb_led__home", "led_home"),
                             ("rgb_led__start", "led_start")):
                if str(row.get(key) or "").strip():
                    continue            # 已配置的不覆盖
                eff = str(led.get(dev) or "").strip()
                if not eff:
                    continue
                row[key] = eff
                row[key + "_text"] = self._effect_cn_text(eff)
                row["led_source"] = "actual"
                filled = True
        if not filled:
            return
        self._save_prd_rows()
        table = self.fault_prd_table
        for row in targets:
            i = self._prd_row_index(row)
            if i < 0:
                continue
            for col, key in ((4, "led_start"), (5, "led_home")):
                combo = table.cellWidget(i, col)
                if not isinstance(combo, QComboBox):
                    continue
                val = row.get(key) or ""
                idx = combo.findData(val)
                combo.blockSignals(True)
                if idx < 0 and val:
                    text = row.get(key + "_text") or val
                    combo.addItem(text, val)
                    combo.setItemData(combo.count() - 1,
                                      f"{text}（{val}）", Qt.ToolTipRole)
                    idx = combo.count() - 1
                combo.setCurrentIndex(idx if idx >= 0 else 0)
                combo.blockSignals(False)

    def _fill_prd_code_from_actual(self, event, code, prd_row=None):
        """故障码为空时用本次实测值回填; 已有值则保留用于判断(用户 2026-11-13)."""
        code = str(code or "").strip()
        if not code:
            return
        targets = ([prd_row] if prd_row is not None and any(
            r is prd_row for r in self._prd_rows)
            else [r for r in self._prd_rows if r.get("event") == event])
        table = self.fault_prd_table
        filled = False
        self._prd_filling = True
        table.blockSignals(True)
        try:
            for row in targets:
                if str(row.get("fault_code") or "").strip():
                    continue
                row["fault_code"] = code
                row["code_source"] = "actual"
                i = self._prd_row_index(row)
                if i < 0:
                    continue
                item = table.item(i, 6)
                if item is None:
                    item = QTableWidgetItem(code)
                    table.setItem(i, 6, item)
                else:
                    item.setText(code)
                item.setToolTip(code)
                filled = True
        finally:
            table.blockSignals(False)
            self._prd_filling = False
        if filled:
            self._save_prd_rows()
            self._fault_log_line(
                f"故障码未配置，已按实测回填：{event} → {code}", "#16a34a")

    def _table_kind(self, row):
        w = self.fault_prd_table.cellWidget(row, 2)
        return str(w.currentText()) if isinstance(w, QComboBox) else ""

    def _sort_prd_rows(self):
        """保持 PRD 语音表顺序(Q 号从小到大), 不按类型拆开(用户 2026-11-13)."""
        def qno(row):
            text = str(row.get("qid") or row.get("voice") or "")
            m = re.search(r"\d+", text)
            return int(m.group(0)) if m else 999999
        self._prd_rows.sort(key=lambda r: (qno(r),
                                           str(r.get("name") or "")))

    def _prd_row_edited(self, source=None):
        if self._prd_filling:
            return
        row_idx = None
        if isinstance(source, QTableWidgetItem):
            row_idx = source.row()
        elif isinstance(source, int):
            row_idx = source
        old_event = None
        if row_idx is not None and 0 <= row_idx < len(self._prd_rows):
            old_event = self._prd_rows[row_idx].get("event")
        self._prd_rows = self._rows_from_table()
        # 手动换事件后, 中文名立即跟随 AP 配置更新(用户 2026-11-13)
        self._apply_prd_names()
        # ★ 只有真正改了“事件”才算用户确认映射; 只改类型/灯显/备注/中文名
        #   不能把整行标成 user, 否则会锁死旧的错误事件映射(用户 2026-11-13)
        if row_idx is not None and 0 <= row_idx < len(self._prd_rows):
            row = self._prd_rows[row_idx]
            changed_event = row.get("event") != old_event
            # 只有改事件才算用户确认映射; 其它编辑不改变来源
            row["source"] = ("user" if changed_event
                             else (row.get("source") or "ai"))
            self._remember_rows([row],
                                source="user" if changed_event else "memory")
        self._save_prd_rows()
        # ★ 只刷新当前行, 不再整表重建/重排: 110 行重建一次约 300ms,
        #   改个类型/灯显都会卡一下(用户 2026-11-13 性能反馈)
        if row_idx is not None and 0 <= row_idx < len(self._prd_rows):
            self._update_prd_row(row_idx)
        self._apply_prd_filter()

    def _update_prd_row(self, row_idx):
        """就地刷新一行(中文名/故障码/备注)，避免整表重建。"""
        if not (0 <= row_idx < len(self._prd_rows)):
            return
        row = self._prd_rows[row_idx]
        table = self.fault_prd_table
        self._prd_filling = True
        table.blockSignals(True)
        try:
            for col, key in ((1, "name"), (6, "fault_code"), (7, "note")):
                val = str(row.get(key) or "")
                item = table.item(row_idx, col)
                if item is None:
                    item = QTableWidgetItem(val)
                    table.setItem(row_idx, col, item)
                elif item.text() != val:
                    item.setText(val)
                item.setToolTip(val)
            if self._event_combo_at(row_idx) is None:
                ev = str(row.get("event") or "")
                item = table.item(row_idx, 0)
                if item is None:
                    item = QTableWidgetItem(ev)
                    item.setToolTip(ev)
                    table.setItem(row_idx, 0, item)
                elif item.text() != ev:
                    item.setText(ev)
                    item.setToolTip(ev)
                item.setTextAlignment(Qt.AlignCenter)
        finally:
            table.blockSignals(False)
            self._prd_filling = False

    def _refresh_prd_table_order(self):
        self._sort_prd_rows()
        self._fill_prd_table()

    def _dedupe_prd_rows(self):
        """同一个最终语音只保留一行(不同事件复用同一语音时合并)。

        PRD 里一条语音可能被多个 AP 事件复用; 全列出来会重复很多"拖布电机
        异常"之类的行。保留一个可触发的事件, 其它事件写进备注。
        """
        out = []
        by_voice = {}
        for r in self._prd_rows:
            voice = str(r.get("voice") or "").strip()
            if not voice:
                out.append(r)
                continue
            name = str(r.get("name") or "")
            side = "左" if "左" in name else ("右" if "右" in name else "")
            # PRD 每个事件一行: 有 qid 时按 qid 区分, 不把复用同一语音的行合并
            key = (str(r.get("qid") or "").strip() or voice, side)
            if key not in by_voice:
                by_voice[key] = r
                out.append(r)
                continue
            keep = by_voice[key]
            alias = r.get("event") or ""
            if alias and alias != keep.get("event"):
                note = keep.get("note") or ""
                extra = f"同一语音事件：{alias}"
                if alias not in note:
                    keep["note"] = (note + "；" + extra).strip("；")
            # 优先保留厂商可模拟的那条
            if (not faults.has_simulation(keep.get("event"))
                    and faults.has_simulation(alias)):
                keep["event"] = alias
                keep["name"] = r.get("name") or keep.get("name")
                keep["voice_text"] = (r.get("voice_text")
                                      or keep.get("voice_text"))
                keep["kind"] = r.get("kind") or keep.get("kind")
                keep["fault_code"] = (r.get("fault_code")
                                      or keep.get("fault_code"))
                keep["fault_push"] = (r.get("fault_push")
                                      or keep.get("fault_push"))
        self._prd_rows = out

    #: PRD 触发条件关键词 → 同语音下应优先的事件(2026-11-13)
    #: 语义优先覆盖: PRD 复用语音与 AP 语音不一致, 但事件语义明确的条目。
    #: 仍保留同语音 AP 事件作为下拉候选, 并在备注里标出语音冲突(用户 2026-11-13)。
    #: 会让设备真实关机/重启的事件: 触发后后续事件不会再有语音/灯显
    # 只跳过会真实断电的关机事件; 恢复出厂设置验证不会实际生效, 不跳过
    _DESTRUCTIVE_EVENTS = {
        "shutdown", "power_off", "low_bat_poweroff", "bat_need_poweroff",
    }
    #: 批量测试事件间隔: 等设备日志刷完再触发下一条(用户 2026-11-16)
    _BATCH_GAP_MS = 2500

    #: 这些场景语义唯一, 候选只保留目标事件, 不再混入同类事件
    _EXCLUSIVE_SCENARIO_KEYWORDS = {
        "轮子悬空": "pickup",
        "轮子离地": "pickup",
        "离地开关": "pickup",
        "离地检测": "pickup",
        "定位机器人": "find_robot",
        "取下拖布": "mop_out",
        "开始充电": "charging",
        "上桩充电": "charging",
    }

    #: 场景词 → 精确事件白名单（按顺序取第一个命中的场景）。
    #: 同一语音/同一大类下往往有十几个事件, 只靠关键词会混进无关候选
    #: (用户 2026-11-17: 重置 WiFi 不能和联网成功/密码错误/联网失败互为候选)。
    _SCENARIO_ALLOW = (
        # 网络/配网: 每个场景只对应自己的事件
        ("重置wifi", ("wifi_reset",)),
        ("密码错误", ("incorrect_wifi_passwd",)),
        ("配网失败", ("distribution_network_timeout",)),
        ("配网成功", ("config_wifi_success",)),
        ("断网重连", ("config_wifi_begin",)),
        ("开始连接路由器配网", ("config_wifi_begin",)),
        # 充电/回充状态
        ("充电时按回充按键", ("robot_in_base_station",)),
        ("充电中关机", ("try_poweroff_on_charger",)),
        ("再次触发回充按键", ("back_charger_resume",)),
        ("暂停回充", ("back_charger_paused", "station_work_back_charger_pause",
                     "work_status_back_charge_pause")),
        ("上桩失败", ("infrared_guidance_back_charger_fail",
                     "charger_slow_err")),
        ("回充失败", ("back_charger_fail", "back_charger_lost_pose",
                     "clean_not_find_single_back_charger_fail")),
        # 电量
        ("清洁中电量不足", ("low_battery_find_charger",
                           "low_battery_to_clean")),
        ("低电量启动清洁", ("low_battery_to_clean",)),
        ("充电桩上启动清洁", ("low_battery_to_clean_on_charger",)),
        ("电量过低", ("bat_need_poweroff", "low_bat_poweroff")),
        # 尘盒/尘袋/集尘
        ("无尘盒触发", ("no_dust_box_water_box_do_task",)),
        ("非尘满取出尘盒", ("dust_box_water_box_out",)),
        ("尘满取出尘盒", ("garbage_box_full_out",)),
        ("尘盒已满", ("garbage_box_full",)),
        ("集尘中信号", ("collect_dust_start",)),
        # 先匹配"集尘完成开始充电", 再匹配宽泛的"集尘完成"
        ("集尘完成开始充电", ("charging",)),
        ("集尘完成", ("collect_dust_done",)),
        ("集尘中断", ("not_finish_dust_collect",)),
        ("尘袋不在位", ("collect_dust_no_bag",)),
        ("集尘宝风机", ("collect_dust_no_fan",)),
        # 风机(基站风机/开路短路要排在宽泛的“风机异常”之前)
        ("风机开路或短路", ("error_kit_fan_err",)),
        ("风机异常", ("error_fan_stuck",)),
        # 拖布
        ("插入拖布", ("mop_in", "double_rotate_rag_in",
                     "double_rotate_rag_in_non_motion")),
        ("旋转拖布", ("error_double_rotate_rag_motor_open_short_circuit",
                     "error_kit_l_rotate_rag_motor_err",
                     "error_kit_r_rotate_rag_motor_err")),
        ("拖布支架卡住", ("error_double_rotate_rag_motor_stuck",
                         "error_l_rotate_rag_motor_stuck",
                         "error_r_rotate_rag_motor_stuck")),
        # 水
        ("主机清水箱缺水", ("error_water_box_empty",)),
        ("检测到基站未安装清水箱", ("base_station_clean_water_tank_not_in",
                                 "base_station_clean_water_tank_out",
                                 "station_clean_water_tank_in")),
        ("请安装清水箱", ("base_station_clean_water_tank_not_in",
                       "base_station_clean_water_tank_out",
                       "station_clean_water_tank_in")),
        ("沿墙线激光被遮挡", ("line_laser_wall_follow_dirty",
                           "error_psd_dirty")),
        # ZY2L: 基站/主机水箱取出-装回是不同事件
        ("把清水箱从基站中取出", ("base_station_clean_water_tank_out",)),
        ("把清水箱装回基站", ("station_clean_water_tank_in",)),
        ("把污水箱从基站中取出", ("station_sewage_tank_out",)),
        ("把污水箱装回基站", ("station_sewage_tank_in",)),
        ("主机清水箱排空完成", ("empty_robot_water_tank_done",)),
        ("开始排空主机清水箱", ("empty_robot_water_tank_start",)),
        ("检测到污水箱没有安装好", ("base_station_sewage_tank_not_in",)),
        ("基站清水箱未安装", ("base_station_clean_water_tank_not_in",
                            "base_station_clean_water_tank_out")),
        # 基站泵/阀/烘干
        ("基站清水泵短路", ("station_clean_water_pump_short",)),
        ("基站清水泵开路", ("station_clean_water_pump_open",)),
        ("基站污水泵短路", ("station_sewage_pump_short",)),
        ("基站污水泵开路", ("station_sewage_pump_open",)),
        ("基站烘干风扇过流", ("dry_mop_fan_short",)),
        ("基站烘干风扇开路", ("dry_mop_fan_open", "dry_mop_fan_error")),
        ("手动移出基站", ("dry_mop_leave_charge",)),
        ("手动启动烘干", ("dry_mop_start",)),
        ("手动结束烘干", ("dry_mop_done",)),
        # 洗/回洗拖布
        ("手动停止清洗拖布", ("stop_wash_mop",)),
        ("暂停回洗拖布", ("back_wash_paused",)),
        ("任务中回洗拖布", ("go_back_mop_after_clean",)),
        # 定位/建图/清扫
        ("定位中", ("location_start",)),
        ("建图完成", ("explore_map_finish_back_charger",
                     "explore_map_finish_clean")),
        ("继续快速建图", ("explore_map_resume",)),
        ("清洁区域无法到达", ("clean_cannot_arrive",)),
        # 传感器/电池
        ("机器倾斜", ("error_tilt_do_task",)),
        ("倾斜面上启动", ("error_tilt_do_task",)),
        ("悬崖传感器故障", ("cliff_sensor_error",)),
        ("跌落传感器", ("error_drop",
                       "error_self_test_monitor_drop_false_trigger")),
        ("前方激光传感器被遮挡", ("error_front_wall_dirty",)),
        ("沿墙传感器被遮挡", ("line_laser_wall_follow_dirty",)),
        ("沿墙传感器开路", ("error_psd_dirty",)),
        ("雷达有效数据少", ("error_kit_lidar_err", "error_kit_lidar_obscured",
                           "line_laser_avoid_obstacle_dirty")),
        ("雷达开路", ("error_kit_lidar_err", "error_kit_lidar_speed")),
        ("水泵开路或短路", ("error_kit_pwm_water_pump_err",)),
        ("电池开路或短路", ("battery_disconnect",)),
        ("电池异常", ("battery_disconnect",)),
        ("电池温度", ("robot_battery_temperature_error",
                     "error_battery_high_temperture",
                     "error_battery_low_temperture")),
        # 语音包/童锁/遥控/音量
        ("更换不同语音包", ("apply_voice_package_success",)),
        ("触发童锁", ("child_lock_on",)),
        ("解除童锁", ("child_lock_off",)),
        ("童锁打开后", ("child_lock_is_on",)),
        ("童锁已激活", ("child_lock_on",)),
        ("童锁已解除", ("child_lock_off",)),
        ("按键已锁定", ("child_lock_is_on",)),
        ("开始遥控", ("remote_ctrl_start",)),
        ("退出遥控", ("remote_ctrl_stop",)),
        ("设置音量", ("change_volum",)),
        ("调节吸力", ("change_fan_level",)),
        ("网络连接成功", ("config_wifi_success",)),
        ("充电完毕开始断点续扫", ("charge_ok_resume_clean",)),
        ("跳过用户设定区域", ("need_skip_area_clean",)),
        ("APP触发回充", ("back_charger_start",)),
        ("童锁开启状态下，点击", ("child_lock_is_on",)),
        ("LDS故障", ("error_kit_lidar_err", "error_kit_lidar_speed",
                    "error_kit_lidar_stuck")),
        # ZY2L 当前配置没有"结束烘干"事件(只有开始烘干)
        ("结束烘干任务", ()),
        ("启动局部清扫", ("start_curpoint_clean",)),
        # 其它项目(ZY2L)的同义/细分场景
        ("即将自动关机", ("low_bat_poweroff",)),
        ("即将关机", ("low_bat_poweroff",)),
        ("开机", ("system_start",)),
        ("清洗中，控制机器操作",
         ("station_self_clean", "do_not_operate_during_station_working")),
        ("建图中，APP点击", ("explore_map_finish_clean",
                           "explore_map_finish_back_charger")),
        # 在桩低电启动清扫(PRD「低电启动任务（在桩）」): 对应 AP 的
        # low_battery_to_clean_on_charger(声音 0260), 不是充电低电提醒
        # (用户 2026-11-17)。只认「在桩」, 「不在桩」不能套这个事件。
        ("低电启动任务（在桩）", ("low_battery_to_clean_on_charger",)),
        ("低电启动任务(在桩)", ("low_battery_to_clean_on_charger",)),
        ("仍低电", ("app_sweeper_enter_low_battery_charging",)),
        ("越障辅助轮", ("error_wheel_lift_error",
                      "error_wheel_lift_left_open_short_circuit",
                      "error_wheel_lift_right_open_short_circuit")),
        ("沿墙线激光模组", ("error_kit_line_laser_error", "error_psd_dirty")),
        ("机器人执行任务", ("clean_paused", "back_charger_paused",
                         "explore_map_paused",
                         "clean_paused_by_charge_key")),
        # ZY2L 当前配置没有对应事件 → 保持「未找到」，不要硬套无关事件
        ("尘盖未盖好", ()),
        ("视觉传感器开路", ()),
        ("基座发生异常", ()),
        ("请安装主机导水槽", ("sewage_tank_not_in_place",)),
        # PTR_MR(v12) ZY2L 细分场景
        # 排水时清洗盘/拖布状态(PRD 0922/0923): 没清洁板→托盘不在位排水,
        # 装了滚筒拖布→拖布在位排水(用户 2026-11-17 新版 PRD)
        ("没有拖布清洁板", ("drainage_water_without_base_station_tray",)),
        ("安装了滚筒拖布", ("drainage_water_with_mop",)),
        # 基站清洗盘「使用24次提醒清洗」, AP 当前没有对应事件
        ("清洗盘清洗提醒", ()),
        # 尘盒不在位不能套到清洗盘(用户 2026-11-17)
        ("尘盒不在位", ("no_dust_box_water_box_do_task",)),
        ("主机被困，无法移动脱困", ("error_physical_trapped",)),
        # 划区全部不可达(0480 的普通重定位失败仍是 clean_lost_pose)
        ("重定位失败或其他原因", ("clean_cannot_arrive",)),
        ("启动拖布烘干", ("dry_mop_start",)),
        # 防垢模块提示: AP 平台没有对应事件(用户 2026-11-17)
        ("防垢模块", ()),
        ("스케일 방지", ()),
        # 充电状态下再触发回充: PRD 是「我已经在基座了」, AP 没有对应事件
        # (不能套继续回充 back_charger_resume, 用户 2026-11-17)
        ("充电状态下，再触发回充", ()),
        ("充电状态下再触发回充", ()),
        ("滚筒抬升", ("error_wheel_lift_error",)),
        ("滚筒外扩", ("error_drum_telescopic_motor_stuck",)),
        ("未从清洗站开始拖地", ("mop_clean_start",)),
        ("结束清扫任务", ("stop_clean",)),
        ("电量不足，开始回充", ("low_battery_find_charger",)),
        ("再触发回充", ()),
        ("打开童锁功能", ("child_lock_on",)),
        ("清水箱水量不足", ("base_station_clean_water_tank_empty",)),
        # AP 当前没有恢复出厂失败事件; 轮子攀爬辅助只有抬升电机事件
        ("恢复出厂默认设置失败", ()),
        ("轮子攀爬", ("error_wheel_lift_error",)),
        # AP 配置里没有对应事件 → 标未匹配, 不要硬套无关事件
        ("APP有新功能更新", ()),
        ("主机回充传感器短路", ()),
        ("sewage filter cover is removed", ("sewage_water_tank_out",)),
        ("sewage filter cover is installed", ("sewage_water_tank_in",)),
        ("melody only", ()),
        ("溢水回充超过", ("roller_clean_tray_missing_or_overflow",
                        "dock_water_recovery")),
        ("high temperature", ("base_station_temperature_sensor_error",)),
        ("건조 온도", ("base_station_temperature_sensor_error",)),
        ("烘乾機溫度", ("base_station_temperature_sensor_error",)),
        ("清洗滚筒时，基站清水箱空",
         ("base_station_clean_water_tank_empty",
          "base_station_sewage_tank_full")),
        ("滚筒清洁完成", ("clean_mop_done",)),
        ("升级中", ("opt_during_upgrade",)),
        ("不满足升级条件", ("cannot_upgrade",)),
        ("固件升级失败", ("firmware_upgrade_fail",)),
        ("休眠唤醒", ("exit_dormant",)),
        ("主动关机", ("shutdown", "bat_need_poweroff",
                     "low_bat_poweroff", "power_off")),
        ("边刷伸缩被卡住", ("error_side_brush_telescoping",)),
        ("边刷摆臂", ("error_side_brush_telescoping",)),
    )

    #: Q号级语义覆盖: 必须同时命中关键词才生效 —— 不同项目里同一个 Q 号
    #: 含义完全不同(HS4 Q100=休眠唤醒, ZY2L Q100=沿墙激光遮挡)。
    _SEMANTIC_EVENT_OVERRIDES = {
        "Q131": (("雷达", "激光"),
                 ("error_kit_lidar_err", "error_kit_lidar_obscured")),
        # 机器休眠唤醒 → 退出休眠事件(AP 实际播报 Q100, PRD 复用的是 Q085)
        "Q100": (("休眠唤醒",), ("exit_dormant",)),
    }

    @classmethod
    def _forced_events_for(cls, qid, text):
        item = cls._SEMANTIC_EVENT_OVERRIDES.get(str(qid or ""))
        if not item:
            return ()
        keywords, events = item
        if any(k in str(text or "") for k in keywords):
            return tuple(events)
        return ()

    _START_EVENT_KEYWORDS = (
        ("全局", "total_clean_start"),
        ("定点", "point_clean_start"),
        ("房间", "area_clean_start"),
        ("区域", "area_clean_start"),
        ("划区", "area_clean_start"),
        ("选区", "auto_area_clean_start"),
        ("拖地", "mop_clean_start"),
        ("累计", "time_total_clean_start"),
        ("探索", "explore_map_finish_clean"),
        ("建图", "explore_map_finish_clean"),
    )

    #: 细分场景 → 精确 AP 事件。用于区分“同语音/同大类”的事件:
    #: 例如轮子悬空是离地检测 pickup, 不是低电量; 边刷摆臂属于伸缩边刷。
    _SCENARIO_EVENT_KEYWORDS = (
        ("轮子悬空", "pickup"),
        ("轮子离地", "pickup"),
        ("离地开关", "pickup"),
        ("离地检测", "pickup"),
        ("雷达被遮挡", "error_kit_lidar_obscured"),
        ("雷达遮挡", "error_kit_lidar_obscured"),
        ("雷达无数据", "error_kit_lidar_err"),
        ("雷达有效数据少", "error_kit_lidar_err"),
        ("雷达转速", "error_kit_lidar_speed"),
        ("雷达控速", "error_kit_lidar_speed"),
        ("转速异常", "error_kit_lidar_speed"),
        ("边刷摆臂", "error_side_brush_telescoping"),
        ("边刷伸缩", "error_side_brush_telescoping"),
        ("伸缩被卡住", "error_side_brush_telescoping"),
        ("伸缩无法收回", "error_side_brush_telescoping_in"),
        ("伸缩无法外摆", "error_side_brush_telescoping_out"),
        ("上桩失败", "back_charger_fail"),
        ("上桩超时", "back_charger_fail"),
        ("机器被困", "error_physical_trapped"),
        ("机器倾斜", "error_tilt_do_task"),
        ("悬崖", "error_drop"),
        ("地检", "error_drop"),
        ("沿墙传感器", "error_psd_dirty"),
        ("正前方避障", "error_front_wall_dirty"),
        ("频繁集尘", "app_sweeper_keyboard_press_frequently"),
        ("集尘过于频繁", "app_sweeper_keyboard_press_frequently"),
        ("集尘中断", "not_finish_dust_collect"),
        ("烘干风扇过流", "dry_mop_fan_short"),
        ("烘干风机过流", "dry_mop_fan_short"),
        ("烘干风扇开路", "dry_mop_fan_open"),
        ("烘干风机开路", "dry_mop_fan_open"),
        ("清洁液", "station_detergent_not_enough"),
        # 开始洗拖布实际走加水开始事件; 开始加水场景不再反选开始洗拖布
        ("开始拖布清洗", "add_water_start"),
        ("基站开始拖布清洗", "add_water_start"),
        ("开始洗拖布", "add_water_start"),
        ("主刷卡住", "error_middle_brush_stuck"),
        ("滚刷卡住", "error_middle_brush_stuck"),
    )

    #: 中文含义关键词 → AP 事件标识/名称里的判别词。不同项目语音号会变,
    #: 这里只认中文语义, 用于事件匹配的默认选择(用户 2026-11-13)。
    _MATCH_KEYWORDS = (
        ("音量", ("volum",)), ("档位", ("level", "fan")),
        ("风机", ("fan",)), ("吸力", ("fan",)),
        ("雷达", ("lidar",)), ("激光", ("lidar", "laser")),
        ("转速", ("speed",)), ("控速", ("speed",)),
        ("尘盒", ("dust_box", "garbage_box")),
        ("尘袋", ("dust",)), ("尘满", ("dust", "garbage")),
        ("清水", ("clean_water", "water")),
        ("污水", ("sewage", "water")), ("水箱", ("tank", "water")),
        ("拖布", ("mop", "rag")), ("抹布", ("mop", "rag")),
        ("边刷", ("brush",)), ("滚刷", ("brush",)),
        ("中刷", ("brush",)),
        ("回充", ("charg",)), ("充电", ("charg",)),
        ("休眠", ("dormant",)), ("唤醒", ("dormant",)),
        ("集尘", ("collect_dust", "dust")),
        ("预约", ("time_to_work", "time_")),
        ("完成", ("finish",)), ("暂停", ("pause",)),
        ("继续", ("resume", "clean_location_success")),
        ("定位", ("location", "pose", "relocation")),
        ("禁区", ("forbid",)), ("升级", ("upgrade",)),
        ("童锁", ("child_lock",)), ("配网", ("wifi", "network")),
        ("wifi", ("wifi",)),
        # —— 细分场景判别词(用户 2026-11-13) ——
        ("离地", ("pickup", "lift")), ("悬空", ("pickup", "lift")),
        ("轮子", ("wheel", "pickup")),
        ("摆臂", ("side_brush_err", "brush_err")),
        ("伸缩", ("telescoping",)), ("外扩", ("telescoping",)),
        ("收回", ("telescoping",)),
        ("上桩", ("back_charger",)), ("入桩", ("back_charger",)),
        ("被困", ("trapped",)), ("倾斜", ("tilt",)),
        ("地检", ("drop", "cliff")), ("悬崖", ("drop", "cliff")),
        ("沿墙", ("psd", "wall_follow")),
        ("雷达遮挡", ("obscured",)), ("无数据", ("lidar",)),
        ("数据少", ("lidar",)),
        ("正前方", ("front_wall", "front")),
        ("避障传感器", ("front_wall", "line_laser")),
        ("线激光", ("line_laser",)),
        ("未安装", ("not_install", "no_")),
        ("不在位", ("not_in", "no_")),
        ("频繁", ("frequently",)), ("中断", ("not_finish", "fail")),
        ("烘干", ("dry_mop",)), ("风扇", ("fan",)),
        ("过流", ("short",)), ("开路", ("open",)),
        ("短路", ("short",)),
        ("清洁液", ("detergent",)), ("余量", ("not_enough",)),
        ("不足", ("not_enough",)),
        ("清洁", ("clean",)), ("清扫", ("clean",)),
        ("电池", ("battery",)), ("温度", ("temperature",)),
        ("水泵", ("water_pump",)), ("电磁阀", ("water_valve",)),
        ("清洗盘", ("clean_tray",)), ("超声波", ("ultrasonic",)),
        ("地毯", ("rug",)), ("语音包", ("voice_package",)),
        ("关机", ("poweroff", "shutdown")),
        ("清洗", ("wash", "clean_mop")),
        ("加水", ("add_water",)),
        ("主轮", ("wheel",)), ("左轮", ("left_wheel",)),
        ("右轮", ("right_wheel",)), ("轮组", ("wheel",)),
        ("主刷", ("middle_brush", "brush")),
        ("滚刷", ("middle_brush", "brush")),
        ("边刷卡住", ("side_brush_stuck", "brush_stuck")),
    )

    #: 候选必须命中的对象词。避免“边刷开路”把风机/轮组/水泵的开路事件
    #: 也当成相关候选(它们都含“开路/短路”字样, 用户 2026-11-13)。
    _OBJECT_KEYWORDS = (
        ("风机", ("fan",)), ("吸力", ("fan",)),
        ("雷达", ("lidar",)), ("激光", ("lidar", "laser")),
        ("边刷", ("side_brush",)), ("中刷", ("middle_brush",)),
        ("主刷", ("middle_brush",)),
        ("滚刷", ("middle_brush",)),
        ("拖布", ("rag", "mop")), ("抹布", ("rag", "mop")),
        ("拖地", ("mop",)), ("轮组", ("wheel",)), ("轮子", ("wheel",)),
        ("主轮", ("wheel",)), ("左轮", ("wheel",)), ("右轮", ("wheel",)),
        ("水泵", ("water_pump",)), ("清水", ("clean_water",)),
        ("污水", ("sewage",)), ("水箱", ("water_box", "tank")),
        ("尘盒", ("dust_box", "garbage_box")),
        ("尘袋", ("dust", "bag")), ("电磁阀", ("water_valve",)),
        ("烘干", ("dry_mop",)), ("风扇", ("fan",)),
        ("悬崖", ("drop", "cliff")), ("地检", ("drop", "cliff")),
        ("沿墙", ("psd", "wall_follow")),
        ("超声波", ("ultrasonic",)), ("电池", ("battery",)),
        ("清洁液", ("detergent",)), ("清洗盘", ("clean_tray",)),
        ("充电", ("charg",)), ("回充", ("charg",)),
        ("集尘", ("collect_dust", "dust")),
        ("清扫", ("clean",)), ("清洁", ("clean",)),
        ("休眠", ("dormant",)), ("被困", ("trapped",)),
        ("倾斜", ("tilt",)), ("遥控", ("remote",)),
        ("升级", ("upgrade",)), ("wifi", ("wifi", "network")),
        ("网络", ("wifi", "network")),
    )

    #: 动作/故障形态词: 有这些词时, 候选必须至少命中一个
    _ACTION_KEYWORDS = (
        ("未安装", ("not_install", "no_mop", "no_dust", "out")),
        ("不在位", ("not_in", "no_")),
        ("装回", ("install", "_in", "new_")),
        ("放回", ("install", "_in", "new_")),
        ("取出", ("out", "remove")), ("取下", ("out", "remove")),
        ("暂停", ("pause",)), ("继续", ("resume",)),
        ("开始", ("start",)),
        ("完成", ("finish", "done")),
        ("成功", ("success", "connect")),
        ("卡住", ("stuck",)), ("堵转", ("stuck", "block")),
        ("开路", ("open",)), ("短路", ("short",)), ("过流", ("short",)),
        ("插入", ("install", "_in")), ("移出", ("leave", "out")),
        ("结束", ("finish", "done")),
        ("脏污", ("dirty", "obscured")), ("遮挡", ("dirty", "obscured")),
        ("温度", ("temperature", "temperture", "temp")),
        ("失败", ("fail", "error")), ("异常", ("error", "err", "fault")),
        ("中断", ("not_finish", "fail")),
        ("不足", ("not_enough", "empty", "low")), ("满", ("full",)),
        ("升级", ("upgrade", "ota")),
        ("配网", ("wifi", "network")), ("联网", ("connect", "network")),
        ("回充", ("charg",)), ("充电", ("charg",)),
        ("定位", ("location", "pose", "relocation", "find")),
        ("建图", ("explore_map",)),
        ("清洗", ("wash", "clean_mop")),
        ("回洗", ("back_wash", "go_back")),
        ("集尘", ("collect_dust",)),
        ("禁区", ("forbid",)), ("泵", ("pump",)), ("阀", ("valve",)),
        ("清洁液", ("detergent",)),
    )
    _MAX_CANDIDATES = 6
    #: 故障形态词: "过流异常"/"开路短路"这类描述是同一个故障的不同说法,
    #: 候选命中任意一个即可; 进程动作(开始/暂停/取出…)仍要求全部命中。
    _FAULT_FORM_ACTIONS = frozenset({
        "开路", "短路", "过流", "异常", "失败", "卡住", "堵转",
        "脏污", "遮挡", "温度", "中断", "不足", "满",
    })

    @classmethod
    def _domain_hits(cls, text, event, name=""):
        """PRD 中文关键词与 AP 事件标识/名称的语义命中数。"""
        t = str(text or "").lower()
        ev = str(event or "").lower()
        nm = str(name or "").lower()
        hits = 0
        for keyword, tokens in cls._MATCH_KEYWORDS:
            if keyword not in t:
                continue
            if any(tok in ev for tok in tokens) or keyword in nm:
                hits += 1
        return hits

    @classmethod
    def _scenario_allow(cls, text):
        """场景词 → 精确事件白名单；没有命中返回 None（走通用过滤）。"""
        t = str(text or "").lower()
        for keyword, targets in cls._SCENARIO_ALLOW:
            if keyword.lower() in t:
                return tuple(targets)     # 保留顺序: 第一个是首选
        return None

    @staticmethod
    def _ap_sound_no(info):
        snd = (info or {}).get("sound")
        if isinstance(snd, dict):
            snd = snd.get("identifier")
        return hs4_config.voice_no(snd)

    @classmethod
    def _candidate_related(cls, text, event, name=""):
        """候选是否与 PRD 文本有语义关联(关键词或中文名相似度).

        同 sound 的事件可能多达十几个且语义无关; 候选池不能无限放大,
        只保留当前事件、强制事件和有语义关联的候选(用户 2026-11-13)。
        """
        return (cls._domain_hits(text, event, name) > 0
                or cls._name_score(text, name or event) >= 0.30)

    @classmethod
    def _priority_object_filter(cls, text):
        """低电/禁区等状态场景只能出现对应状态事件，不能拿清扫任务凑候选。"""
        t = str(text or "")
        low_bat = any(k in t for k in ("电量", "低电"))
        working = any(k in t for k in ("清洁", "清扫", "工作", "任务"))
        if low_bat and working:
            return (("battery", "low_bat", "charg", "poweroff"),
                    ("电量", "低电", "充电", "关机"))
        if "禁区" in t:
            return (("forbid",), ("禁区",))
        return None

    @staticmethod
    def _is_water_only(text):
        """PRD 只是“开始加水”时, 不把“开始洗拖布”当候选。"""
        t = str(text or "")
        return ("加水" in t
                and not any(k in t for k in ("洗拖布", "拖布清洗",
                                             "清洗拖布")))

    @staticmethod
    def _is_start_context(text, current):
        """PRD 是开始清扫类时, 保留所有同语音的开始/清扫事件。"""
        if "start" not in str(current or "") and "finish" not in str(
                current or ""):
            return False
        return any(k in str(text or "") for k in (
            "开始", "清扫", "清洁", "拖地", "洗地", "回充", "充电"))

    @classmethod
    def _event_rank(cls, text, event):
        """PRD 条件与事件标识的关键词匹配分(只用于默认事件选择)。"""
        rank = 0
        for kw, target in cls._START_EVENT_KEYWORDS:
            if kw not in text:
                continue
            if event == target:
                rank += 3
            elif target.split("_")[0] in event:
                rank += 1
        # 细分场景精确命中给更高权重, 用来压过“同语音但语义更宽”的事件
        for kw, target in cls._SCENARIO_EVENT_KEYWORDS:
            if kw in text and event == target:
                rank += 4
        return rank

    @staticmethod
    def _name_score(text, name):
        """PRD 条件与 AP 中文事件名的语义相似度。

        不同项目/机型语音序号会变, 不能靠语音号匹配; 这里按中文关键词
        (去掉通用词后的二字片段重合度)排序, 对各项目的事件名都适用。
        """
        import difflib
        synonyms = (("房间", "区域"), ("划区", "区域"), ("选区", "区域"),
                    ("清洁", "清扫"), ("拖布", "拖地"))
        generic = ("事件", "报错", "请", "检查", "触发", "设置", "生效",
                   "状态", "工作", "任务", "开始", "结束")

        def prep(value):
            s = prd_memory.norm(value)
            for a, b in synonyms:
                s = s.replace(a, b)
            for word in generic:
                s = s.replace(word, "")
            return s

        a = prep(text)
        b = prep(name)
        if not a or not b:
            return 0.0
        if a in b or b in a:
            return 1.0

        def grams(s):
            return {s[i:i + 2] for i in range(max(0, len(s) - 1))}

        ga, gb = grams(a), grams(b)
        overlap = (len(ga & gb) / min(len(ga), len(gb))
                   if ga and gb else 0.0)
        ratio = difflib.SequenceMatcher(None, a, b).ratio()
        return max(overlap, ratio)

    def _enrich_prd_event_options(self):
        """把 AP 配置里和该语音同 sound 的事件都放进下拉。

        PRD 一条语音可能被多个 AP 事件复用(开始全局扫/拖地/划区/选区…),
        以前只显示记忆库选中的那一个, 用户看不到其它事件(2026-11-13)。
        这里补全候选, 并按 PRD 条件关键词选一个更合理的默认事件。
        """
        expect = self._fault_expect or {}
        if not expect or not self._prd_catalog:
            return
        entries = self._catalog_entries(self._prd_catalog)
        by_sound = {}
        for ev, info in expect.items():
            no = self._ap_sound_no(info)
            if no is not None:
                by_sound.setdefault(int(no), []).append((ev, info))
        for row in self._prd_rows:
            voice = hs4_config.voice_no(row.get("voice"))
            reuse = hs4_config.voice_no(row.get("reuse_voice"))
            qid_text = str(row.get("qid") or "")
            primary = None
            if qid_text:
                for key, info in (self._prd_catalog.get("active")
                                  or {}).items():
                    q = str(info.get("qid") or f"Q{int(key):03d}")
                    if q == qid_text:
                        primary = info
                        break
            if primary is None:      # 旧数据没有 qid: 退回按最终语音取主条目
                infos = entries.get(int(voice)) if voice is not None else []
                infos = infos or []
                primary = next(
                    (info for qid, info in infos
                     if hs4_config.voice_no(info.get("final", qid))
                     == voice),
                    infos[0][1] if infos else {})
            # 语义匹配以 PRD 的触发条件为准; 中文文案/复用语音文本可能是
            # 另一条语音的描述, 不能拿来扩大候选(用户 2026-11-13)
            text = str(primary.get("condition") or "").strip()
            if not text:
                text = str(primary.get("chinese")
                           or primary.get("text") or "").strip()
            # 候选以“中文含义”为主: 同语音事件 + 全配置里名称最接近的事件;
            # 语音号只作为辅助。同 sound 事件可能很多但语义无关, 这里过滤,
            # 只保留当前事件、强制事件、语义相关事件和开始清扫类变体
            # (用户 2026-11-13: 不显示无关 AP 事件)。
            current = str(row.get("event") or "")
            forced_events = set(self._forced_events_for(qid_text, text))
            start_ctx = self._is_start_context(text, current)
            water_only = self._is_water_only(text)
            objects = [(kw, tokens) for kw, tokens in self._OBJECT_KEYWORDS
                       if kw in text]
            actions = [(kw, tokens) for kw, tokens in self._ACTION_KEYWORDS
                       if kw in text]
            scenario_targets = {
                target for kw, target in self._SCENARIO_EVENT_KEYWORDS
                if kw in text}
            exclusive = {
                target for kw, target
                in self._EXCLUSIVE_SCENARIO_KEYWORDS.items()
                if kw in text}
            allow = self._scenario_allow(text)
            if allow is None:
                # 触发条件没写明场景时(如"配网中，主机连上公网"), 用 PRD
                # 语音中文再试一次, 避免漏掉正确的 AP 事件(用户 2026-11-17)
                allow = self._scenario_allow(" ".join(filter(None, (
                    primary.get("chinese"), primary.get("text")))))
            priority = self._priority_object_filter(text)

            def priority_ok(ev, name):
                if not priority:
                    return True
                tokens, names = priority
                return (any(tok in ev for tok in tokens)
                        or any(k in name for k in names))

            def object_ok(ev, name):
                if not objects:
                    return True
                return any(
                    any(tok in ev for tok in tokens) or kw in name
                    for kw, tokens in objects)

            def action_ok(ev, name):
                if not actions:
                    return True
                hard = [(kw, tokens) for kw, tokens in actions
                        if kw not in self._FAULT_FORM_ACTIONS]
                soft = [(kw, tokens) for kw, tokens in actions
                        if kw in self._FAULT_FORM_ACTIONS]
                if not all(
                        any(tok in ev for tok in tokens) or kw in name
                        for kw, tokens in hard):
                    return False
                if soft and not any(
                        any(tok in ev for tok in tokens) or kw in name
                        for kw, tokens in soft):
                    return False
                return True

            def keep(ev, info):
                name = str((info or {}).get("name") or ev)
                if expect and ev not in expect:
                    return False
                if allow is not None and ev not in allow:
                    return False
                if exclusive and ev not in exclusive:
                    return False
                if ev != current and not priority_ok(ev, name):
                    return False
                if (ev != current and not start_ctx
                        and not action_ok(ev, name)):
                    return False
                if water_only and ev in ("clean_mop_start",
                                         "clean_mop_done"):
                    return False
                if (ev == current or ev in forced_events
                        or ev in scenario_targets):
                    return True
                if start_ctx and ("start" in ev or "clean" in ev):
                    return True
                hits = self._domain_hits(text, ev, name)
                sim = self._name_score(text, name)
                # 没有关键词命中时, 名称相似度必须更高, 避免弱相关候选
                if hits == 0 and sim < 0.5:
                    return False
                return object_ok(ev, name)

            raw = (list(by_sound.get(int(voice), []))
                   if voice is not None else [])
            if reuse is not None and reuse != voice:
                for item in by_sound.get(int(reuse), []):
                    if all(item[0] != c[0] for c in raw):
                        raw.append(item)
            cands = [(ev, info) for ev, info in raw if keep(ev, info)]
            for ev in forced_events:
                info = expect.get(ev)
                if info and all(ev != c[0] for c in cands):
                    cands.append((ev, info))
            # 开始清扫类已经由「同语音事件」覆盖, 不再跨语音补语义候选,
            # 否则会把定点/划区等其它语音的事件也塞进来(用户 2026-11-13)
            semantic = [] if start_ctx else sorted(
                expect.items(),
                key=lambda kv: (
                    self._domain_hits(text, kv[0],
                                      kv[1].get("name") or kv[0]),
                    self._name_score(text, kv[1].get("name") or kv[0])),
                reverse=True)[:10]
            for ev, info in semantic:
                if not info:
                    continue
                if allow is not None and ev not in allow:
                    continue
                if exclusive and ev not in exclusive:
                    continue
                hits = self._domain_hits(
                    text, ev, info.get("name") or ev)
                name = str(info.get("name") or ev)
                if ev != current and not priority_ok(ev, name):
                    continue
                if ev != current and not start_ctx and not action_ok(ev, name):
                    continue
                if not hits and self._name_score(text, name) < 0.45:
                    continue
                if ev not in scenario_targets and not object_ok(ev, name):
                    continue
                if all(ev != c[0] for c in cands):
                    cands.append((ev, info))
            # 白名单里的精确事件强制进候选: 它们可能语音号对不上/名称相似度
            # 不够, 但就是该场景的正确事件(用户 2026-11-17)
            if allow:
                for ev in allow:
                    info = expect.get(ev)
                    if info and all(ev != c[0] for c in cands):
                        cands.append((ev, info))
            # AI/旧学习库可能给出当前 AP 配置里不存在的事件(别的项目才有);
            # 这种映射不能留在表里, 换成配置里最贴近的候选或标未找到。
            cur_ev = str(row.get("event") or "")
            if expect and cur_ev and cur_ev not in expect:
                if cands:
                    best_ev, best_info = max(
                        cands, key=lambda x: (
                            self._domain_hits(
                                text, x[0], x[1].get("name") or x[0]),
                            self._name_score(
                                text, x[1].get("name") or x[0])))
                    row["event"] = best_ev
                    row["name"] = str(best_info.get("name") or best_ev)
                else:
                    row["event"] = ""
                    row["name"] = "未找到对应事件"
                    row["fault_code"] = ""
                    row["fault_push"] = ""
            if not cands:
                # 没有有效候选也要继续清理旧候选: 不能把别的项目/AI 的
                # 无效事件留在下拉里(用户 2026-11-17)
                cands = []
            # 用户确认 / 真机验证 / AI 审核过的映射不再被关键词自动覆盖;
            # 只补全候选, 把选择权留给用户(用户 2026-11-13)
            locked = str(row.get("source") or "") in (
                "user", "verified", "ai_review")
            # 人工确认/真机验证的映射不动; AI 审核/记忆的建议可以被白名单纠正
            hard_locked = str(row.get("source") or "") in ("user", "verified")
            if allow is not None and not hard_locked:
                # 白名单按优先级排列: 当前事件不在白名单时用第一个可用事件;
                # 空白名单=当前 AP 配置没有对应事件, 明确标未找到
                cur_ev = str(row.get("event") or "")
                if not allow:
                    row["event"] = ""
                    row["name"] = "未找到对应事件"
                    row["fault_code"] = ""
                    row["fault_push"] = ""
                elif cur_ev not in allow:
                    pick = next((ev for ev in allow if ev in expect), None)
                    if pick:
                        info = expect[pick]
                        row["event"] = pick
                        row["name"] = str(info.get("name") or pick)
                        row["fault_code"] = str(info.get("code") or "")
                        row["fault_push"] = str(info.get("fault_push") or "")
            if not locked and cands and allow is None:
                cur = str(row.get("event") or "")
                cur_name = str((expect.get(cur) or {}).get("name") or cur)
                cur_hits = self._domain_hits(text, cur, cur_name)
                cur_rank = self._event_rank(text, cur)
                cur_sim = self._name_score(text, cur_name)
                best_ev, best_info = max(
                    cands, key=lambda x: (
                        self._domain_hits(
                            text, x[0], x[1].get("name") or x[0]),
                        self._event_rank(text, x[0]),
                        self._name_score(text,
                                         x[1].get("name") or x[0])))
                best_hits = self._domain_hits(
                    text, best_ev, best_info.get("name") or best_ev)
                best_rank = self._event_rank(text, best_ev)
                best_sim = self._name_score(
                    text, best_info.get("name") or best_ev)
                if (best_hits > cur_hits
                        or (best_hits == cur_hits
                            and (best_rank > cur_rank
                                 or (best_sim >= 0.35
                                     and best_sim > cur_sim + 0.05)))):
                    row["event"] = best_ev
                    row["name"] = str(best_info.get("name") or best_ev)
                    if best_info.get("code"):
                        row["fault_code"] = str(
                            best_info.get("code") or "")
                    if best_info.get("fault_push"):
                        row["fault_push"] = str(
                            best_info.get("fault_push") or "")
            forced = self._forced_events_for(qid_text, text)
            # 人工确认/真机验证的事件不能被 Q 号级覆盖改回去
            if forced and forced[0] in expect and not hard_locked:
                row["event"] = forced[0]
                row["name"] = str(
                    expect[forced[0]].get("name") or forced[0])
                # 备注只保留 PRD 内容, 不附加工具的语音冲突说明(用户 2026-11-13)
            opts = {str(o.get("event") or ""): dict(o)
                    for o in (row.get("event_options") or [])
                    if isinstance(o, dict) and o.get("event")}
            # 旧数据里已经存在的无关候选也要清掉, 不只是过滤新加的候选
            for ev in list(opts):
                if not keep(ev, expect.get(ev) or {}):
                    del opts[ev]
            current = str(row.get("event") or "")
            if current and current not in opts:
                info = expect.get(current) or {}
                opts[current] = {
                    "event": current,
                    "name": str(info.get("name")
                                or row.get("name") or current),
                    "fault_code": str(info.get("code") or ""),
                    "fault_push": str(info.get("fault_push") or ""),
                    "kind": str(row.get("kind") or ""),
                    "source": str(row.get("source") or "ap"),
                }
            for ev, info in cands:
                if ev not in opts:
                    opts[ev] = {
                        "event": ev,
                        "name": str(info.get("name") or ev),
                        "fault_code": str(info.get("code") or ""),
                        "fault_push": str(info.get("fault_push") or ""),
                        "kind": str(row.get("kind") or ""),
                        "source": "ap",
                    }
            # ★ 旧候选/名称匹配生成的选项没有中文名 —— 用 AP 配置名补全,
            #   下拉项悬停才能显示中文(用户 2026-11-17)。
            for ev, opt in opts.items():
                info = expect.get(ev) or {}
                name = str(opt.get("name")
                           or info.get("name") or "").strip()
                if not name or name == ev:
                    name = str(faults.event_name(ev, expect) or "").strip()
                opt["name"] = name or ev
            ordered = sorted(
                opts.values(),
                key=lambda o: (o.get("event") != row.get("event"),
                               -self._event_rank(text, str(o.get("event"))),
                               str(o.get("event"))))
            if allow is not None:
                # 白名单是硬约束: 旧候选/名称补全等任何路径都不能绕过
                forced_set = set(forced_events)
                ordered = [
                    o for o in ordered
                    if (str(o.get("event")) in allow
                        or str(o.get("event")) in forced_set
                        or str(o.get("event")) == str(row.get("event") or ""))]
            # 非开始清扫类最多保留 6 个候选; 开始清扫保留同语音全部变体
            if not start_ctx and len(ordered) > self._MAX_CANDIDATES:
                ordered = ordered[:self._MAX_CANDIDATES]
            row["event_options"] = ordered

    def _apply_prd_names(self):
        """中文名统一用 AP 平台事件配置里的名称(用户 2026-11-13)。

        以前优先用 PRD 语音文案, 会和 AP 事件名对不上; 现在:
        AP 配置名称 > 内置事件翻译 > 常用故障标签 > 事件标识。
        PRD 语音文案仍保留在 voice_text / 备注里, 不再占用中文名列。
        """
        for r in self._prd_rows:
            event = r.get("event")
            if not event:
                # 未匹配到 AP 事件时中文名保持可读提示(用户 2026-11-17)
                r["name"] = "未找到对应事件"
                continue
            name = faults.event_name(event, self._fault_expect)
            if name == event:   # 没有 AP 配置名/内置翻译时用常用故障标签
                name = (getattr(self, "_fault_presets", {}).get(event)
                        or event)
            r["name"] = name
            r["kind"] = ai_prd.normalize_kind(
                r.get("kind"), event, name, r.get("voice_text"))
            # 通用/提醒事件默认没有故障码; AI/旧记忆臆造的码不要显示。
            # 但真机实测回填/用户确认的码要保留(用户 2026-11-13)
            if (r["kind"] not in ("故障", "基站")
                    and r.get("code_source") not in
                    ("actual", "verified", "user")):
                r["fault_code"] = ""
                r["fault_push"] = ""
            # AP 配置没有故障码时, 也不保留 AI 臆造的码(ZY2L 日志无 code)
            cfg = (self._fault_expect or {}).get(event) or {}
            if (self._fault_expect and not str(cfg.get("code") or "")
                    and r.get("code_source") not in
                    ("actual", "verified", "user")):
                r["fault_code"] = ""
                r["fault_push"] = ""

    def _load_prd_rows(self):
        path = str(self.settings.value("apos/prd_path", "") or "")
        self._prd_path = path
        self._show_file_hint(self.fault_prd_label, path)
        if path:
            self._load_hs4_project(path)
        ver = self.settings.value("apos/prd_parsed_ver", 0, type=int)
        if ver != PRD_ROWS_VERSION:
            self._prd_rows = []
            self._fill_prd_table()
            self.fault_status.setText(
                "解析结果版本较旧，请重新点「AI 解析」生成最新列表")
            self.fault_status.setStyleSheet(
                "color:#b45309;font-size:12px;")
            return
        raw = str(self.settings.value("apos/prd_parsed", "") or "")
        try:
            rows = json.loads(raw) if raw else []
        except Exception:
            rows = []
        craw = str(self.settings.value("apos/prd_catalog", "") or "")
        if craw and not getattr(self, "_prd_catalog", None):
            try:
                self._prd_catalog = json.loads(craw)
            except Exception:
                self._prd_catalog = None
        # 故障/通用/提醒/基站四类都要保留(用户 2026-11-12); 补中文名
        self._prd_rows = [r for r in rows if isinstance(r, dict)] \
            if isinstance(rows, list) else []
        self._backfill_prd_voice_text()
        self._backfill_prd_skip_voice()
        self._relabel_prd_ids()
        self._apply_note_translations()
        self._enrich_prd_event_options()
        self._apply_prd_names()
        # 启动时故障页还没显示: 懒加载, 避免 100+ 行下拉框拖慢整个界面启动
        if self.fault_page.isVisible():
            self._fill_prd_table()
        else:
            self._prd_fill_pending = True

    def _backfill_prd_voice_text(self):
        """旧解析结果可能丢了 voice_text: 用 PRD 语音清单按 Q 号补回来。

        「暂时不提供语音」等取消标记丢了, 批量测试会误判成"缺语音文件",
        报告也不再显示"语音取消"(用户 2026-11-16 的 Q191)。
        """
        catalog = getattr(self, "_prd_catalog", None) or {}
        active = catalog.get("active") or {}
        if not active:
            return
        changed = False
        for row in self._prd_rows:
            if str(row.get("voice_text") or "").strip():
                continue
            digits = re.sub(r"\D", "", str(row.get("qid") or ""))
            if not digits:
                continue
            info = self._catalog_info(active, digits)
            if not isinstance(info, dict):
                continue
            text = ai_prd.voice_display_text(
                info.get("condition"), info.get("chinese"),
                info.get("text"))
            if text:
                row["voice_text"] = text
                changed = True
        if changed:
            self._save_prd_rows()

    def _relabel_prd_ids(self):
        """旧解析结果的 Q 号 → PRD 实际编号（用户 2026-11-17）。

        只有 PRD 本来用 Q 号的项目(HS4)才保留 Q；LG MR 这类 PRD 只有
        语音序号(0030/0001)，报告与表格都按原样显示，不能补一个 Q。
        """
        labels = self._catalog_id_labels()
        if not labels:
            return
        changed = False
        for row in self._prd_rows:
            digits = re.sub(r"\D", "", str(row.get("qid") or ""))
            if not digits:
                continue
            label = labels.get(int(digits))
            old = str(row.get("qid") or "")
            if not label or label == old:
                continue
            row["qid"] = label
            note = str(row.get("note") or "")
            if note:
                row["note"] = note.replace(f"（{old}）", f"（{label}）")
            changed = True
        if changed:
            self._save_prd_rows()

    def _catalog_id_labels(self):
        """{语音号: 显示编号}; 旧版目录没有 label 时重解析一次 PRD 补上。"""
        catalog = getattr(self, "_prd_catalog", None) or {}
        labels = self._labels_from_catalog(catalog)
        if labels:
            return labels
        path = str(getattr(self, "_prd_path", "") or "").strip()
        if not path or not os.path.exists(path):
            return {}
        try:
            fresh = ai_prd.extract_voice_catalog(path)
        except Exception:
            return {}
        fresh_labels = self._labels_from_catalog(fresh)
        if not fresh_labels:
            return {}
        # 只补 label, 不改已有映射/排除结论
        active = catalog.get("active") if isinstance(catalog, dict) else None
        new_active = (fresh or {}).get("active") or {}
        if isinstance(active, dict):
            for key, info in active.items():
                src = new_active.get(key) or new_active.get(int(key))
                if isinstance(info, dict) and isinstance(src, dict):
                    if src.get("label"):
                        info["label"] = src["label"]
            self._save_prd_catalog()
        return fresh_labels

    @staticmethod
    def _labels_from_catalog(catalog):
        labels = {}
        active = (catalog or {}).get("active") or {}
        for key, info in active.items():
            if not isinstance(info, dict):
                continue
            label = str(info.get("label") or "")
            m = re.search(r"\d+", label or str(key))
            if not label or not m:
                continue
            labels[int(m.group(0))] = label
        return labels

    @staticmethod
    def _catalog_info(active, digits):
        """按语音号取目录条目; 目录键在 JSON 里是字符串, 兼容 int/str 两种。"""
        try:
            n = int(digits)
        except (TypeError, ValueError):
            return {}
        for key in (digits, n, str(n)):
            info = (active or {}).get(key)
            if isinstance(info, dict):
                return info
        return {}

    def _backfill_prd_skip_voice(self):
        """旧解析结果补 comment / skip_voice: PRD 明说不用这条语音的
        不计入语音包检查(用户 2026-11-17)。"""
        catalog = getattr(self, "_prd_catalog", None) or {}
        active = catalog.get("active") or {}
        if not active:
            return
        changed = False
        for row in self._prd_rows:
            digits = re.sub(r"\D", "", str(row.get("qid") or "")
                            or str(row.get("voice") or ""))
            if not digits:
                continue
            info = self._catalog_info(active, digits)
            if not isinstance(info, dict):
                continue
            comment = str(info.get("comment") or "")
            if comment and not row.get("comment"):
                row["comment"] = comment
                changed = True
            skip = bool(info.get("skip_voice")) or ai_prd.voice_not_needed(
                row.get("comment"), row.get("voice_text"), row.get("note"))
            if row.get("skip_voice") is not skip:
                row["skip_voice"] = skip
                changed = True
        if changed:
            self._save_prd_rows()

    def _save_prd_rows(self):
        try:
            self.settings.setValue(
                "apos/prd_parsed",
                json.dumps(self._prd_rows, ensure_ascii=False))
            self.settings.setValue("apos/prd_path",
                                   getattr(self, "_prd_path", ""))
            self.settings.setValue("apos/prd_parsed_ver", PRD_ROWS_VERSION)
        except Exception:
            pass

    def _save_prd_catalog(self):
        """PRD 语音清单只在解析时写一次; 批量测试逐条保存时不再重复写大 JSON。"""
        if not getattr(self, "_prd_catalog", None):
            return
        try:
            self.settings.setValue(
                "apos/prd_catalog",
                json.dumps(self._prd_catalog, ensure_ascii=False))
        except Exception:
            pass

    def _missing_voice_numbers(self):
        """语音包检查里缺失的语音号集合(PRD 语音文件不存在)。"""
        out = set()
        for item in (self._voice_check_result or {}).get("missing") or []:
            n = hs4_config.voice_no(str((item or {}).get("file") or ""))
            if n is not None:
                out.add(n)
        return out

    def _prd_expected(self, event, prd_row=None):
        """PRD 解析条目 → 验证用的期望结构（没有返回 None）。

        batch 逐条测试时传入该 PRD 行, 避免同一 AP 事件有多个 PRD 语音时
        永远只按第一条判断(用户 2026-11-13)。
        """
        row = prd_row
        if row is None or row.get("event") != event:
            row = next((r for r in self._prd_rows
                        if r.get("event") == event), None)
        if row is None:
            return None
        cfg = (self._fault_expect or {}).get(event) or {}
        exp = dict(cfg)
        exp["name"] = (row.get("name")
                       or self._fault_presets.get(event) or event)
        prd_voice = str(row.get("voice") or "")
        # 若 AP 配置里明确没有语音, 就按「未配置」处理, 不要拿 PRD 语音
        # 硬判成「未捕获」(频繁集尘/集尘失败等, 用户 2026-11-13)
        ap_sound = cfg.get("sound")
        if isinstance(ap_sound, dict):
            ap_sound = ap_sound.get("identifier")
        ap_sound = str(ap_sound or "").strip()
        full_text = " ".join(filter(None, (
            str(row.get("voice_text") or ""), str(row.get("note") or ""))))
        cancelled = any(k in full_text for k in (
            "暂时不提供语音", "暂不提供语音", "不提供语音", "取消播报"))
        # PRD 标注 Not apply / 不适用 的语音, 判定口径要跟语音包检查一致
        # (skip_voice=True 时不能再要求设备播报, 否则必然假失败)
        cancelled = bool(cancelled or row.get("skip_voice")
                         or ai_prd.voice_not_needed(
                             row.get("comment"), row.get("voice_text"),
                             row.get("note")))
        exp["voice_cancelled"] = cancelled
        exp["sound"] = ("" if cancelled else
                        (prd_voice if (not cfg or ap_sound) else ""))
        # ★ 只接受 PRD 自己的语音和 PRD 里复用的语音; AP 事件配置的语音
        #   不自动放行, 否则 PRD/AP 语音不一致会被掩盖(用户 2026-11-13)
        accepted = []
        # 同一 AP 事件在当前平台上实际使用的语音也算通过(用户 2026-11-16):
        # 例如 Q124 PRD=124, 但 pickup 事件播报 125; Q085 PRD=085,
        # change_fan_level 播报 107。语音号会随项目变, 但仍是同一个事件。
        # ★ 例外: PRD 语音文件在机器人上缺失时, AP 自己的声音不能顶替
        #   (否则 0921 播 0824 也判通过, 用户 2026-11-17)。
        prd_missing = hs4_config.voice_no(prd_voice) in \
            self._missing_voice_numbers()
        if not cancelled and not prd_missing:
            for value in (str(row.get("reuse_voice") or ""), ap_sound):
                if (value
                        and hs4_config.voice_no(value)
                        != hs4_config.voice_no(prd_voice)
                        and all(hs4_config.voice_no(value)
                                != hs4_config.voice_no(x)
                                for x in accepted)):
                    accepted.append(value)
        if accepted:
            exp["acceptable_sounds"] = accepted
        led = {}
        names = {}
        # 实测回填的灯显(led_source=actual)只用于展示, 不能当期望
        if str(row.get("led_source") or "") != "actual":
            for dev, key, text_key in (
                    ("rgb_led__home", "led_home", "led_home_text"),
                    ("rgb_led__start", "led_start", "led_start_text")):
                val = str(row.get(key) or "").strip()
                if val:
                    led[dev] = val
                    names[dev] = {
                        "device": dev,
                        "effect": str(row.get(text_key) or val),
                        "effect_id": val,
                    }
        # ★ 灯显期望只来自 PRD/解析表, 不能直接用 AP 事件配置 —— 否则等于
        #   拿被测配置当期望, 测不出 AP 与 PRD 不一致(用户 2026-11-17)。
        #   PRD 行没填时, 用 PRD 灯显清单里的场景定义补(见 led_scene_for)。
        if not led:
            text = " ".join(filter(None, (
                str(row.get("voice_text") or ""),
                str(row.get("note") or ""))))
            scene = hs4_config.led_scene_for(self._hs4, text)
            if scene:
                vocab = {}
                for info in (self._fault_expect or {}).values():
                    for meta in (info.get("led_names") or {}).values():
                        eid = str((meta or {}).get("effect_id") or "")
                        cn = str((meta or {}).get("effect") or "")
                        if eid and cn:
                            vocab.setdefault(eid, cn)
                for dev, cn in (
                        ("rgb_led__start", scene.get("power")),
                        ("rgb_led__home", scene.get("home"))):
                    eid = hs4_config.led_effect_id(cn, vocab)
                    if not eid:
                        continue
                    led[dev] = eid
                    names[dev] = {
                        "device": dev,
                        "effect": str(cn),
                        "effect_id": eid,
                    }
        exp["led"] = led
        exp["led_names"] = names
        # 实测灯显可能来自其它事件, 用当前 AP 配置里的中文词表翻译
        vocab = {}
        for info in (self._fault_expect or {}).values():
            for meta in (info.get("led_names") or {}).values():
                eid = str((meta or {}).get("effect_id") or "")
                cn = str((meta or {}).get("effect") or "")
                if eid and cn:
                    vocab.setdefault(eid, cn)
        exp["led_vocab"] = vocab
        # ★ 故障码期望只认 AP 事件配置里的码, 或人工/真机流程确认过的码;
        #   实测回填的码(actual)不能当期望, 否则等于自己验证自己。
        ap_code = str(cfg.get("code") or "")
        prd_code = str(row.get("fault_code") or "")
        source = str(row.get("code_source") or "")
        exp["code"] = (ap_code or (prd_code if source in (
            "user", "verified") else ""))
        return exp

    def _expected_for(self, event, prd_row=None):
        """验证期望值：PRD 解析结果优先，其次事件配置。"""
        prd = self._prd_expected(event, prd_row=prd_row)
        if prd is not None:
            return prd
        return (self._fault_expect or {}).get(event) or {}

    def _load_fault_config(self, path, quiet=False):
        try:
            expect = faults.load_event_config(path)
        except Exception as e:
            self.fault_cfg_label.setText(
                f"加载失败：{os.path.basename(path)}")
            self.fault_cfg_label.setToolTip(f"{path}\n{e}")
            return
        self._fault_expect = expect
        self._fault_cfg_path = path
        # 输入框只显示文件名, 完整路径放 tooltip, 避免长路径换行(用户 2026-11-13)
        self._show_file_hint(self.fault_cfg_label, path)
        self.settings.setValue("apos/fault_event_config", path)
        # 配置里的全部事件都可以手动选/改后触发
        if hasattr(self, "fault_event_combo"):
            self.fault_event_combo.blockSignals(True)
            self.fault_event_combo.clear()
            for ident, info in sorted(expect.items()):
                name = faults.event_name(ident, expect)
                # 输入框只显示中文, 弹层由 delegate 补英文标识(用户 2026-11-27)
                self.fault_event_combo.addItem(name, ident)
                self.fault_event_combo.setItemData(
                    self.fault_event_combo.count() - 1,
                    f"{name}（{ident}）", Qt.ToolTipRole)
            self.fault_event_combo.blockSignals(False)
            if self.fault_event_combo.count():
                self.fault_event_combo.setCurrentIndex(0)
            items = []
            for i in range(self.fault_event_combo.count()):
                items.append(self.fault_event_combo.itemText(i))
                ident = str(self.fault_event_combo.itemData(i) or "")
                if ident:
                    items.append(ident)      # 中英文都能搜到
            self.fault_event_combo.setCompleter(
                self._make_event_completer(self.fault_event_combo, items))
            self._fit_combo_width(self.fault_event_combo, 90, 200)
        if hasattr(self, "fault_pick_combo"):
            self._refresh_fault_picks()
        if not quiet:
            # 状态栏只留一句短的, 详细数量放在判定行, 不再重复两遍
            self.fault_status.setText("AP 配置已加载")
            self.fault_status.setStyleSheet("color:#16a34a;font-size:12px;")
        self.fault_verdict.setText(
            f"已加载 AP 平台方案配置（{len(expect)} 个事件）："
            "配置里没有的事件会标为「未配置」")
        self.fault_verdict.setStyleSheet("color:#64748b;font-size:12px;")

    def _make_event_completer(self, combo, items):
        """关键字包含匹配的候选列表（下拉在输入框下方弹出）。"""
        comp = QCompleter(list(items), combo)
        comp.setCaseSensitivity(Qt.CaseInsensitive)
        comp.setFilterMode(Qt.MatchContains)
        comp.setCompletionMode(QCompleter.PopupCompletion)
        comp.setMaxVisibleItems(12)
        return comp

    def _current_fault_event(self):
        """下拉项/候选返回 identifier; 手动输入的名字原样返回。"""
        combo = self.fault_event_combo
        text = combo.currentText().strip()
        for i in range(combo.count()):
            data = combo.itemData(i)
            if data and (combo.itemText(i) == text or str(data) == text):
                return str(data)
        # 手动粘贴「中文（error_x）」/ 只输入英文标识时也能还原
        m = re.search(r"（([a-zA-Z][a-zA-Z0-9_]*)）", text)
        if m:
            return m.group(1)
        return text

    def _trigger_custom_event(self):
        event = self._current_fault_event()
        self._trigger_event(event, faults.event_name(event, self._fault_expect))

    def _trigger_event(self, event, label="", then=None, fast=False,
                       prd_row=None):
        """按事件名触发：主题由工具自动判断(用户 2026-11-11)。返回是否发出。"""
        event = str(event or "").strip()
        if not event:
            self.fault_status.setText("请选择或输入事件名")
            self.fault_status.setStyleSheet("color:#dc2626;font-size:12px;")
            return False
        if not (self._connected and self._service is not None):
            self._warn_not_connected()
            return False
        label = label or event
        if event in self._DESTRUCTIVE_EVENTS:
            if self._batch is not None:
                self.fault_status.setText(
                    f"已跳过 {event}：该事件会让设备真实关机")
                self.fault_status.setStyleSheet(
                    "color:#b45309;font-size:12px;")
                self._fault_log_line(
                    f"批量跳过 {label}：该事件会让设备真实关机",
                    "#b45309")
                return False
            if not _is_pytest():
                ans = QMessageBox.warning(
                    self, "危险事件",
                    f"{label}（{event}）会让设备真实关机。\n"
                    "触发后设备将不再响应后续事件，需要手动开机/重新连接。\n"
                    "确定继续？",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                if ans != QMessageBox.Yes:
                    self.fault_status.setText("已取消触发关机事件")
                    self.fault_status.setStyleSheet(
                        "color:#b45309;font-size:12px;")
                    return False
            self._fault_log_line(
                "注意：该事件会让设备真实关机，后续事件将无响应",
                "#dc2626")
        topic = faults.topic_for(event)
        t0 = time.time()
        # ★ 发送放到后台线程: AposSdkSendEvent 在通道断开/网络抖动时会阻塞
        #   数秒, 原来直接在 UI 线程调用, 点"触发"就会卡住无响应(用户 2026-11-16)。
        self.fault_status.setText(f"正在发送：{label}…")
        self.fault_status.setStyleSheet("color:#b45309;font-size:12px;")

        def sent(ok, ts=0.0):
            nonlocal t0
            if not ok:
                self.fault_status.setText(
                    f"发送失败：{event}"
                    "（推送通道断开，正在自动重连，请稍后重试）")
                self.fault_status.setStyleSheet(
                    "color:#dc2626;font-size:12px;")
                if then is not None:
                    # 批量: 不能把"通道断开"记成这条事件的失败并跳过去 ——
                    # 保持 idx 不动, 等通道恢复后重测同一条。
                    self._fault_log_line(
                        f"{label}：发送失败，等待推送通道恢复后重测本条",
                        "#b45309")
                    QTimer.singleShot(3000, self._batch_step)
                return
            if ts:
                # 日志窗口从真正发出事件那一刻算(等 SDK 锁的时间不算)
                t0 = float(ts)
            # ★ 等待截止时间从"发送调用返回"算: 通道拥塞时 post_event 可能
            #   阻塞 5~9 秒, 用 t0+7 会导致第一次轮询就超时(用户 2026-11-17)。
            sent_at = time.time()
            self.fault_status.setText(f"已触发：{label}，等待设备日志…")
            self.fault_status.setStyleSheet("color:#b45309;font-size:12px;")
            self.fault_result.setText("…")
            if self._batch is None:
                self._fault_log_reset()        # 单次触发只看最新日志
            self._fault_log_line(f"—— 触发 {label} ——", "#94a3b8")

            def finish():
                if then:
                    then(event, label, t0)

            def after():
                got = self._show_event_result(
                    event, label, t0, allow_retry=True, prd_row=prd_row)
                exp_item = self._expected_for(event, prd_row=prd_row)
                need_code = bool(str(exp_item.get("code") or ""))
                got_code = bool(getattr(self, "_last_event_res", {}).get("code"))
                need_led = bool((exp_item or {}).get("led"))
                got_led = bool((getattr(self, "_last_event_res", {}) or {})
                               .get("led"))
                retry = self._batch is not None and (
                    not got or (need_code and not got_code)
                    or (need_led and not got_led))
                if retry and not self._stream_ready():
                    # ★ 触发后推送通道断了: 这次"未捕获"不可信, 撤销结果,
                    #   等通道恢复后重测本条(不推进 idx, 不算失败)。
                    self._discard_last_fault_row(event)
                    self._fault_log_line(
                        f"{label}：推送通道中断，等待恢复后重测本条",
                        "#b45309")
                    QTimer.singleShot(3000, self._batch_step)
                    return
                if retry:
                    # 批量补一次: 无响应或语音/灯显到了但故障码没到。
                    # 有些事件第一次会被状态忽略, 或故障码日志晚于语音。
                    self._fault_log_line(
                        f"{label}：{'未捕获' if not got else '故障码未捕获'}，"
                        "重发一次并补读", "#b45309")

                    def resent(_ok, _ts=0.0):
                        QTimer.singleShot(
                            2000, lambda: (
                                self._show_event_result(
                                    event, label, t0, allow_retry=False,
                                    replace=True, prd_row=prd_row),
                                finish()))

                    self._send_event_async(topic, event, resent)
                else:
                    finish()

            if fast:
                # ★ 设备调试日志是成批下发的: 同一事件的 beep/LED 行可能比
                #   Recv 行晚 3~6 秒才到(用户 2026-11-17 实测)。
                #   按"期望的语音/灯显都到齐"提前结束, 没到齐就等到 7 秒。
                exp_item = self._expected_for(event, prd_row=prd_row)
                want_beep = bool(str((exp_item or {}).get("sound") or ""))
                # 语音文件在机器人上缺失时不用等语音(等也等不到), 直接等
                # 灯显/故障码, 否则 100+ 条要白等 7 秒(用户 2026-11-17 ZY2L)
                qid = str((prd_row or {}).get("qid") or "")
                if want_beep and qid:
                    for miss in ((self._voice_check_result or {})
                                 .get("missing") or []):
                        if qid in (miss.get("qids") or []):
                            want_beep = False
                            break
                want_led = bool((exp_item or {}).get("led"))
                want_code = bool(str((exp_item or {}).get("code") or ""))
                deadline = sent_at + 7.0
                stable_at = [None]

                def poll():
                    try:
                        lines = self._service.fault_log_since(t0)
                        res = faults.parse_verification(lines, event)
                    except Exception:
                        res = {}
                    now = time.time()
                    got_beep = bool(res.get("beep"))
                    got_led = bool(res.get("led"))
                    got_code = bool(res.get("code"))
                    complete = ((not want_beep or got_beep)
                                and (not want_led or got_led)
                                and (not want_code or got_code))
                    any_ev = got_beep or got_led or got_code
                    if complete and any_ev:
                        # 都到齐后再稳 0.6 秒, 防止同事件的后续行还在路上
                        if stable_at[0] is None:
                            stable_at[0] = now
                        if now - stable_at[0] >= 0.6:
                            after()
                            return
                    elif complete and now >= sent_at + 4.0:
                        # 没有期望值/设备不响应: 4 秒后结束
                        after()
                        return
                    if now >= deadline:
                        after()
                        return
                    QTimer.singleShot(200, poll)

                QTimer.singleShot(300, poll)
            else:
                QTimer.singleShot(4000, after)

        self._send_event_async(topic, event, sent)
        return True

    def _send_event_async(self, topic, event, on_done, key=""):
        """后台发送事件; on_done(ok, 实际发送开始时间) 在 UI 线程回调。"""
        if self._service is None:
            on_done(False, 0.0)
            return False
        old = self._send_worker
        if old is not None and old.isRunning():
            on_done(False, 0.0)
            return False
        worker = _SendEventWorker(self._service, topic, event, key=key,
                                  parent=self)
        self._send_worker = worker

        def done(ok, ts=0.0):
            on_done(ok, ts)

        def finished(w=worker):
            # 线程真正退出后才丢引用: 还在跑时窗口析构会 qFatal
            if self._send_worker is w:
                self._send_worker = None

        worker.done.connect(done)
        worker.finished.connect(finished)
        worker.finished.connect(worker.deleteLater)
        worker.start()
        return True

    def _refresh_fault_picks(self):
        """常用故障下拉：常用项 + 厂商模拟指令里的其它事件。"""
        combo = self.fault_pick_combo
        self._fault_presets = {}
        self._fault_groups = {}
        combo.blockSignals(True)
        combo.clear()
        for p in faults.presets(self._fault_expect):
            combo.addItem(p["label"], p["event"])
            combo.setItemData(combo.count() - 1, p["event"], Qt.ToolTipRole)
            self._fault_presets[p["event"]] = p["name"]
            self._fault_groups[p["event"]] = p["group"]
        combo.blockSignals(False)
        combo.setCompleter(self._make_event_completer(
            combo, [combo.itemText(i) for i in range(combo.count())]))
        self._fit_combo_width(combo, 220, 480)

    def _picked_fault_event(self):
        """下拉项/候选/手动输入(关键字或事件标识) → 事件名。"""
        combo = self.fault_pick_combo
        text = combo.currentText().strip()
        for i in range(combo.count()):
            if combo.itemText(i) == text:
                return str(combo.itemData(i) or "")
        if text in self._fault_presets:
            return text
        # 手动输入的关键字: 唯一匹配某个候选时才采用
        hits = [ev for label, ev in (
            (combo.itemText(i), str(combo.itemData(i) or ""))
            for i in range(combo.count())) if text and text in label]
        return hits[0] if len(hits) == 1 else ""

    def _simulate_picked_fault(self):
        event = self._picked_fault_event()
        if not event:
            self.fault_status.setText("请选择或输入故障关键字（从候选中选一个）")
            self.fault_status.setStyleSheet("color:#dc2626;font-size:12px;")
            return
        self._trigger_event(event, self._fault_presets.get(event) or "")

    def _simulate_fault(self, key):
        item = faults.get(key)
        if item is None:
            return
        if not (self._connected and self._service is not None):
            self._warn_not_connected()
            return
        t0 = time.time()
        self.fault_status.setText(f"正在发送：{item['name']}…")
        self.fault_status.setStyleSheet("color:#b45309;font-size:12px;")

        def sent(ok, ts=0.0):
            nonlocal t0
            if not ok:
                self.fault_status.setText(
                    f"发送失败：{item['name']}"
                    "（推送通道断开，正在自动重连，请稍后重试）")
                self.fault_status.setStyleSheet(
                    "color:#dc2626;font-size:12px;")
                return
            if ts:
                t0 = float(ts)
            self.fault_status.setText(f"已触发：{item['name']}，等待设备日志…")
            self.fault_status.setStyleSheet("color:#b45309;font-size:12px;")
            self.fault_result.setText("…")
            if self._batch is None:
                self._fault_log_reset()
            self._fault_log_line(f"—— 触发 {item['name']} ——", "#94a3b8")
            QTimer.singleShot(
                3000, lambda: self._show_fault_result(
                    key, t0, allow_retry=True))

        self._send_event_async("", item["event"], sent, key=key)

    def _show_fault_result(self, key, t0, allow_retry=False):
        item = faults.get(key)
        if item is None:
            return
        self._show_event_result(item["event"], item["name"], t0,
                                allow_retry=allow_retry)

    def _fault_log_reset(self):
        """清空日志内容(单次触发/批量开始只看本次内容)。"""
        self._fault_log_hist = []
        if hasattr(self, "fault_log"):
            self.fault_log.clear()

    def _fault_log_add(self, html):
        """追加一行日志, 默认滚到最新; 用户手动滚动时保留用户位置。

        ★ 故障模拟页的所有日志同时写进工具日志 test.log(用户 2026-11-13):
          界面里是带颜色的 HTML, 落盘前转成纯文本。
        """
        raw = str(html)
        plain = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)
        plain = re.sub(r"<[^>]+>", "", plain)
        import html as _html
        plain = _html.unescape(plain)
        for line in plain.splitlines():
            if line.strip():
                log.info("[故障模拟] %s", line.strip())
        if not hasattr(self, "_fault_log_hist"):
            self._fault_log_hist = []
        hist = self._fault_log_hist
        if not hist:
            self.fault_log.clear()
        hist.append(str(html))
        trim = len(hist) > 300
        if trim:
            del hist[:-300]
        auto = getattr(self, "_log_auto_scroll", True)
        bar = self.fault_log.verticalScrollBar()
        old = bar.value()
        if trim:
            # 超长时整体重建(很少发生), 用户暂停时尽量恢复原位置
            self.fault_log.setHtml("<br>".join(hist))
            if not auto:
                bar.setValue(min(old, bar.maximum()))
        else:
            cur = self.fault_log.textCursor()
            cur.movePosition(QTextCursor.End)
            if self.fault_log.document().isEmpty():
                cur.insertHtml(str(html))
            else:
                cur.insertHtml("<br>" + str(html))
            if not auto:
                bar.setValue(old)
        if auto:
            self._scroll_log_bottom()

    def _scroll_log_bottom(self):
        bar = self.fault_log.verticalScrollBar()
        bar.setValue(bar.maximum())

        def again():
            if getattr(self, "_log_auto_scroll", True):
                bar.setValue(bar.maximum())
        QTimer.singleShot(0, again)

    def _log_user_scrolled(self, *_):
        """用户手动滚动/按键: 暂停自动跟随, 20 秒无操作后恢复。"""
        if not hasattr(self, "_log_idle_timer"):
            return
        self._log_auto_scroll = False
        self._log_idle_timer.start(20000)
        QTimer.singleShot(0, self._log_check_bottom)

    def _log_check_bottom(self):
        bar = self.fault_log.verticalScrollBar()
        if bar.value() >= bar.maximum() - 2:
            self._log_auto_scroll = True
            self._log_idle_timer.stop()

    def _resume_log_autoscroll(self):
        self._log_auto_scroll = True
        self._scroll_log_bottom()

    def _fault_log_line(self, text, color="#64748b"):
        import html
        stamp = time.strftime("%H:%M:%S")
        self._fault_log_add(
            f'<span style="color:#94a3b8">{stamp}</span> '
            f'<span style="color:{color}">{html.escape(str(text))}</span>')

    def _show_event_result(self, event, label, t0, allow_retry=False,
                           replace=False, prd_row=None):
        if self._service is None:
            return
        try:
            lines = self._service.fault_log_since(t0)
        except Exception:
            lines = []
        res = faults.parse_verification(lines, event)
        self._last_event_res = res
        # 实测灯显回填到表格供查看(标记 actual, 不参与期望判定)
        self._fill_prd_led_from_actual(event, res.get("led") or {},
                                       prd_row=prd_row)
        self._fill_prd_code_from_actual(event, res.get("code"),
                                        prd_row=prd_row)
        # PRD 解析结果优先; 都没有时才只显示实测值
        exp_item = self._expected_for(event, prd_row=prd_row)
        exp_map = {event: exp_item} if exp_item else {}
        sound_cfg = bool(exp_item.get("sound"))
        led_cfg = bool(exp_item.get("led"))
        has_any_cfg = bool(self._fault_expect) or bool(self._prd_rows)
        unset = "AP平台未配置" if (exp_item or has_any_cfg) else "未捕获"
        beep = res.get("beep") or (unset if not sound_cfg else "未捕获")
        led = res.get("led") or {}
        led_text = faults.led_display(led, exp_map) or (
            unset if not led_cfg else "未捕获")
        code = res.get("code") or "—"
        self.fault_result.setText(
            f"语音序号 {beep} · 灯显 {led_text} · 故障码 {code}")
        evidence = res.get("evidence") or res.get("matched") or []
        if not lines:
            empty_text = ("（没有收到任何设备日志：调试流可能断开/心跳未生效，"
                          "请检查连接）")
        elif not evidence:
            others = faults.other_event_responses(lines, event)
            if others:
                detail = "、".join(f"{n}(beep {b})"
                                  for n, b in list(others.items())[:5])
                empty_text = f"（未匹配到本事件，但设备播报了：{detail}）"
            elif not faults.has_simulation(event):
                empty_text = "（该事件没有厂商模拟指令，设备可能不会响应）"
            else:
                empty_text = "（日志中未找到该事件，可能设备当前状态不响应）"
        else:
            empty_text = ""
        if self._batch is not None:
            # 批量测试: 每条只写一行摘要。把 30 行带颜色的日志全塞进
            # QTextEdit, 110 条后文档巨大, 界面点哪都卡(用户 2026-11-13)
            self._fault_log_line(
                f"{label}：语音 {beep} · 灯显 {led_text} · 故障码 {code}",
                "#475569")
        else:
            rows = [faults.highlight_log_line(x) for x in evidence[-30:]]
            if rows:
                self._fault_log_add("<br>".join(rows))
            else:
                self._fault_log_line(empty_text, "#94a3b8")
        if not evidence and "设备播报了" in empty_text:
            self.fault_status.setText("未捕获本事件，但检测到其它事件播报（详见日志）")
            self.fault_status.setStyleSheet(
                "color:#b45309;font-size:12px;")
        # ★ 按 PRD/事件配置验证: 配置里没有的事件 = 未配置(不算失败)
        if not self._fault_expect and not self._prd_rows:
            self.fault_verdict.setText("未加载配置：只显示实测值")
            self.fault_verdict.setStyleSheet("color:#94a3b8;font-size:12px;")
        else:
            cmp = faults.compare(res, exp_map, event)
            if cmp["ok"] is None:
                # 区分"配置里没有该事件"和"配置里未配置语音/灯显"
                if cmp.get("expected") is None:
                    tip = "AP平台未配置该事件（不做判定）"
                else:
                    tip = cmp["problems"][0] + "（不做判定）"
                self.fault_verdict.setText(tip)
                self.fault_verdict.setStyleSheet(
                    "color:#94a3b8;font-size:12px;")
            elif cmp["ok"]:
                self.fault_verdict.setText("✓ 与配置一致")
                self.fault_verdict.setStyleSheet(
                    "color:#16a34a;font-size:12px;font-weight:600;")
            else:
                self.fault_verdict.setText(
                    "✗ 与配置不一致：" + "；".join(cmp["problems"]))
                self.fault_verdict.setStyleSheet(
                    "color:#dc2626;font-size:12px;font-weight:600;")
        self.fault_status.setText(f"已触发：{label}（设备日志解析完成）")
        self.fault_status.setStyleSheet("color:#16a34a;font-size:12px;")
        # 项目清单对照 + 记录到测试报告
        ver = hs4_config.verify(res, exp_map, event, self._hs4)
        self._update_hs4_note(ver)
        if (replace and self._fault_rows
                and self._fault_rows[-1].get("event") == event):
            self._fault_rows.pop()
        self._record_fault_row(event, label, ver, res, prd_row=prd_row)
        # 3 秒窗口没抓到语音/灯显/故障码时, 再等 1.5 秒重读一次(单次触发);
        # 语音先到、灯显 JSON 晚到时也要补读(用户 2026-11-17)。
        missing_led = bool(exp_item.get("led")) and not res.get("led")
        if (allow_retry and self._batch is None
                and (missing_led or not (res.get("beep") or res.get("led")
                                         or res.get("code")))):
            self._fault_log_line("等待延迟播报/灯显…", "#94a3b8")
            # 灯显 JSON 可能比语音晚 1~2 秒到, 补读要给足时间
            delay = 2500 if missing_led else 1500
            QTimer.singleShot(
                delay, lambda: self._show_event_result(
                    event, label, t0, allow_retry=False, replace=True,
                    prd_row=prd_row))
        return bool(res.get("beep") or res.get("led") or res.get("code"))

    def _update_hs4_note(self, ver):
        if self._hs4 is None:
            self.fault_hs4_note.setText("")
            return

        def part(name, d):
            note = d.get("note") or ""
            extra = f"（{note}）" if note and note != d["status"] else ""
            return f"{name} {d['status']}{extra}"

        self.fault_hs4_note.setText(
            "清单对照：" + " · ".join([
                part("语音", ver["voice"]), part("灯显", ver["led"]),
                part("故障码", ver["code"])]))

    def _record_fault_row(self, event, label, ver, res, prd_row=None):
        if prd_row is None:
            prd_row = next((r for r in self._prd_rows
                            if r.get("event") == event), {})
        # PRD 要求语音、但 AP 配置里根本没配声音: 整体应判「AP平台未配置」,
        # 不能因为灯显对上就把整条算通过(用户 2026-11-17: 开机 0001)
        prd_voice = str(prd_row.get("voice") or "").strip()
        cfg = (self._fault_expect or {}).get(event) or {}
        ap_sound = cfg.get("sound")
        if isinstance(ap_sound, dict):
            ap_sound = ap_sound.get("identifier")
        ap_sound = str(ap_sound or "").strip()
        if (prd_voice and not ap_sound and not prd_row.get("skip_voice")
                and ver.get("overall") == hs4_config.STATUS_OK
                and ver.get("voice", {}).get("status")
                == hs4_config.STATUS_UNCONFIGURED):
            ver["overall"] = hs4_config.STATUS_UNCONFIGURED
        note = str(prd_row.get("note") or "")
        m = re.search(r"PRD[：:]\s*(.+?)(?:\n（|$)", note, re.S)
        prd_content = ((m.group(1).strip() if m else "")
                       or str(prd_row.get("voice_text") or ""))
        # 报告里 PRD 事件内容用中文, 原文是非中文时取翻译(用户 2026-11-17)
        prd_content = str(prd_row.get("prd_content_zh") or prd_content)
        row = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "event": event,
            "name": label or event,
            "qid": str(prd_row.get("qid") or ""),
            "prd_content": prd_content,
            "prd_voice": str(prd_row.get("voice") or ""),
            "group": self._fault_groups.get(event, ""),
            "overall": ver["overall"],
            # 报告「期望语音」显示 PRD 自己的语音序号(用户 2026-11-17),
            # AP 允许的替代声音只参与判定, 不混进期望列
            "voice_expected": (str(prd_row.get("voice") or "")
                               or ver["voice"].get("expected", "")),
            "voice_actual": ver["voice"].get("actual", ""),
            "voice_status": ver["voice"]["status"],
            "voice_note": ver["voice"].get("note", ""),
            "led_expected": ver["led"].get("expected", ""),
            "led_actual": ver["led"].get("actual", ""),
            "led_status": ver["led"]["status"],
            "led_note": ver["led"].get("note", ""),
            "code_expected": ver["code"].get("expected", ""),
            "code_actual": ver["code"].get("actual", ""),
            "code_status": ver["code"]["status"],
            "code_note": ver["code"].get("note", ""),
            "note": "",
        }
        if not self._fault_report_started:
            self._fault_report_started = row["time"]
        self._fault_rows.append(row)
        self.fault_report_btn.setEnabled(True)
        self._learn_verified(event, ver, row)

    def _ap_fingerprint(self):
        """AP 配置指纹: 只含事件/语音/灯显配置, 不含 IP/SN。"""
        if getattr(self, "_ap_fp_cache", ""):
            return self._ap_fp_cache
        import hashlib
        pairs = []
        for ev, info in (self._fault_expect or {}).items():
            snd = info.get("sound")
            if isinstance(snd, dict):
                snd = snd.get("identifier")
            pairs.append(f"{ev}:{snd or ''}:{len(info.get('led') or {})}")
        self._ap_fp_cache = hashlib.sha1(
            "|".join(sorted(pairs)).encode("utf-8")).hexdigest()[:12]
        return self._ap_fp_cache

    def _learn_verified(self, event, ver, row):
        """批量/单次通过且语音+灯显都配置正确时, 沉淀为可共享学习项。"""
        if ver.get("overall") != hs4_config.STATUS_OK:
            return
        if (ver.get("voice", {}).get("status") != hs4_config.STATUS_OK
                or ver.get("led", {}).get("status") != hs4_config.STATUS_OK):
            return
        exp = (self._fault_expect or {}).get(event) or {}
        sound = exp.get("sound")
        if isinstance(sound, dict):
            sound = sound.get("identifier")
        # 只收 AP 里同时配置了语音和灯显的事件
        if not str(sound or "").strip() or not exp.get("led"):
            return
        voice = str(row.get("voice_actual") or row.get("voice_expected") or "")
        prd = next((r for r in self._prd_rows
                    if r.get("event") == event
                    and str(r.get("voice") or "") == voice), None)
        if prd is None or not prd.get("voice_key"):
            return
        info = self._device_info or {}
        self._verified_pending[prd["voice_key"]] = {
            "event": event,
            "event_name": (prd.get("name")
                           or faults.event_name(event, self._fault_expect)),
            "voice": voice,
            "voice_text": prd.get("voice_text") or "",
            "kind": prd.get("kind") or "",
            "fault_code": (row.get("code_actual")
                           or prd.get("fault_code") or ""),
            "fault_push": prd.get("fault_push") or "",
            "qid": prd.get("qid") or "",
            "ap_sound": str(sound),
            "led_expected": row.get("led_expected") or "",
            "verified_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "model": str(info.get("model") or ""),
            "firmware": str(info.get("firmware_version") or ""),
            "ap_fingerprint": self._ap_fingerprint(),
        }
        if self._batch is None:
            QTimer.singleShot(1000, self._flush_verified_learning)

    def _flush_verified_learning(self):
        pending = getattr(self, "_verified_pending", None)
        if not pending:
            return
        for key, mapping in list(pending.items()):
            self._prd_memory.put(key, mapping, source="verified",
                                 save=False)
        self._prd_memory.save()
        pending.clear()

    def _batch_report_path(self):
        """批量测试自动报告: reports/项目_基站版本_时间.xlsx。"""
        info = self._device_info or {}
        model = str(info.get("model") or "").strip()
        if not model:
            prd = str(getattr(self, "_prd_path", "") or "").strip()
            model = os.path.splitext(os.path.basename(prd))[0] or "未知项目"
        # ★ 名称 = 项目 + 固件版本 + 时间(用户 2026-11-13: 只用固件版本)
        fw = str(info.get("firmware_version") or "").strip()
        version = fw or "未知版本"
        stamp = time.strftime("%Y%m%d_%H%M%S")
        from core.driver import BASE_DIR, REPORT_SUBDIRS
        # 报告名称/内容都不写设备 IP(用户 2026-11-13)
        raw = f"故障模拟_{model}_{version}_{stamp}.xlsx"
        safe = re.sub(r'[\/:*?"<>|]+', "_", raw)
        return os.path.join(BASE_DIR, "reports",
                            REPORT_SUBDIRS["fault"], safe)

    def _save_batch_report(self):
        path = self._batch_report_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            hs4_config.make_report(
                path, self._report_rows(),
                (self._hs4 or {}).get("path", ""),
                str(getattr(self, "_connected_ip", "") or ""),
                self._fault_report_started,
                device_info=self._device_info,
                voice_check=self._voice_check_result,
                unmatched=self._unmatched_prd_rows())
        except Exception as e:
            self.fault_status.setText(f"自动报告保存失败：{e}")
            self.fault_status.setStyleSheet(
                "color:#dc2626;font-size:12px;")
            return
        self.fault_status.setText(f"批量测试报告已自动保存：{path}")
        self._fault_log_line(f"批量测试报告已保存：{path}", "#16a34a")

    def _fault_config_dir(self):
        """故障模拟事件配置统一放在工具目录下(用户 2026-11-13)。"""
        from core.driver import BASE_DIR
        path = os.path.join(BASE_DIR, "config", "fault_events")
        try:
            os.makedirs(path, exist_ok=True)
        except Exception:
            pass
        return path

    def _project_name(self):
        """项目名称: 机器人型号(如 CLA_HS4); 没连接时用已记录型号/PRD 名兜底."""
        model = str((self._device_info or {}).get("model") or "").strip()
        if model:
            return model
        ip = (getattr(self, "_connected_ip", "")
              or self._selected_ip())
        for dev in self._known_devices():
            if str(dev.get("ip") or "") == str(ip) and dev.get("model"):
                return str(dev["model"])
        label = getattr(self, "fault_prd_label", None)
        prd = os.path.basename(label.text().strip() if label else "")
        stem = os.path.splitext(prd)[0].strip()
        return stem or "AutoTest"

    def _save_prd_config_file(self):
        if not self._prd_rows:
            self.fault_status.setText("还没有解析结果，先做一次 AI 解析")
            self.fault_status.setStyleSheet("color:#dc2626;font-size:12px;")
            return
        default_dir = self._fault_config_dir()
        stamp = time.strftime("%Y%m%d_%H%M%S")
        # 命名: 项目名称_故障模拟配置_时间(用户 2026-11-17)
        default = os.path.join(
            default_dir,
            f"{self._project_name()}_故障模拟配置_{stamp}.yaml")
        path, _ = QFileDialog.getSaveFileName(
            self, "保存解析配置", default, "YAML 配置 (*.yaml)")
        if not path:
            return
        if not path.lower().endswith((".yaml", ".yml")):
            path += ".yaml"
        data = {
            "version": 1,
            "prd": getattr(self, "_prd_path", ""),
            "device": dict(self._device_info or {}),
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "events": [dict(r) for r in self._prd_rows],
        }
        try:
            with open(path, "w", encoding="utf-8") as f:
                yaml.safe_dump(data, f, allow_unicode=True,
                               sort_keys=False)
        except Exception as e:
            self.fault_status.setText(f"保存配置失败：{e}")
            self.fault_status.setStyleSheet("color:#dc2626;font-size:12px;")
            return
        self.fault_status.setText(f"配置已保存：{path}")
        self._fault_log_line(f"解析配置已保存：{path}", "#16a34a")

    def _load_prd_config_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "读取解析配置", self._fault_config_dir(),
            "YAML 配置 (*.yaml *.yml);;所有文件 (*)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            events = (data.get("events") if isinstance(data, dict)
                      else data) or []
            if not isinstance(events, list):
                raise ValueError("文件里没有 events 列表")
        except Exception as e:
            self.fault_status.setText(f"读取配置失败：{e}")
            self.fault_status.setStyleSheet("color:#dc2626;font-size:12px;")
            return
        rows = []
        for it in events:
            if not isinstance(it, dict) or not it.get("event"):
                continue
            row = dict(it)
            row["kind"] = ai_prd.normalize_kind(
                row.get("kind"), row.get("event"), row.get("name"),
                row.get("voice_text"))
            rows.append(row)
        if not rows:
            self.fault_status.setText("配置里没有可用的事件")
            self.fault_status.setStyleSheet("color:#dc2626;font-size:12px;")
            return
        if isinstance(data, dict) and data.get("prd"):
            self._prd_path = str(data["prd"])
            self._show_file_hint(self.fault_prd_label, self._prd_path)
        self._prd_rows = rows
        self._sort_prd_rows()
        self._fill_prd_table()
        self._save_prd_rows()
        self._remember_rows(self._prd_rows, source="user")
        self.fault_status.setText(
            f"已读取配置：{len(rows)} 个事件（{os.path.basename(path)}）")
        self.fault_status.setStyleSheet("color:#16a34a;font-size:12px;")
        self._fault_log_line(
            f"已读取解析配置：{len(rows)} 个事件（{path}）", "#16a34a")

    @staticmethod
    def _has_cjk(text):
        return bool(re.search(r"[一-鿿]", str(text or "")))

    def _apply_note_translations(self):
        """备注里的 PRD 含义用中文翻译(有 prd_content_zh 时), 用户 2026-11-17."""
        changed = False
        for r in self._prd_rows:
            zh = str(r.get("prd_content_zh") or "").strip()
            note = str(r.get("note") or "")
            if not zh or not note.startswith("PRD"):
                continue
            m = re.match(r"(PRD[：:])(.+?)(\n（.*)$", note, re.S)
            if m:
                if m.group(2).strip() == zh:
                    continue
                r["note"] = m.group(1) + zh + m.group(3)
            else:
                r["note"] = re.sub(r"^(PRD[：:]).+$",
                                   lambda mm: mm.group(1) + zh,
                                   note, count=1, flags=re.S)
            changed = True
        if changed:
            self._save_prd_rows()

    def _prd_meaning_text(self, row):
        """PRD 行的场景含义(备注里的 PRD 内容, 没有则用语音文案)。"""
        note = str((row or {}).get("note") or "")
        m = re.search(r"PRD[：:]\s*(.+?)(?:\n（|$)", note, re.S)
        return ((m.group(1).strip() if m else "")
                or str((row or {}).get("voice_text") or ""))

    def _ensure_prd_translations(self):
        """非中文的 PRD 事件内容翻译成中文, 报告更易读(用户 2026-11-17)."""
        pending = {}
        for r in self._prd_rows:
            content = self._prd_meaning_text(r)
            if (not content or self._has_cjk(content)
                    or r.get("prd_content_zh")):
                continue
            vt = str(r.get("voice_text") or "").strip()
            if self._has_cjk(vt) and vt not in ("仅旋律", "僅旋律"):
                r["prd_content_zh"] = vt
                continue
            pending[content] = ""
        if not pending:
            return
        cache = {}
        try:
            cache = json.loads(
                str(self.settings.value("apos/prd_zh_cache", "") or "{}"))
        except Exception:
            cache = {}
        if not isinstance(cache, dict):
            cache = {}
        for text in list(pending):
            zh = str(cache.get(text) or "")
            if zh and self._has_cjk(zh):
                pending[text] = zh
        todo = [t for t, v in pending.items() if not v]
        if todo:
            cfg = self._ai_config()
            if cfg.get("base_url") and cfg.get("model"):
                try:
                    res = ai_prd.translate_texts(
                        todo, cfg["base_url"], cfg.get("api_key", ""),
                        cfg["model"], timeout=60)
                except Exception as e:
                    log.info(f"[故障模拟] PRD 内容翻译失败: {e}")
                    res = {}
                for text, zh in (res or {}).items():
                    if self._has_cjk(zh):
                        pending[text] = zh
                        cache[text] = zh
                if res:
                    try:
                        self.settings.setValue(
                            "apos/prd_zh_cache",
                            json.dumps(cache, ensure_ascii=False))
                    except Exception:
                        pass
        changed = False
        for r in self._prd_rows:
            text = self._prd_meaning_text(r)
            zh = pending.get(text) or ""
            if zh and self._has_cjk(zh) and \
                    r.get("prd_content_zh") != zh:
                r["prd_content_zh"] = zh
                changed = True
        if changed:
            self._apply_note_translations()
            self._save_prd_rows()
            # 解析页备注列同步刷新为中文
            if self.fault_page.isVisible():
                self._fill_prd_table()

    def _unmatched_prd_rows(self):
        """PRD 有、AP 平台没有对应事件的行 —— 报告里要列出来(用户 2026-11-17)."""
        out = []
        for r in self._prd_rows:
            if str(r.get("event") or "").strip():
                continue
            note = str(r.get("note") or "")
            m = re.search(r"PRD[：:]\s*([^\n；]+)", note)
            content = ((m.group(1).strip() if m else "")
                       or str(r.get("voice_text") or ""))
            extra = "（PRD 标注不使用该语音）" if r.get("skip_voice") else ""
            out.append({
                "qid": str(r.get("qid") or ""),
                "voice": str(r.get("voice") or ""),
                "prd_content": content,
                "event": "",
                "name": "未找到对应事件",
                "overall": hs4_config.STATUS_NO_EVENT,
                "note": f"AP平台配置中没有对应事件，未触发{extra}",
            })
        return out

    def _report_rows(self):
        """报告按 PRD 事件去重, 同一 PRD 事件保留最严重的一次结果。

        PRD 多个语音可能映射到同一个 AP 事件, 但它们是不同的 PRD 事件,
        报告必须各保留一行并写明 PRD 内容。只有同一个 PRD 事件(qid)
        被重复测试时才去重; 结果不同优先保留失败, 避免隐藏未通过项
        (用户 2026-11-13)。
        """
        rank = {"不一致": 3, "清单未覆盖": 2, "AP平台未配置": 1, "通过": 0}
        picked = {}
        for row in self._fault_rows:
            qid = str(row.get("qid") or "")
            content = str(row.get("prd_content") or "")
            key = qid or f"{row.get('event')}|{content}"
            old = picked.get(key)
            if (old is None
                    or rank.get(str(row.get("overall")), 0)
                    >= rank.get(str(old.get("overall")), 0)):
                picked[key] = row
        return sorted(picked.values(),
                      key=lambda r: str(r.get("time") or ""))

    def _export_fault_report(self):
        rows = self._report_rows()
        if not rows:
            self.fault_status.setText("还没有测试结果，先触发一次事件")
            self.fault_status.setStyleSheet("color:#dc2626;font-size:12px;")
            return
        name = ("HS4_故障模拟测试报告_"
                + time.strftime("%Y%m%d_%H%M%S") + ".xlsx")
        default = os.path.join(os.path.expanduser("~"), "Desktop", name)
        path, _ = QFileDialog.getSaveFileName(
            self, "保存测试报告", default, "Excel 报告 (*.xlsx)")
        if not path:
            return
        if not path.lower().endswith(".xlsx"):
            path += ".xlsx"
        try:
            hs4_config.make_report(
                path, rows,
                (self._hs4 or {}).get("path", ""),
                str(getattr(self, "_connected_ip", "") or ""),
                self._fault_report_started,
                device_info=self._device_info,
                voice_check=self._voice_check_result,
                unmatched=self._unmatched_prd_rows())
        except Exception as e:
            self.fault_status.setText(f"报告保存失败：{e}")
            self.fault_status.setStyleSheet("color:#dc2626;font-size:12px;")
            return
        self.fault_status.setText(f"报告已保存：{path}")
        self.fault_status.setStyleSheet("color:#16a34a;font-size:12px;")

    # ── 批量测试：依次触发、自动对照、最后出报告 ──
    def _batch_items(self):
        combo = self.fault_pick_combo
        text = combo.currentText().strip()
        exact = any(combo.itemText(i) == text for i in range(combo.count()))
        keyword = None if (not text or exact) else text
        if self._prd_rows:
            # 有 AI 解析结果时, 每个 PRD 事件各测一次; 同一 AP 事件可能对应
            # 多个 PRD 语音, 不能按 AP 事件合并(用户 2026-11-13)
            base = [(r.get("event", ""), r.get("name") or r.get("event"), r)
                    for r in self._prd_rows if r.get("event")]
        else:
            base = []
            for i in range(combo.count()):
                event = str(combo.itemData(i) or "")
                if event:
                    base.append((event,
                                 self._fault_presets.get(event) or event,
                                 None))
        out, seen = [], set()
        for event, label, prd_row in base:
            if keyword and keyword not in (label or "") and keyword not in event:
                continue
            # 同一 PRD 事件(qid)只保留一个; 不同 PRD 语音映射到同一 AP
            # 事件时仍分别测试(各自期望语音/PRD内容不同)。
            # 没有 qid 的 PRD(PDF/TXT/MD) 不能用空串当 key, 否则只会剩第一条
            key = (str(prd_row.get("qid") or "")
                   if prd_row else f"{event}|{label}")
            if prd_row is not None and not key:
                key = f"{event}|{label}|{id(prd_row)}"
            if key in seen:
                continue
            seen.add(key)
            out.append((event, label or event, prd_row))
        return out

    def _warn_not_connected(self):
        """未连接设备：状态栏红字 + 用户主动操作时弹窗（批量内部不弹）。"""
        self.fault_status.setText("请先连接设备")
        self.fault_status.setStyleSheet("color:#dc2626;font-size:12px;")
        if getattr(self, "_batch", None) is None:
            QMessageBox.warning(
                self, "未连接扫地机",
                "请先在「灯显语音测试」页点击「连接」并确认设备已连接，"
                "再执行测试。")

    def _toggle_batch(self):
        if self._batch:
            self._batch["stop"] = True
            self.fault_batch_btn.setText("正在停止…")
            return
        if not (self._connected and self._service is not None):
            self._warn_not_connected()
            return
        items = self._batch_items()
        if not items:
            self.fault_status.setText("没有可批量测试的事件")
            self.fault_status.setStyleSheet("color:#dc2626;font-size:12px;")
            return
        # 语音包检查页的 PRD 内容、报告内容都要中文(非中文先翻译)
        try:
            self._ensure_prd_translations()
        except Exception as e:
            log.info(f"[故障模拟] PRD 内容翻译跳过: {e}")
        if self._voice_check_worker is not None:
            return
        host = str(getattr(self, "_connected_ip", "")
                   or self._selected_ip() or "").strip()
        self._batch_pending_items = items
        self._voice_check_result = None
        self.fault_batch_btn.setEnabled(False)
        self.fault_batch_btn.setText("检查语音包…")
        self.fault_status.setText("批量测试前检查机器人语音包…")
        self.fault_status.setStyleSheet("color:#b45309;font-size:12px;")
        self._voice_check_worker = _VoiceCheckWorker(
            host, self._prd_rows, self, expect=self._fault_expect)
        self._voice_check_worker.done.connect(self._on_voice_checked)
        self._voice_check_worker.failed.connect(
            lambda msg: self._on_voice_checked({
                "checked": False, "error": str(msg), "missing": []}))
        self._voice_check_worker.start()

    def _on_voice_checked(self, result):
        self._voice_check_worker = None
        self._voice_check_result = result if isinstance(result, dict) else {}
        self.fault_batch_btn.setEnabled(True)
        items = list(getattr(self, "_batch_pending_items", []) or [])
        self._batch_pending_items = []
        missing = self._voice_check_result.get("missing") or []
        if missing:
            qids = []
            for item in missing:
                for qid in item.get("qids") or []:
                    if qid and qid not in qids:
                        qids.append(qid)
            self._fault_log_line(
                "语音包检查缺失：" + "、".join(qids), "#dc2626")
            self.fault_status.setText(
                f"语音包缺失 {len(missing)} 个（{'、'.join(qids)}），"
                "报告中会注明")
            self.fault_status.setStyleSheet(
                "color:#b45309;font-size:12px;")
        elif self._voice_check_result.get("checked"):
            self._fault_log_line("语音包检查：PRD 所需语音齐全", "#16a34a")
            self.fault_status.setText("语音包检查：齐全")
            self.fault_status.setStyleSheet(
                "color:#16a34a;font-size:12px;")
        else:
            self._fault_log_line(
                "语音包检查未执行："
                + str(self._voice_check_result.get("error") or "")[:200],
                "#b45309")
            self.fault_status.setText("语音包检查未执行，报告中会注明")
            self.fault_status.setStyleSheet(
                "color:#b45309;font-size:12px;")
        self._start_batch(items)

    def _start_batch(self, items):
        if not items:
            self.fault_batch_btn.setText("批量测试")
            return
        # PRD 内容先翻译成中文, 报告里「PRD事件内容」列才易读
        try:
            self._ensure_prd_translations()
        except Exception as e:
            log.info(f"[故障模拟] PRD 内容翻译跳过: {e}")
        ordered = _space_duplicate_events(items)
        reordered = ([x[0] for x in ordered] != [x[0] for x in items])
        items = ordered
        missing = (self._voice_check_result or {}).get("missing") or []
        check_text = ""
        if missing:
            check_text = (f"语音包检查：缺失 {len(missing)} 个，"
                          "报告中会注明。\n")
        elif not (self._voice_check_result or {}).get("checked"):
            check_text = "语音包检查未完成，报告中会注明。\n"
        else:
            check_text = "语音包检查：齐全。\n"
        minutes = len(items) * 5.5 / 60
        ok = QMessageBox.question(
            self, "批量测试",
            f"将依次触发 {len(items)} 个事件，约 {minutes:.1f} 分钟。\n"
            + check_text +
            "每条触发后等待日志解析完成，再间隔 2.5 秒触发下一条。\n"
            "期间请保持机器人空闲，是否继续？")
        if ok != QMessageBox.Yes:
            self.fault_batch_btn.setText("批量测试")
            return
        # 新一批必须清掉上一批结果, 否则报告混入旧设备/旧 PRD 的记录
        self._fault_rows = []
        self._fault_report_started = ""
        self.fault_report_btn.setEnabled(False)
        self._batch = {"items": items, "idx": 0, "stop": False}
        self._fault_log_reset()
        self._fault_log_line(
            f"批量测试开始：{len(items)} 个事件", "#b45309")
        if reordered:
            self._fault_log_line(
                "相邻重复事件已错开执行", "#b45309")
        self.fault_batch_btn.setText("停止批量")
        self.runningChanged.emit(True)
        # 开测前先回到空闲: 上一条残留任务会让后续事件不响应(ZY2L 实测)
        self._reset_robot_state()
        QTimer.singleShot(800, self._batch_step)

    def _failed_batch_items(self, batch):
        """初测结果为「不一致」的批条目(按 qid 去重), 用于单独重测。"""
        qids = {str(r.get("qid") or "") for r in self._fault_rows
                if r.get("overall") == hs4_config.STATUS_MISMATCH}
        out, seen = [], set()
        for item in batch.get("items") or []:
            qid = str((item[2] or {}).get("qid") or "")
            if qid and qid in qids and qid not in seen:
                seen.add(qid)
                out.append(item)
        return out

    def _batch_step(self):
        b = self._batch
        if not b:
            return
        if b["stop"] or b["idx"] >= len(b["items"]):
            if not b.get("retry") and not b["stop"]:
                # ★ 批量初测不一致的事件(多为通道抖动/发送阻塞)单独再测一遍,
                #   避免一次偶发失败直接进报告(用户 2026-11-17)
                failed = self._failed_batch_items(b)
                if failed:
                    qids = {str((it[2] or {}).get("qid") or "")
                            for it in failed}
                    self._fault_rows = [
                        r for r in self._fault_rows
                        if str(r.get("qid") or "") not in qids]
                    self._fault_log_line(
                        f"批量初测有 {len(failed)} 条不一致，单独重测…",
                        "#b45309")
                    self._batch = {"items": failed, "idx": 0, "stop": False,
                                   "retry": True}
                    self._batch_step()
                    return
            self._batch = None
            self.runningChanged.emit(False)
            rows = self._report_rows()
            self.fault_batch_btn.setText("批量测试")
            self.fault_report_btn.setEnabled(bool(rows))
            self.fault_status.setText(
                f"批量测试结束：本次报告共 {len(rows)} 个事件结果")
            self.fault_status.setStyleSheet("color:#16a34a;font-size:12px;")
            self._reset_robot_state()
            self._flush_verified_learning()
            if self._fault_rows:
                self._save_batch_report()
            return
        if not self._stream_ready():
            # ★ 机器人网络重启(如 config_wifi_success/wifi_reset)会让推送通道
            #   断十几秒。这时继续触发只会把后面的事件全记成失败, 必须先等恢复。
            waited = int(b.get("wait_secs", 0) or 0)
            if waited >= 300:
                self._fault_log_line(
                    "推送通道 300 秒未恢复，批量测试停止（已测结果保留）",
                    "#dc2626")
                b["stop"] = True
                self._batch_step()      # 走结束分支: 生成已测部分的报告
                return
            b["wait_secs"] = waited + 2
            self.fault_status.setText(
                f"推送通道中断，等待恢复…（已等 {waited} 秒）")
            self.fault_status.setStyleSheet(
                "color:#b45309;font-size:12px;")
            if waited == 0:
                self._fault_log_line(
                    "推送通道中断，批量暂停，等待恢复后继续本条…", "#b45309")
            QTimer.singleShot(2000, self._batch_step)
            return
        b["wait_secs"] = 0
        event, label, prd_row = b["items"][b["idx"]]
        self.fault_batch_btn.setText(
            f"停止批量 {b['idx'] + 1}/{len(b['items'])}")
        if not self._trigger_event(event, label, then=self._batch_after,
                                   fast=True, prd_row=prd_row):
            # 发送失败也跳过, 避免批量卡死
            b["idx"] += 1
            QTimer.singleShot(300, self._batch_step)

    def _batch_after(self, event, label, t0):
        b = self._batch
        if not b:
            return
        b["idx"] += 1
        # 对比验证: 3017 清除状态未见实际效果, 批量不再发送;
        # 日志按本次触发时间截断, 只需留出间隔等日志刷完。
        QTimer.singleShot(self._BATCH_GAP_MS, self._batch_step)

    def _reset_robot_state(self):
        """批量结束后停止清扫/找桩, 让机器回到空闲。"""
        svc = self._service
        if svc is None or not hasattr(svc, "stop_tasks"):
            return
        try:
            results = svc.stop_tasks() or []
        except Exception as e:
            self._fault_log_line(
                f"批量复位失败：{str(e)[:160]}", "#b45309")
            return
        if results:
            summary = "，".join(f"{path}={rc}" for path, rc in results)
            self._fault_log_line(f"批量复位：{summary}", "#16a34a")

    def _clear_faults(self):
        if self._service is None:
            self.fault_status.setText("请先连接设备")
            return
        try:
            ok = bool(self._service.clear_faults())
        except Exception:
            ok = False
        self.fault_status.setText(
            "已发送清除状态(3017)" if ok else "清除失败（设备未连接？）")
        self.fault_status.setStyleSheet(
            "color:#16a34a;font-size:12px;" if ok
            else "color:#dc2626;font-size:12px;")

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
        width = max(260, min(longest + 72, 680))
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
        target_combo = NoWheelComboBox()
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
        # ★ 已连接同一台设备时不重复连接（用户 2026-11-26）：
        #   只有换了 IP 才重新发起，避免点「已连接」又把推流/任务打断。
        if self.connected and ip == self._connected_ip:
            self.status_label.setText(f"已连接 {ip}，无需重复连接")
            self.status_label.setStyleSheet("color:#475569;")
            self._sync_connect_btn()
            return
        # ★ 先占住"连接中"标志: 创建 SDK 服务也要时间, 这期间别让自动连接/再次
        #   点击插进来并发连接(偶发"连接失败"的来源之一)。
        self._connecting = True
        self._sync_connect_btn()
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
                self._sync_connect_btn()

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
            # ★ 切换设备失败时不能把上一台的旧值留在表里: 否则看起来像
            #   "参数没有更新"(用户 2026-11-10 切到 192.168.10.21 的实际现象)。
            self._last_snapshot = {"ok": {}, "failed": {}, "ts": 0}
            self.curve_view.clear_history()
            self._rebuild_table()
            msg = str(info.get("error") or "").strip()
            if msg:
                self.status_label.setText(f"连接失败（{ip}）：{msg}")
                self.status_label.setStyleSheet("color:#dc2626;")
            log.info(f"[连接] {ip} 失败: {msg or '未知原因'}")
            self.connectionChanged.emit(False, "")
            self.connect_finished.emit(False, ip, msg)
            self._flush_pending_connect()
            self._sync_connect_btn()
            if getattr(self, "_relink_left", 0) > 0:
                QTimer.singleShot(self.RELINK_DELAY_MS, self._relink_once)
            return
        self._connected = True
        self._connected_ip = ip
        self._remember_ip(ip)
        # ★ 重连成功后清掉「连接已断开(长时间无数据)」旧提示(用户 2026-11-27)
        self._relink_left = 0
        self.status_label.setText(f"已连接 {ip}")
        self.status_label.setStyleSheet("color:#16a34a;font-size:11px;")
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
        conn_label = "-".join(x for x in (model, sn) if x)
        self.connectionChanged.emit(True, conn_label or ip)
        self.connect_finished.emit(True, ip, "")
        self._flush_pending_connect()
        self._sync_connect_btn()

    def _info_ready(self, ip, extra):
        """后台补到的补充信息（基站版本）：只更新当前连接的那台。"""
        if not self._connected or ip != self._connected_ip:
            return
        self._device_info.update(extra or {})
        self._update_info_label()
        # 型号/SN 可能比连接结果晚到：补进设备列表并通知刷新下拉，
        # 让各页的「型号-SN-IP」显示补全（用户 2026-11-26）。
        model = str(self._device_info.get("model") or "").strip()
        sn = str(self._device_info.get("sn") or "").strip()
        if not (model or sn):
            return
        devs = {str(d.get("ip") or ""): dict(d) for d in self._devices}
        cur = devs.setdefault(str(ip), {"ip": ip})
        if (cur.get("model") != model or cur.get("sn") != sn):
            cur["model"] = model or cur.get("model", "")
            cur["sn"] = sn or cur.get("sn", "")
            self._save_devices(list(devs.values()))

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
                fresh = read_robot_info(self._service, version_retries=3,
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

    def disconnect_device(self, auto=False):
        """断开连接（后台线程；ParamService.stop 最多 join 5 秒，不能卡 UI）。

        auto=True: 断链触发的断开 —— 保留自动重连预算; 用户手动断开则清零。
        """
        if self._connecting or self._disconnecting:
            return
        if not auto:
            self._relink_left = 0
        # ★ 用户主动断开: 取消排队中的切换 IP, 否则断开后又会自动连回去
        self._pending_connect_ip = ""
        self._pending_connect_retries = 0
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
        self._flush_pending_connect()
        self._sync_connect_btn()
        # 断链触发的断开: 延时后自动重连(网络恢复后无需手动点连接)
        if getattr(self, "_relink_left", 0) > 0:
            QTimer.singleShot(self.RELINK_DELAY_MS, self._relink_once)

    def _sync_connect_btn(self):
        """连接按钮文案跟随真实状态: 连接中… / 已连接 / 连接（用户 2026-11-26）。

        只有「当前选中的 IP == 已连接的 IP」才显示已连接；输入了别的 IP 时
        按钮回到「连接」，提示这是一次切换。
        """
        btn = getattr(self, "connect_btn", None)
        if btn is None:
            return
        same = (self.connected
                and self._selected_ip() == self._connected_ip)
        text = ("连接中…" if self._connecting
                else "已连接" if same else "连接")
        if btn.text() != text:
            btn.setText(text)

    def shared_stream(self):
        """全局唯一的设备推送会话(用户 2026-11-27)。

        后续所有需要设备参数/数据流的地方(续航、循环清扫、未来的功能)都从
        这里拿同一条 UploadStream, 不许再自建 —— 多个 libapos 推送会话会互相
        挤掉订阅。未连接时返回 None, 调用方自行回退自建流(离线场景)。
        """
        svc = self._service
        return svc.stream if svc is not None else None

    def pause_param_stream(self):
        """续航测试运行期间让出机器人推送会话，返回是否成功暂停。"""
        svc = self._service
        if svc is None:
            return False
        try:
            svc.pause_stream()
            log.info("[参数] 已暂停设备页推流（续航测试运行中）")
            return True
        except Exception as e:
            log.debug("[参数] 暂停推流失败: %s" % e)
            return False

    def resume_param_stream(self):
        """续航测试结束后恢复设备页推流。"""
        svc = self._service
        if svc is None:
            return
        try:
            svc.resume_stream()
            log.info("[参数] 已恢复设备页推流")
        except Exception as e:
            log.debug("[参数] 恢复推流失败: %s" % e)

    # ── 数据刷新 ──
    @property
    def connected(self):
        return bool(self._connected and self._service is not None)

    def _stream_ready(self):
        """推送通道是否可用(发事件/收设备日志都依赖它)。

        真实服务带 UploadStream; 断网/机器人网络重启时会 running=False。
        测试桩没有 stream 字段, 视为可用, 不改变原有行为。
        """
        svc = self._service
        if svc is None:
            return False
        stream = getattr(svc, "stream", None)
        if stream is None:
            return True
        return bool(getattr(stream, "running", False))

    def _discard_last_fault_row(self, event):
        """撤销上一条记录(通道中断时结果不可信, 重测后只保留最终结果)。"""
        if (self._fault_rows
                and self._fault_rows[-1].get("event") == event):
            self._fault_rows.pop()

    @property
    def connecting(self):
        return bool(self._connecting)

    @property
    def connected_ip(self):
        return self._connected_ip if self._connected else ""

    def connect_to(self, ip):
        """切换到指定 IP 并发起连接（后台线程）。

        返回 True = 已连接、已开始连接或已排队（当前连接结束后续连）；
        False = 没发起（无 IP / 正在连的正好就是同一台）。
        连接结果通过 connect_finished 通知。
        """
        ip = str(ip or "").strip()
        if not ip:
            return False
        if self.connected and ip == self._connected_ip:
            return True
        if self._connecting or self._disconnecting:
            # ★ 旧连接还在进行时切换 IP 不能直接丢掉: 排队, 结束后立刻连新 IP
            #   (用户 2026-11-16: 打开界面初始连接期间切换 IP 没有生效)。
            if (self._disconnecting
                    or ip != self._connect_ip):
                self._pending_connect_ip = ip
                return True
            return False
        if not self._select_ip(ip):
            self.device_combo.setEditText(ip)
        self.connect_device()
        return bool(self._connecting)

    def _flush_pending_connect(self):
        """把排队的新 IP 在旧连接线程彻底退出后接上。"""
        if not self._pending_connect_ip:
            self._pending_connect_retries = 0
            return
        # 旧线程 done 之后还要补读基站版本；这时插新连接会和它抢 SDK 句柄
        busy = (self._connecting or self._disconnecting
                or (self._connect_thread is not None
                    and self._connect_thread.isRunning()))
        if busy:
            self._pending_connect_retries += 1
            if self._pending_connect_retries > 60:   # 约 15 秒仍不退出: 放弃排队
                self._pending_connect_ip = ""
                self._pending_connect_retries = 0
                return
            QTimer.singleShot(250, self._flush_pending_connect)
            return
        self._pending_connect_retries = 0
        ip = self._pending_connect_ip
        self._pending_connect_ip = ""
        if self.connected and ip == self._connected_ip:
            return
        self.connect_to(ip)

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

    #: 断链后自动重连的剩余次数(用户 2026-11-27)
    RELINK_MAX_TRIES = 3
    RELINK_DELAY_MS = 20000

    def _on_link_lost(self):
        """设备侧断链(断网/机器人重启): 切未连接, 并按计划自动重连。"""
        if not self._connected:
            return
        log.warning("[连接] 参数链路已断开, 界面切换为未连接")
        self.status_label.setText("连接已断开（长时间无数据）")
        self.status_label.setStyleSheet("color:#dc2626;")
        self._relink_left = self.RELINK_MAX_TRIES
        self.disconnect_device(auto=True)

    def _relink_once(self):
        """断链后的自动重连尝试; 成功在 _connect_done 里清零。"""
        if (self._connected or self._connecting or self._relink_left <= 0):
            return
        ip = self._selected_ip() or self._connected_ip
        if not ip:
            return
        self._relink_left -= 1
        self.status_label.setText(
            f"正在自动重连（第 {self.RELINK_MAX_TRIES - self._relink_left}"
            f"/{self.RELINK_MAX_TRIES} 次）…")
        self.status_label.setStyleSheet("color:#b45309;font-size:11px;")
        self.connect_device()

    def _poll(self):
        if not self._connected or self._service is None:
            return
        # ★ 断链实时反馈: 参数服务长时间读不到数据会置 _connected=False,
        #   这里立刻把界面/其它页的连接状态切成「未连接」(用户 2026-11-27)。
        try:
            alive = self._service.is_connected()
        except Exception:
            alive = True
        if not alive:
            self._on_link_lost()
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
        for attr in ("_connect_thread", "_disconnect_thread", "_send_worker"):
            t = getattr(self, attr, None)
            if t is not None and t.isRunning():
                t.wait(6000)          # 关窗: 等后台连接/断开/发送收尾
        if self._service is not None:
            try:
                self._service.disconnect()
            except Exception:
                pass
        if self._scan_thread and self._scan_thread.isRunning():
            self._scan_thread.cancel()
            self._scan_thread.wait(5000)
            # ★ 仍在跑就继续等: QThread 还活着时窗口析构会 qFatal(0xC0000409)
            if self._scan_thread.isRunning():
                self._scan_thread.wait()
