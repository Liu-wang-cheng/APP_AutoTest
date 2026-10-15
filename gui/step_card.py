# -*- coding: utf-8 -*-
# 由 gui/main_window.py 拆分而来(结构整理 2026-10-15):
# 步骤卡片: 字段控件工厂 + 卡片本体 + 卡片列表/编辑器。
# ★ 拆分只为可读性 —— 这批类原先就在同一个模块里, 行为未改动。

# 只导入本模块用到的名字(结构整理 2026-10-15)
import copy
import os
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QDoubleValidator, QIntValidator
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFrame, QGridLayout,
                                QHBoxLayout, QLabel, QLineEdit, QPushButton,
                                QScrollArea, QToolButton, QVBoxLayout, QWidget)
from core import registry as reg
from core.driver import BASE_DIR
from core.logger import get_logger
from gui import schema
from gui.style import CATEGORY_COLORS, _FIELD_LABEL_W
from gui.widgets import ClickableFrame, _FitCombo, _FormCheckBox, make_action_menu

log = get_logger()


def _make_field_widget(field, value, steps=None, exclude_index=None,
                       preconditions=None):
    """按 schema 字段类型建控件,返回 (widget, 取值getter)。
    steps/exclude_index: stepshot 类型(基准图选步骤下拉)用的上下文"""
    t = field["type"]
    hint = field.get("hint", "")
    if t == "template":
        from core import vision
        combo = _FitCombo()                 # 宽度自适应 + popup 尺寸按最长模板名
        combo.setEditable(True)
        combo.addItems(vision.list_templates())
        if value:
            combo.setCurrentText(str(value))
        combo.lineEdit().setPlaceholderText(hint or "选择模板")
        combo.currentTextChanged.connect(lambda _t, c=combo: c.fit_width_to_items())

        def _reload_templates():
            """每次打开下拉重扫模板目录 —— 新截的模板立刻可选, 不用重加步骤

            ★ 弹层**已经开着**时绝不重建: QComboBox.clear() 会把已展开的弹层关掉,
              表现就是"点一下立马收起"(概率复现, 取决于点击是否触发了二次 showPopup)。
            """
            if combo.view().isVisible():
                return
            cur = combo.currentText()
            names = list(vision.list_templates())
            if names == [combo.itemText(i) for i in range(combo.count())]:
                return                      # 没变化就别动(免得打断用户正在选的项)
            combo.blockSignals(True)         # 重建期间别触发 currentTextChanged/自动保存
            combo.clear()
            combo.addItems(names)
            if cur:
                combo.setCurrentText(cur)
            combo.blockSignals(False)
            combo.fit_width_to_items()       # 名单变了宽度也要跟着变

        combo.set_refresh(_reload_templates)
        combo.fit_width_to_items()
        return combo, combo.currentText
    if t == "stepshot":
        # 基准图下拉: 选项 = 当前用例中开启了自动截图的其他步骤(用户要求)
        # + 2026-10-14: 子步骤(if 分支里点过/截过图的) + 前置项的截图
        combo = _FitCombo()                 # 宽度自适应: "步骤N: 描述" 可能较长
        options = []
        for i, st in enumerate(steps or []):
            if i == exclude_index:
                continue
            # ★ "这一步会有截图" = 勾了自动截图 **或**它是点击类动作(点击强制截图, 见
            #   core.registry.is_click_action) —— 判据要与执行端一致, 否则点击步骤明明
            #   会产出截图, 却在下拉里挑不到(用户要求 2026-10-11)。
            if not (st.get("screenshot")
                    or reg.is_shot_action(reg.primary_action_key(st))):
                continue
            desc = st.get("desc") or "(无描述)"
            options.append((f"step:{i + 1}", f"步骤{i + 1}: {desc}"))
            # ★ 子步骤(用户 2026-10-14): then/else 里点过/截过图的也算 ——
            #   值用 shot:<描述>, 执行端按结果表描述匹配(忽略分支前缀)
            for branch in ("then", "else"):
                for j, sub in enumerate(st.get(branch) or []):
                    if not (sub.get("screenshot") or
                            reg.is_click_action(reg.primary_action_key(sub))):
                        continue
                    sub_desc = sub.get("desc") or "(无描述)"
                    mark = "成立" if branch == "then" else "不成立"
                    options.append((f"shot:{sub_desc}",
                                    f"↳ 步骤{i + 1}[条件{mark}]子{j + 1}: {sub_desc}"))
        # ★ 前置项截图(用户 2026-10-14): 前置先于用例执行, 是很常用的对比基准
        for pre in preconditions or []:
            if not pre.get("enabled", True):
                continue
            from core.session import item_label as _il
            label = _il(pre)
            options.append((f"pre:{label}", f"前置: {label}"))
        for v, label in options:
            combo.addItem(label, v)
        if value and value not in [v for v, _ in options]:
            combo.addItem(f"(手动路径) {value}", value)   # 旧路径/自定义值保留显示
        if combo.count() == 0:
            combo.addItem("(前面没有会截图的步骤)", "")
            combo.setEnabled(False)
        if value:
            k = combo.findData(value)
            if k >= 0:
                combo.setCurrentIndex(k)
        combo.currentTextChanged.connect(lambda _t, c=combo: c.fit_width_to_items())
        combo.fit_width_to_items()
        return combo, combo.currentData
    if t == "bool":
        w = _FormCheckBox()          # 整块可点 + 高度够, 见类注释(真机闪动/点不动)
        w.setChecked(bool(value))
        return w, w.isChecked
    if t == "int":
        w = QLineEdit("" if value is None else str(value))
        w.setValidator(QIntValidator(-100000, 100000))
        w.setPlaceholderText(hint)
        w.setToolTip(hint)                 # 多行提示在单行 placeholder 里显示不全
        return w, _int_of(w)
    if t == "float":
        w = QLineEdit("" if value is None else str(value))
        w.setValidator(QDoubleValidator(0, 100000, 4))
        w.setPlaceholderText(hint)
        return w, _float_of(w)
    if t == "int4":
        w = QLineEdit(",".join(map(str, value)) if value else "")
        w.setPlaceholderText(hint or "x1,y1,x2,y2")
        return w, _int4_of(w)
    # text: 未编辑时保留原值(列表/数字等非字符串类型不被表单转成字符串)
    w = QLineEdit("" if value is None else str(value))
    w.setPlaceholderText(hint)
    w.setToolTip(hint)                     # 多行提示在单行 placeholder 里显示不全
    original_value = value
    original_display = "" if value is None else str(value)

    def text_getter(w=w):
        if w.text() == original_display:
            return original_value
        return w.text()

    return w, text_getter


def _int_of(w):
    def get():
        txt = w.text().strip()
        return int(txt) if txt.lstrip("-").isdigit() else None
    return get


def _float_of(w):
    def get():
        try:
            return float(w.text().strip())
        except ValueError:
            return None
    return get


def _int4_of(w):
    def get():
        parts = [p.strip() for p in w.text().split(",") if p.strip()]
        if not parts:
            return []
        if len(parts) != 4 or not all(p.lstrip("-").isdigit() for p in parts):
            return None
        return [int(p) for p in parts]
    return get


class StepCard(QFrame):
    """单个步骤卡片: 折叠=摘要行,展开=参数表单(高级参数另收一层)

    sub_index 为 None 时是顶层步骤卡片;否则是 else 子步骤卡片(缩进显示)。
    """

    def __init__(self, main, index, parent_index=None, sub_index=None, branch="else"):
        super().__init__()
        self.main = main
        self.index = index
        self.parent_index = parent_index
        self.sub_index = sub_index
        self.branch = branch        # 子步骤所属分支: "then"(条件成立时) / "else"(不成立时)
        self.is_sub = sub_index is not None
        if self.is_sub:
            self.step = main.steps[parent_index].setdefault(branch, [])[sub_index]
        else:
            self.step = main.steps[index]
        self.getters = {}
        self.widgets = {}  # 表单控件引用(测试/程序化设值用)
        self.key = (index,) if not self.is_sub else (parent_index, branch, sub_index)
        if self.is_sub:
            self.expanded = (main.expanded_key == self.key)
        else:
            # 顶层卡片: 本身展开,或其 else 子步骤处于展开态,都视为打开
            ek = main.expanded_key
            self.expanded = (ek == self.key or
                             (isinstance(ek, tuple) and len(ek) >= 2 and ek[0] == index))
        if self.is_sub:
            self.setObjectName("subStepCardOpen" if self.expanded else "subStepCard")
        else:
            self.setObjectName("stepCardOpen" if self.expanded else "stepCard")
        self.setCursor(Qt.PointingHandCursor if not self.expanded else Qt.ArrowCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(30 if self.is_sub else 12, 5, 10, 7)
        lay.setSpacing(5)
        self._build_header(lay)
        if self.expanded:
            self._build_form(lay)

    # ── 折叠态: 编号 + 组件标签 + 摘要 + 徽标 + 操作按钮 ──
    def _build_header(self, lay):
        header = ClickableFrame()
        header.setStyleSheet("QFrame { background: transparent; }")
        header.setCursor(Qt.PointingHandCursor)
        header.clicked.connect(self._header_clicked)
        head = QHBoxLayout(header)
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        if self.is_sub:
            num = QLabel(f"{self.sub_index + 1})")
            num.setFixedWidth(18)
        else:
            num = QLabel(str(self.index + 1))
            num.setFixedWidth(18)
        num.setObjectName("numLabel")
        num.setAlignment(Qt.AlignCenter)
        head.addWidget(num)

        # ★ if 子步骤标出所属分支(用户 2026-10-12: "if 分支的子步骤在测试步骤详情里也做了
        #   区分吗")。then 全在前、else 全在后地缩进连排, 光看序号分不清第几张是 else 的第一
        #   步; 这里补一个徽标, 与执行结果里的 `[条件成立]`/`[条件不成立]` 前缀对得上。
        if self.is_sub:
            badge = QLabel("▸then" if self.branch == "then" else "▸else")
            badge.setObjectName("branchBadge")
            badge.setToolTip("条件成立时执行的子步骤" if self.branch == "then"
                             else "条件不成立时执行的子步骤")
            head.addWidget(badge)

        action = self.main._find_action(self.step)
        color = CATEGORY_COLORS.get(action["category"], "#64748b") if action else "#94a3b8"
        chip = QLabel(action["label"] if action else "未知")
        chip.setObjectName("chip")
        chip.setStyleSheet(f"background: {color};")
        head.addWidget(chip)

        self.summary_label = QLabel()
        head.addWidget(self.summary_label, 1)
        self._refresh_summary()

        if self.is_sub:
            ops = [("▲", "上移", lambda: self.main.move_sub(self.parent_index, self.sub_index, -1,
                                                          self.branch)),
                   ("▼", "下移", lambda: self.main.move_sub(self.parent_index, self.sub_index, 1,
                                                          self.branch)),
                   ("✕", "删除", lambda: self.main.del_sub(self.parent_index, self.sub_index,
                                                         self.branch))]
        else:
            ops = [("▲", "上移", lambda: self.main.move_step(self.index, -1)),
                   ("▼", "下移", lambda: self.main.move_step(self.index, 1)),
                   ("⧉", "复制", lambda: self.main.dup_step(self.index)),
                   ("✕", "删除", lambda: self.main.del_step(self.index))]
        for text, tip, fn in ops:
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            b.setFixedSize(22, 20)
            if text == "✕":
                b.setStyleSheet("QToolButton:hover { color:#dc2626; background:#fef2f2; }")
            b.clicked.connect(fn)
            head.addWidget(b)
        lay.addWidget(header)

    def mousePressEvent(self, event):
        """点击卡片非头部区域: 折叠态 → 展开;展开态不响应(避免编辑时误收起)"""
        if not self.expanded and event.button() == Qt.LeftButton:
            self.main.expand_card(self.key)
        super().mousePressEvent(event)

    def _header_clicked(self):
        """点击头部行: 展开/收回(toggle);子步骤收回时回到父级"""
        if self.is_sub:
            if self.main.expanded_key == self.key:
                self.main.expand_card((self.parent_index,))
            else:
                self.main.expand_card(self.key)
            return
        if self.main.expanded_key == self.key:
            self.main.expand_card(None)
        elif isinstance(self.main.expanded_key, tuple) and \
                len(self.main.expanded_key) == 2 and self.main.expanded_key[0] == self.index:
            # 父卡片因子步骤展开而打开: 点击头部切换到编辑父卡片本身
            self.main.expand_card(self.key)
        else:
            self.main.expand_card(self.key)

    def _refresh_summary(self):
        # 徽标(else/截图/超时/等待/重试)统一在 schema.step_summary 里拼装
        self.summary_label.setText(schema.step_summary(self.step))

    # ── 展开态: 参数表单 ──
    def _build_form(self, lay):
        action = self.main._find_action(self.step)
        form = QWidget()
        fv = QVBoxLayout(form)
        fv.setContentsMargins(26, 0, 4, 0)
        fv.setSpacing(5)

        # 测试步骤描述(desc)始终可编辑
        desc_grid = QGridLayout()
        desc_grid.setHorizontalSpacing(10)
        self._add_field(desc_grid, 0, {"key": "desc", "label": "测试步骤描述",
                                       "type": "text", "hint": "显示在报告和结果面板"})
        fv.addLayout(desc_grid)

        if action:
            grid = QGridLayout()
            grid.setHorizontalSpacing(10)
            grid.setVerticalSpacing(5)
            r = 0
            for f in action["fields"]:
                if f["type"] == "group":
                    r = self._add_group_field(grid, r, f)
                else:
                    r = self._add_field(grid, r, f)
            if action["fields"]:
                fv.addLayout(grid)
            if action.get("tip"):
                tip = QLabel("提示: " + action["tip"])
                tip.setStyleSheet("color:#b45309; font-size:11px;")
                fv.addWidget(tip)

        # 高级参数(通用字段,默认收起)
        self.adv_btn = QToolButton()
        self.adv_btn.setText("▸ 高级参数(截图/延时/超时/重试)")
        self.adv_btn.setStyleSheet("color:#2563eb;")
        self.adv_btn.clicked.connect(self._toggle_advanced)
        fv.addWidget(self.adv_btn)
        self.adv_grid_host = QWidget()
        adv_grid = QGridLayout(self.adv_grid_host)
        adv_grid.setContentsMargins(0, 0, 0, 0)
        adv_grid.setHorizontalSpacing(10)
        adv_grid.setVerticalSpacing(5)
        adv_fields = [f for f in schema.GENERIC_FIELDS if f["key"] != "desc"]
        # ★ 点击类动作: "自动截图"**强制开启且不可关闭**(用户要求 2026-10-11:
        #   "点击操作默认要截图, 不可以关闭") —— 报告里必须能看到"当时点在哪"。
        force_shot = bool(action) and reg.is_click_action(action["key"])
        for r, f in enumerate(adv_fields):
            self._add_field(adv_grid, r, f)
            if force_shot and f["key"] == "screenshot":
                w = self.widgets.get("screenshot")
                if isinstance(w, QCheckBox):
                    # ★ blockSignals: 只改界面表现, **不要**触发写回 —— 否则一打开卡片就把
                    #   `screenshot: true` 写进数据, 用例被标成"有修改"、YAML 也跟着变。
                    #   真正的强制截图在执行端(见 runner._execute 的 click_step)。
                    w.blockSignals(True)
                    w.setChecked(True)
                    w.blockSignals(False)
                    w.setEnabled(False)
                    w.setToolTip("点击操作**默认截图**(会把点击位置标在图上并写进报告), "
                                 "不可关闭 —— 点错地方是最难查的一类问题")
        self.adv_grid_host.setVisible(False)
        fv.addWidget(self.adv_grid_host)

        # if / if not: then / else 两个分支的子步骤管理区(子卡片在 render_cards 里紧随其后渲染)
        if action and action["key"] in ("if", "if not"):
            self._build_branch_sections(fv)

        lay.addWidget(form)

    def _build_branch_sections(self, fv):
        """条件成立(then) / 不成立(else) 两个分支各一组子步骤(子步骤见下方缩进卡片)。

        ★ 用户要求(2026-10-11): then 分支原来没有子步骤, 只能靠"条件取反 + else"绕。
        """
        for branch, text in (("then", "then 分支(条件成立时执行,子步骤见下方缩进卡片)"),
                             ("else", "else 分支(条件不满足时执行,子步骤见下方缩进卡片)")):
            row = QHBoxLayout()
            lbl = QLabel(text)
            lbl.setObjectName("fieldLabel")
            row.addWidget(lbl)
            add_btn = QPushButton("＋ 子步骤")
            add_btn.setObjectName("chipBtn")
            add_btn.setToolTip(f"给「{branch}」分支加一条子步骤")
            add_btn.setMenu(make_action_menu(
                add_btn, lambda k, b=branch: self.main.add_sub(self.index, k, b)))
            row.addWidget(add_btn)
            row.addStretch()
            fv.addLayout(row)

    def _add_field(self, grid, row, field):
        lbl = QLabel(field["label"])
        lbl.setObjectName("fieldLabel")
        lbl.setFixedWidth(_FIELD_LABEL_W)   # ★ 标签列固定宽(否则被 grid 拉伸)
        lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        grid.addWidget(lbl, row, 0)
        grid.setColumnStretch(1, 1)         # 控件列吃剩余空间(控件靠左, 不居中)
        w, getter = _make_field_widget(field, self.step.get(field["key"]),
                                       steps=self.main.steps,
                                       exclude_index=self.index,
                                       preconditions=getattr(self.main,
                                                             "preconditions", None))
        if field["key"] == "compare_area":
            # ★ 预览对比范围(用户 2026-10-14): 点一下连当前设备截屏, 按本框现值
            #   画标注图弹窗 —— 范围合不合理, 不用跑用例就能确认。
            #   信号/宽度仍绑在**原控件** w 上; box 只负责网格布局(否则 editingFinished
            #   等属性在容器上找不到, 保存回写会断 —— 实测踩过)。
            from PySide6.QtWidgets import (
                QPushButton as _PB, QWidget as _QW, QHBoxLayout as _HL)
            btn = _PB("预览范围")
            btn.setToolTip("连接当前设备截屏, 按本框现值画范围标注图\n"
                           "确认对比范围合不合理(不跑用例)")
            btn.clicked.connect(lambda: self.main.preview_compare_area(getter()))
            box = _QW()
            hb = _HL(box)
            hb.setContentsMargins(0, 0, 0, 0)
            hb.addWidget(w)
            hb.addWidget(btn)
            grid.addWidget(box, row, 1, Qt.AlignLeft)
            self.widgets[field["key"]] = w
            self.getters[field["key"]] = getter
            if hasattr(w, "changed"):
                w.changed.connect(self._write_back)
            else:
                w.editingFinished.connect(self._write_back)
            return row + 1
        if isinstance(w, _FormCheckBox):
            pass                     # ★ 勾选框保持自然大小: 拉宽会产生"点不动/点哪儿都算"
        elif isinstance(w, _FitCombo):
            w.fit_width_to_items()   # 选择型下拉: 按最长项(内容驱动, 上限 420)
        else:
            w.setMinimumWidth(200)
            w.setMaximumWidth(300)   # 收紧(原 430 右侧大片留白)
        grid.addWidget(w, row, 1, Qt.AlignLeft)   # 靠左紧贴标签(拉伸列内不居中)
        self.widgets[field["key"]] = w
        self.getters[field["key"]] = getter
        if hasattr(w, "changed"):             # 组合控件(点击模板勾选): 自带 changed 信号
            w.changed.connect(self._write_back)
        elif isinstance(w, QComboBox):        # stepshot 下拉: 选中即写回
            w.currentIndexChanged.connect(self._write_back)
        else:
            signal = w.toggled if isinstance(w, QCheckBox) else w.editingFinished
            signal.connect(self._write_back)
        return row + 1

    def _add_group_field(self, grid, row, field):
        lbl = QLabel(field["label"])
        lbl.setObjectName("fieldLabel")
        grid.addWidget(lbl, row, 0, Qt.AlignTop)
        host = QWidget()
        hv = QVBoxLayout(host)
        hv.setContentsMargins(0, 0, 0, 0)
        hv.setSpacing(4)
        cur = self.step.get(field["key"])
        cur = cur if isinstance(cur, dict) else {}
        sub_getters = {}
        for sub in field["fields"]:
            row_h = QHBoxLayout()
            sl = QLabel(sub["label"])
            sl.setObjectName("fieldLabel")
            sl.setFixedWidth(90)
            row_h.addWidget(sl)
            w, getter = _make_field_widget(sub, cur.get(sub["key"]),
                                           steps=self.main.steps,
                                           exclude_index=self.index)
            if not isinstance(w, _FormCheckBox):
                w.setMinimumWidth(200)     # 勾选框保持自然大小(见 _FormCheckBox)
            row_h.addWidget(w)
            hv.addLayout(row_h)
            sub_getters[sub["key"]] = getter
            if hasattr(w, "changed"):         # 组合控件(点击模板勾选)
                w.changed.connect(self._write_back)
            elif isinstance(w, QComboBox):
                w.currentIndexChanged.connect(self._write_back)
            else:
                signal = w.toggled if isinstance(w, QCheckBox) else w.editingFinished
                signal.connect(self._write_back)
        grid.addWidget(host, row, 1)
        self.getters[field["key"]] = ("__group__", sub_getters)
        return row + 1

    def _toggle_advanced(self):
        vis = not self.adv_grid_host.isVisible()
        self.adv_grid_host.setVisible(vis)
        self.adv_btn.setText("▾ 高级参数(截图/延时/超时/重试)" if vis
                             else "▸ 高级参数(截图/延时/超时/重试)")

    def _write_back(self, *args):
        """表单编辑结束 → 写回步骤,刷新摘要与 YAML"""
        step = self.step
        for key, getter in self.getters.items():
            if isinstance(getter, tuple) and getter[0] == "__group__":
                vals = {k: g() for k, g in getter[1].items()}
                vals = {k: v for k, v in vals.items() if v not in (None, "")}
                step[key] = vals if vals else True
            else:
                v = getter()
                if v is not None:
                    step[key] = v
                else:
                    w = self.widgets.get(key)
                    # ★ 输入框被**清空** = 清掉这个参数(用默认值), 而不是保留旧值 ——
                    #   用户 2026-10-11 反馈"超时和延时改不到空白": 整型/浮点框清空时
                    #   取值是 None, 而这里原来直接跳过 None → 旧值一直留着。
                    if isinstance(w, QLineEdit) and not w.text().strip():
                        step.pop(key, None)
        self._refresh_summary()
        self.main.on_card_edited()


class _StepsHost(QObject):
    """前置条件里「步骤编辑」的迷你宿主 —— 让 StepCard 等用例编辑组件原样复用。

    StepCard 只依赖宿主这组接口(steps / expanded_key / _find_action /
    expand_card / move_* / dup_* / del_* / add_sub / on_card_edited),
    这里全部实现; 渲染由 _StepsEditor 负责(逻辑与 MainWindow.render_cards 一致,
    含 else 子步骤), 所以前置条件的步骤编辑与用例编辑功能完全相同。
    """

    changed = Signal()

    def __init__(self, steps=None):
        super().__init__()
        self.steps = [dict(x) for x in (steps or [])]
        self.expanded_key = None

    @staticmethod
    def _find_action(step):
        # ★ 用 schema.display_action_key(先问引擎的判据, 再按 schema 兜底): 卡片显示的动作必须
        #   就是会被执行的那个 —— 原来按 dict 键序取, {assert, click} 会显示"断言"却
        #   执行"点击"(2026-10-11 实测)。
        key = schema.display_action_key(step)
        return schema.ACTION_BY_KEY.get(key) if key else None

    def expand_card(self, key):
        self.expanded_key = key
        self.changed.emit()

    def on_card_edited(self):
        self.changed.emit()

    # ── 与 MainWindow 同名同义的操作 ──
    def move_step(self, index, delta):
        j = index + delta
        if 0 <= index < len(self.steps) and 0 <= j < len(self.steps):
            self.steps[index], self.steps[j] = self.steps[j], self.steps[index]
            self.expanded_key = (j,)
            self.changed.emit()

    def dup_step(self, index):
        if 0 <= index < len(self.steps):
            self.steps.insert(index + 1, copy.deepcopy(self.steps[index]))
            self.expanded_key = (index + 1,)
            self.changed.emit()

    def del_step(self, index):
        if 0 <= index < len(self.steps):
            self.steps.pop(index)
            self.expanded_key = None
            self.changed.emit()

    def add_sub(self, parent_index, action_key, branch="else"):
        if not (0 <= parent_index < len(self.steps)):
            return
        sub = schema.new_step(action_key)
        subs = self.steps[parent_index].setdefault(branch, [])
        subs.append(sub)
        self.expanded_key = (parent_index, branch, len(subs) - 1)
        self.changed.emit()

    def move_sub(self, parent_index, sub_index, delta, branch="else"):
        subs = self.steps[parent_index].get(branch) or []
        j = sub_index + delta
        if 0 <= sub_index < len(subs) and 0 <= j < len(subs):
            subs[sub_index], subs[j] = subs[j], subs[sub_index]
            self.expanded_key = (parent_index, branch, j)
            self.changed.emit()

    def dup_sub(self, parent_index, sub_index, branch="else"):
        subs = self.steps[parent_index].get(branch) or []
        if 0 <= sub_index < len(subs):
            subs.insert(sub_index + 1, copy.deepcopy(subs[sub_index]))
            self.expanded_key = (parent_index, branch, sub_index + 1)
            self.changed.emit()

    def del_sub(self, parent_index, sub_index, branch="else"):
        subs = self.steps[parent_index].get(branch) or []
        if 0 <= sub_index < len(subs):
            subs.pop(sub_index)
            self.expanded_key = ((parent_index,) if subs else None)
            self.changed.emit()


class _StepsEditor(QWidget):
    """卡片式步骤编辑(前置条件用): 「＋添加步骤」分类菜单 + 步骤卡片(可展开改参数)。

    与用例编辑区共用 StepCard / make_action_menu / _make_field_widget,
    因此动作、参数、else 子步骤等行为完全一致。
    """

    def __init__(self, steps=None, parent=None, app_group=""):
        super().__init__(parent)
        from core import vision
        self.host = _StepsHost(steps)
        self.host.changed.connect(self.render_cards)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)

        # ★ APP 组选择: 模板/基准图下拉按所选组取模板(用户要求)
        grow = QHBoxLayout()
        grow.addWidget(QLabel("APP 组"))
        self.group_combo = QComboBox()
        self.group_combo.addItems(self._list_groups())
        # 优先用传入的组, 其次当前上下文, 最后**默认第一个组** ——
        # 否则首次打开时组为空 → 模板上下文为空 → 模板下拉读不到模板,
        # 要手动切一次组才有(用户实测)
        want = app_group or vision.current_app_group()
        k = self.group_combo.findText(want) if want else -1
        if k >= 0:
            self.group_combo.setCurrentIndex(k)
        elif self.group_combo.count() > 0:
            self.group_combo.setCurrentIndex(0)
        self.group_combo.setToolTip("选择 APP 组后, 模板下拉会列出该组的模板")
        self.group_combo.currentTextChanged.connect(self._on_group_changed)
        # ★ setCurrentIndex 不触发信号 → 这里显式同步一次模板上下文,
        #   否则首次打开时模板下拉仍为空(要手动切组才有)
        vision.set_template_app_group(self.group_combo.currentText() or "")
        grow.addWidget(self.group_combo)
        grow.addStretch(1)
        v.addLayout(grow)

        bar = QHBoxLayout()
        add_btn = QPushButton("＋ 添加步骤")
        add_btn.setObjectName("chipBtn")
        add_btn.setMenu(make_action_menu(add_btn, self.add_step))
        bar.addWidget(add_btn)
        bar.addStretch(1)
        self.count_label = QLabel("")
        self.count_label.setStyleSheet("color:#94a3b8;")
        bar.addWidget(self.count_label)
        v.addLayout(bar)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        self.cards_lay = QVBoxLayout(inner)
        self.cards_lay.setContentsMargins(2, 2, 2, 2)
        self.cards_lay.setSpacing(4)
        self.cards_lay.addStretch(1)
        scroll.setWidget(inner)
        v.addWidget(scroll, 1)
        self.render_cards()

    @staticmethod
    def _list_groups():
        """Test_cases/ 下的 APP 组目录列表"""
        try:
            root = os.path.join(BASE_DIR, "Test_cases")
            return sorted(d for d in os.listdir(root)
                          if os.path.isdir(os.path.join(root, d))
                          and not d.startswith((".", "_")))
        except Exception:
            return []

    def _on_group_changed(self, group):
        """切换 APP 组 → 重设模板上下文并重建卡片(模板下拉随之刷新)"""
        from core import vision
        vision.set_template_app_group(group or "")
        self.render_cards()

    def add_step(self, key):
        self.host.steps.append(schema.new_step(key))
        self.host.expanded_key = (len(self.host.steps) - 1,)
        self.render_cards()

    def steps(self):
        return self.host.steps

    def render_cards(self):
        """与 MainWindow.render_cards 同逻辑(含 else 子步骤缩进卡片)"""
        while self.cards_lay.count() > 1:      # 末尾 stretch 保留
            item = self.cards_lay.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        for i in range(len(self.host.steps)):
            self.cards_lay.insertWidget(self.cards_lay.count() - 1,
                                        StepCard(self.host, i))
            ek = self.host.expanded_key
            step = self.host.steps[i]
            if ek and ek[0] == i:
                for branch in ("then", "else"):        # 两个分支的子卡片都要渲染
                    subs = step.get(branch)
                    if not isinstance(subs, list):
                        continue
                    for j in range(len(subs)):
                        self.cards_lay.insertWidget(
                            self.cards_lay.count() - 1,
                            StepCard(self.host, i, parent_index=i, sub_index=j,
                                     branch=branch))
        self.count_label.setText(f"共 {len(self.host.steps)} 步")
