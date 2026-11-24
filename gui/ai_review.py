# -*- coding: utf-8 -*-
"""后台 AI 审核学习库映射；PRD 解析后和学习库页共用。"""
from PySide6.QtCore import QThread, Signal

from core.apos import ai_prd, seed_review


class AiReviewWorker(QThread):
    """调用 AI 复核「PRD 含义 → AP 事件」映射, 不卡界面。"""
    done = Signal(object)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self, entries, allowed, cfg, parent=None):
        super().__init__(parent)
        self._entries = entries
        self._allowed = allowed
        self._cfg = cfg

    def _call(self, prompt):
        base = self._cfg.get("base_url") or ""
        key = self._cfg.get("api_key") or ""
        model = self._cfg.get("model") or ""
        return ai_prd.call_relay(
            base, key, model, [{"role": "user", "content": prompt}],
            timeout=300,
            max_tokens=int(self._cfg.get("max_tokens")
                           or ai_prd.DEFAULT_MAX_TOKENS))

    def run(self):
        try:
            corrections, checked, errors = seed_review.audit_entries(
                self._entries, self._allowed, self._call,
                progress=lambda done, total: self.progress.emit(
                    f"AI 审核中 {done}/{total}…"))
            self.done.emit({"corrections": corrections,
                            "checked": checked, "errors": errors})
        except Exception as e:
            self.failed.emit(str(e))
