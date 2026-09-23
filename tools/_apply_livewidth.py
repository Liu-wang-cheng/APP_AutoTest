# -*- coding: utf-8 -*-
"""一次性修复: 输入框宽度实时跟随(逐字) + 历史刷新后也重算。用后删除。"""
p = "gui/main_window.py"
s = open(p, encoding="utf-8").read()

# ── 1) 逐字输入即变宽: 用 lineEdit().textChanged(比 currentTextChanged 更及时) ──
old = '''        combo.currentTextChanged.connect(
            lambda _t, c=combo: self._refresh_name_tip(c))
        self._refresh_name_tip(combo)'''
assert old in s, "text changed wiring"
new = '''        # ★ 逐字输入就重算宽度: editable combo 的 currentTextChanged 不够及时,
        #   lineEdit().textChanged 才是每次按键都发(用户要求"实时变化")
        if combo.lineEdit() is not None:
            combo.lineEdit().textChanged.connect(
                lambda _t, c=combo: self._refresh_name_tip(c))
        combo.currentTextChanged.connect(
            lambda _t, c=combo: self._refresh_name_tip(c))
        self._refresh_name_tip(combo)'''
s = s.replace(old, new)

# ── 2) _reload_name_combo: 解除信号屏蔽后重算宽度(原来只调了 fit_popup_now) ──
old = '''        combo.blockSignals(False)
        if hasattr(combo, "fit_popup_now"):
            combo.fit_popup_now()

    def _purge_name_history(self, hist_key, value):'''
assert old in s, "reload tail"
new = '''        combo.blockSignals(False)
        self._refresh_name_tip(combo)     # ★ 宽度也要跟新内容重算

    def _purge_name_history(self, hist_key, value):'''
s = s.replace(old, new)

# ── 3) _remove_name_history: 同样──────────
old = '''        combo.blockSignals(False)
        # 下拉正打开 → 立即按剩余项重算尺寸(否则仍显示删除前的大小)
        if hasattr(combo, "fit_popup_now"):
            combo.fit_popup_now()'''
assert old in s, "remove tail"
new = '''        combo.blockSignals(False)
        # 下拉正打开 → 立即按剩余项重算尺寸(否则仍显示删除前的大小); 宽度同步
        if hasattr(combo, "fit_popup_now"):
            combo.fit_popup_now()'''
s = s.replace(old, new)

open(p, "w", encoding="utf-8", newline="").write(s)
print("live width ok")
