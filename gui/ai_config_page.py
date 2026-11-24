# -*- coding: utf-8 -*-
"""AI 配置页：PRD 解析用的中转站地址 / API Key / 模型。

* 支持从 `/models` 自动获取中转站可用模型；
* 模型名带 1m 标记时自动勾选「1M 上下文」，PRD 长度上限放大到 1M 级别；
* 参数保存在本机 QSettings（与设备页同一份），PRD 解析时读取。
"""
import os
import time

from PySide6.QtCore import QSettings, QThread, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox,
                               QFileDialog, QFormLayout, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QPushButton,
                               QSpinBox, QTabWidget, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from core.apos import ai_prd, faults, prd_memory, seed_review
from gui.ai_review import AiReviewWorker
from gui.widgets import NoWheelComboBox


class _ModelFetchWorker(QThread):
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, base_url, api_key, parent=None):
        super().__init__(parent)
        self._base = base_url
        self._key = api_key

    def run(self):
        try:
            self.done.emit(ai_prd.fetch_models(self._base, self._key))
        except Exception as e:
            self.failed.emit(str(e))


class AiConfigPage(QWidget):
    def __init__(self, settings=None, parent=None):
        super().__init__(parent)
        self.settings = settings or QSettings("vacuum_test", "case_studio")
        self._fetch_worker = None
        self._audit_worker = None
        self._library_mem = None
        self._audit_mem = None
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        ai_tab = QWidget()
        v = QVBoxLayout(ai_tab)
        v.setContentsMargins(18, 14, 18, 14)
        v.setSpacing(10)
        title = QLabel("AI 配置")
        title.setStyleSheet(
            "font-size:16px;font-weight:700;color:#1e293b;")
        v.addWidget(title)
        tip = QLabel("用于 AI 解析 PRD（事件 → 灯显 / 语音 / 故障码）。"
                     "填写 OpenAI 兼容中转站的参数，参数只保存在本机。")
        tip.setStyleSheet("color:#94a3b8;font-size:11px;")
        tip.setWordWrap(True)
        v.addWidget(tip)
        form = QFormLayout()
        form.setSpacing(10)
        self.url_edit = QLineEdit(
            str(self.settings.value("ai/prd_base_url", "") or "")
            or ai_prd.DEFAULT_BASE_URL)
        self.url_edit.setPlaceholderText(
            "如 http://192.168.160.145:8081/v1（会自动接 /chat/completions）")
        form.addRow("中转站地址", self.url_edit)
        self.key_edit = QLineEdit(
            str(self.settings.value("ai/prd_api_key", "") or ""))
        self.key_edit.setEchoMode(QLineEdit.Password)
        self.key_edit.setPlaceholderText("sk-...（留空则不发送 Authorization）")
        form.addRow("API Key", self.key_edit)

        model_row = QHBoxLayout()
        self.model_combo = NoWheelComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setInsertPolicy(QComboBox.NoInsert)
        self.model_combo.setMinimumWidth(320)
        self.model_combo.setPlaceholderText("先获取模型或直接输入模型名")
        self._add_model(str(self.settings.value("ai/prd_model", "") or ""))
        model_row.addWidget(self.model_combo, 1)
        self.fetch_btn = QPushButton("获取模型")
        self.fetch_btn.setObjectName("chipBtn")
        self.fetch_btn.clicked.connect(self.fetch_models)
        model_row.addWidget(self.fetch_btn)
        form.addRow("模型", model_row)

        self.max_tokens_spin = QSpinBox()
        self.max_tokens_spin.setRange(1024, 131072)
        self.max_tokens_spin.setSingleStep(4096)
        self.max_tokens_spin.setValue(int(self.settings.value(
            "ai/prd_max_tokens", ai_prd.DEFAULT_MAX_TOKENS,
            type=int) or ai_prd.DEFAULT_MAX_TOKENS))
        self.max_tokens_spin.setToolTip(
            "推理模型会先消耗大量 reasoning；太小会被截断(表现为解析失败)")
        form.addRow("最大输出 tokens", self.max_tokens_spin)

        one_m = QHBoxLayout()
        self.one_m_check = QCheckBox("1M 上下文")
        self.one_m_check.setToolTip(
            "模型支持 1M 上下文时勾选：PRD 长度上限从 9 万字符放大到 90 万")
        self.one_m_check.setChecked(bool(self.settings.value(
            "ai/prd_model_1m", False, type=bool)))
        one_m.addWidget(self.one_m_check)
        self.model_hint = QLabel("")
        self.model_hint.setStyleSheet("color:#94a3b8;font-size:11px;")
        one_m.addWidget(self.model_hint)
        one_m.addStretch(1)
        form.addRow("", one_m)

        v.addLayout(form)

        row = QHBoxLayout()
        save = QPushButton("保存")
        save.clicked.connect(self.save)
        row.addWidget(save)
        reset = QPushButton("恢复默认地址")
        reset.setObjectName("chipBtn")
        reset.clicked.connect(
            lambda: self.url_edit.setText(ai_prd.DEFAULT_BASE_URL))
        row.addWidget(reset)
        row.addStretch(1)
        v.addLayout(row)
        save.setMinimumWidth(save.sizeHint().width())
        reset.setMinimumWidth(reset.sizeHint().width())
        self.fetch_btn.setMinimumWidth(self.fetch_btn.sizeHint().width())
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color:#16a34a;font-size:12px;")
        v.addWidget(self.status)
        v.addStretch(1)
        self.model_combo.activated.connect(self._model_selected)
        self.model_hint.setText(
            "已识别 1M 上下文" if self.one_m_check.isChecked() else "")
        root.addWidget(self.tabs)
        self.tabs.addTab(ai_tab, "AI 设置")
        self.tabs.addTab(self._build_library_tab(), "学习库")

    def _build_library_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(18, 14, 18, 14)
        v.setSpacing(8)
        # 学习库操作按钮统一放最上方(用户 2026-11-13)
        row = QHBoxLayout()
        refresh = QPushButton("刷新")
        refresh.setObjectName("chipBtn")
        refresh.clicked.connect(self._reload_library)
        row.addWidget(refresh)
        self.library_audit_btn = QPushButton("AI 审核")
        self.library_audit_btn.setObjectName("chipBtn")
        self.library_audit_btn.setToolTip(
            "用 AI 按 PRD 含义复核全部学习条目（含人工/真机），"
            "自动修正明显错误的事件")
        self.library_audit_btn.clicked.connect(self._audit_library)
        row.addWidget(self.library_audit_btn)
        export = QPushButton("导出种子…")
        export.setObjectName("chipBtn")
        export.setToolTip("导出 verified/ai_review/user 学习项为脱敏 JSON")
        export.clicked.connect(self._export_seed)
        row.addWidget(export)
        row.addStretch(1)
        v.addLayout(row)
        tip = QLabel("本机学习库：真机验证/人工确认的结果优先；"
                     "PRD AI 解析后会自动整合并审核，避免错误映射负优化。")
        tip.setStyleSheet("color:#94a3b8;font-size:11px;")
        tip.setWordWrap(True)
        v.addWidget(tip)
        self.library_stats = QLabel("")
        self.library_stats.setStyleSheet(
            "color:#475569;font-size:12px;font-weight:600;")
        v.addWidget(self.library_stats)
        self.library_table = QTableWidget(0, 6)
        self.library_table.setHorizontalHeaderLabels(
            ["PRD 含义", "AP 事件", "中文名", "类型", "来源", "更新时间"])
        hdr = self.library_table.horizontalHeader()
        for i in range(5):
            hdr.setSectionResizeMode(i, QHeaderView.Interactive)
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for i, width in enumerate((300, 220, 220, 80, 90, 140)):
            self.library_table.setColumnWidth(i, width)
        self.library_table.setEditTriggers(
            QAbstractItemView.NoEditTriggers)
        self.library_table.verticalHeader().setVisible(False)
        self.library_table.setSelectionBehavior(
            QAbstractItemView.SelectRows)
        v.addWidget(self.library_table, 1)
        self.library_status = QLabel("")
        self.library_status.setStyleSheet("color:#16a34a;font-size:12px;")
        self.library_status.setWordWrap(True)
        v.addWidget(self.library_status)
        self._reload_library()
        return w

    @staticmethod
    def _source_label(source):
        return {"verified": "真机验证", "ai_review": "AI 审核",
                "user": "用户确认", "seed": "种子库",
                "ai": "AI 学习", "local": "本地候选",
                "memory": "旧记忆"}.get(str(source or ""), str(source or ""))

    def _reload_library(self):
        mem = prd_memory.PrdMemory()
        try:
            mem.load_seed_file(prd_memory.default_seed_path())
        except Exception:
            pass
        self._library_mem = mem
        items = [(k, v) for k, v in mem.items.items()
                 if isinstance(v, dict)]
        order = {"verified": 0, "ai_review": 1, "user": 2, "seed": 3,
                 "ai": 4, "local": 5, "memory": 6}
        items.sort(key=lambda kv: (order.get(kv[1].get("source"), 9),
                                   kv[0]))
        self.library_table.setRowCount(len(items))
        counts = {}
        for i, (key, val) in enumerate(items):
            src = str(val.get("source") or "")
            counts[src] = counts.get(src, 0) + 1
            vals = [key, val.get("event", ""), val.get("event_name", ""),
                    val.get("kind", ""), self._source_label(src),
                    val.get("verified_at") or val.get("updated", "")]
            for col, text in enumerate(vals):
                item = QTableWidgetItem(str(text))
                item.setToolTip(str(text))
                self.library_table.setItem(i, col, item)
        total = len(items)
        self.library_stats.setText(
            f"共 {total} 条 · 真机验证 {counts.get('verified', 0)} · "
            f"AI 审核 {counts.get('ai_review', 0)} · "
            f"用户确认 {counts.get('user', 0)} · "
            f"种子 {counts.get('seed', 0)}")
        self.library_status.setText("")

    def _library_event_catalog(self):
        """审核候选事件：AP 配置 > 内置事件翻译；含自定义事件名。"""
        expect = {}
        path = str(self.settings.value(
            "apos/fault_event_config", "") or "")
        if path and os.path.exists(path):
            try:
                expect = faults.load_event_config(path)
            except Exception:
                expect = {}
        return seed_review.event_catalog(expect)

    def _audit_library(self):
        if self._audit_worker is not None:
            return
        cfg = self.values()
        if not cfg["base_url"] or not cfg["model"]:
            self.library_status.setText("请先在「AI 设置」里填写中转站地址和模型")
            self.library_status.setStyleSheet(
                "color:#dc2626;font-size:12px;")
            return
        mem = self._library_mem or prd_memory.PrdMemory()
        entries, skipped = [], 0
        for key, val in mem.items.items():
            if not isinstance(val, dict) or not val.get("event"):
                continue
            # 用户 2026-11-16: 人工/真机记录也一起审核, 允许 AI 修正覆盖
            entries.append({
                "key": key,
                "event": str(val.get("event") or ""),
                "event_name": str(val.get("event_name") or ""),
                "voice": str(val.get("voice") or ""),
                "voice_text": str(val.get("voice_text") or key),
            })
        if not entries:
            self.library_status.setText(
                "学习库没有可审核的 AI/种子条目"
                + (f"（已跳过 {skipped} 条人工/真机结果）" if skipped else ""))
            self.library_status.setStyleSheet(
                "color:#b45309;font-size:12px;")
            return
        # 有 AP 配置时候选只来自 AP 平台真实事件; 当前事件不合法时 AI 会给出替代项
        allowed = self._library_event_catalog()
        self._audit_mem = mem
        self._audit_skipped = skipped
        self._audit_allowed = allowed
        self.library_audit_btn.setEnabled(False)
        self.library_audit_btn.setText("审核中…")
        self.library_status.setText(
            f"AI 审核中（{len(entries)} 条，已跳过 {skipped} 条人工/真机结果）…")
        self.library_status.setStyleSheet(
            "color:#b45309;font-size:12px;")
        self._audit_worker = AiReviewWorker(entries, allowed, cfg, self)
        self._audit_worker.done.connect(self._on_library_audit_done)
        self._audit_worker.failed.connect(self._on_library_audit_failed)
        self._audit_worker.progress.connect(self._on_library_audit_progress)
        self._audit_worker.start()

    def _on_library_audit_progress(self, text):
        self.library_status.setText(text)
        self.library_status.setStyleSheet(
            "color:#b45309;font-size:12px;")

    def _on_library_audit_failed(self, message):
        self._audit_worker = None
        self.library_audit_btn.setEnabled(True)
        self.library_audit_btn.setText("AI 审核")
        self.library_status.setText(
            f"AI 审核失败：{str(message or '未知错误')[:300]}")
        self.library_status.setStyleSheet(
            "color:#dc2626;font-size:12px;")

    def _on_library_audit_done(self, result):
        self._audit_worker = None
        self.library_audit_btn.setEnabled(True)
        self.library_audit_btn.setText("AI 审核")
        result = result if isinstance(result, dict) else {}
        corrections = result.get("corrections") or []
        checked = int(result.get("checked") or 0)
        errors = result.get("errors") or []
        mem = self._audit_mem
        allowed = getattr(self, "_audit_allowed", {})
        applied = skipped = 0
        if corrections and mem is not None:
            fresh = prd_memory.PrdMemory(mem.path)
            for corr in corrections:
                key = str(corr.get("key") or "")
                event = str(corr.get("event") or "")
                old = mem.items.get(key) or {}
                if not key or not event or not old:
                    continue
                if str(old.get("event") or "") == event:
                    continue
                val = dict(old)
                val["event"] = event
                val["event_name"] = str(allowed.get(event) or event)
                fresh.put(key, val, source="ai_review", force=True,
                          save=False)
                applied += 1
            if applied:
                fresh.save()
        self._reload_library()
        msg = f"AI 审核完成：检查 {checked} 条，修正 {applied} 条"
        skip_n = skipped or int(getattr(self, "_audit_skipped", 0) or 0)
        if skip_n:
            msg += f"，跳过人工/真机 {skip_n} 条"
        if errors:
            msg += f"；{len(errors)} 批失败：{str(errors[0])[:120]}"
        self.library_status.setText(msg)
        self.library_status.setStyleSheet(
            "color:#b45309;font-size:12px;" if errors
            else "color:#16a34a;font-size:12px;")

    def _export_seed(self):
        from core.driver import BASE_DIR
        stamp = time.strftime("%Y%m%d_%H%M%S")
        default = os.path.join(BASE_DIR, "config", "fault_events",
                               f"learned_seed_{stamp}.json")
        path, _ = QFileDialog.getSaveFileName(
            self, "导出学习种子", default, "JSON (*.json)")
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".json"
        try:
            n = prd_memory.PrdMemory().export_seed(
                path, sources=("verified", "ai_review", "user"))
        except Exception as e:
            self.library_status.setText(f"导出失败：{e}")
            self.library_status.setStyleSheet(
                "color:#dc2626;font-size:12px;")
            return
        self.library_status.setText(f"已导出 {n} 条种子：{path}")
        self.library_status.setStyleSheet("color:#16a34a;font-size:12px;")

    def _add_model(self, model, select=True):
        model = str(model or "").strip()
        if not model:
            return
        label = f"{model}（1M）" if ai_prd.is_1m_model(model) else model
        idx = self.model_combo.findData(model)
        if idx < 0:
            self.model_combo.addItem(label, model)
            idx = self.model_combo.count() - 1
        if select:
            self.model_combo.setCurrentIndex(idx)

    def _current_model(self):
        idx = self.model_combo.currentIndex()
        text = self.model_combo.currentText().strip()
        if idx >= 0 and self.model_combo.itemText(idx) == text:
            return str(self.model_combo.itemData(idx) or "")
        return text.replace("（1M）", "").strip()

    def _model_selected(self, *_):
        model = self._current_model()
        is_1m = ai_prd.is_1m_model(model)
        if model:
            self.one_m_check.setChecked(is_1m)
        self.model_hint.setText(
            "已识别 1M 上下文" if is_1m else
            ("非 1M 模型：PRD 过长会被截断" if model else ""))

    def fetch_models(self):
        base = self.url_edit.text().strip()
        if not base:
            self.status.setText("请先填写中转站地址")
            self.status.setStyleSheet("color:#dc2626;font-size:12px;")
            return
        self.fetch_btn.setEnabled(False)
        self.fetch_btn.setText("获取中…")
        self.status.setText("正在从 /models 获取模型列表…")
        self.status.setStyleSheet("color:#b45309;font-size:12px;")
        self._fetch_worker = _ModelFetchWorker(base, self.key_edit.text(),
                                               self)
        self._fetch_worker.done.connect(self._on_models)
        self._fetch_worker.failed.connect(self._on_models_failed)
        self._fetch_worker.start()

    def _on_models(self, models):
        self.fetch_btn.setEnabled(True)
        self.fetch_btn.setText("获取模型")
        self._fetch_worker = None
        current = self._current_model()
        for model in models or []:
            self._add_model(model, select=False)
        if current:
            self._add_model(current)
        self._model_selected()
        self.status.setText(f"已获取 {len(models or [])} 个模型"
                            "（带「1M」标记的为 1M 上下文）")
        self.status.setStyleSheet("color:#16a34a;font-size:12px;")

    def _on_models_failed(self, message):
        self.fetch_btn.setEnabled(True)
        self.fetch_btn.setText("获取模型")
        self._fetch_worker = None
        self.status.setText(f"获取模型失败：{str(message)[:300]}")
        self.status.setStyleSheet("color:#dc2626;font-size:12px;")

    def values(self):
        return {
            "base_url": self.url_edit.text().strip(),
            "api_key": self.key_edit.text().strip(),
            "model": self._current_model(),
            "model_1m": self.one_m_check.isChecked(),
            "max_tokens": self.max_tokens_spin.value(),
        }

    def save(self):
        vals = self.values()
        self.settings.setValue("ai/prd_base_url", vals["base_url"])
        self.settings.setValue("ai/prd_api_key", vals["api_key"])
        self.settings.setValue("ai/prd_model", vals["model"])
        self.settings.setValue("ai/prd_model_1m", vals["model_1m"])
        self.settings.setValue("ai/prd_max_tokens", vals["max_tokens"])
        self.status.setText("已保存，可回到「设备参数 → 故障模拟」解析 PRD")
        self.status.setStyleSheet("color:#16a34a;font-size:12px;")
