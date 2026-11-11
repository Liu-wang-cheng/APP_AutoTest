# -*- coding: utf-8 -*-
"""实时曲线（pyqtgraph）：曲线页 → 分组 → 一张图多条曲线，支持缩放/平移/跟随。

数据由 DevicePage 的 QTimer 拉 `ParamService.latest()` 后调用 `update_snapshot`。
同一分组(如边刷转速的左/右、碰撞的左/右/前左)合并到一张图里，靠图例区分。
"""
import bisect
import copy
import html
import json
import math
from collections import deque

import pyqtgraph as pg
from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QFrame, QGridLayout, QHBoxLayout,
                                QLabel, QListWidget, QListWidgetItem, QMenu,
                                QPushButton, QScrollArea, QSizePolicy,
                                QVBoxLayout, QWidget)

from core.apos import catalog
from gui.widgets import FlowLayout

# ★ 关闭抗锯齿：实测 3600 点 × 多曲线的图，抗锯齿开启时一帧 ~76ms，
#   关闭后 ~5ms，切页/拖动才不会卡（曲线略粗糙但可接受）。
pg.setConfigOptions(antialias=False)

#: 多曲线配色（高对比、固定顺序，不随数据变化）
SERIES_COLORS = ("#e11d48", "#2563eb", "#16a34a", "#f59e0b",
                 "#7c3aed", "#0891b2", "#db2777", "#65a30d", "#0f172a")

#: 每条曲线的历史点数上限（5Hz × 12 分钟；窗口默认 10 分钟，足够）
HISTORY_MAX = 3600

#: 手动缩放/拖动后，闲置这么久自动恢复「自适应 + 跟随实时」(用户 2026-11-11)
AUTO_FIT_IDLE_MS = 30_000


def _series_pen(i):
    """统一实线，只靠颜色区分（用户 2026-11-09）。"""
    return pg.mkPen(SERIES_COLORS[i % len(SERIES_COLORS)], width=2.2)


def _series_key(curve):
    """一条曲线 = (属性路径, 字典字段[, 同路径拆线编号])。"""
    sid = curve.get("series_id")
    if sid is None:
        return (curve["path"], curve.get("key"))
    return (curve["path"], curve.get("key"), sid)


def _fmt(value, curve):
    unit = curve.get("unit", "")
    try:
        v = float(value)
        # 里程计/编码器这类大整数不要用 %g（会变成 1.14715e+06）
        if v.is_integer():
            text = str(int(v))
        else:
            text = f"{v:.6f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        text = str(value)
    return text + (" " + unit if unit else "")


class CurveCard(QFrame):
    """一个分组对应一张图：分组里的每条曲线画一根线。"""

    def __init__(self, group, data_map=None, parent=None):
        super().__init__(parent)
        self.group = group
        self.series = list(group.get("curves") or [])
        data_map = data_map if data_map is not None else {}
        self.series_data = {}
        self._lines = {}
        self._xs = {}
        self._ys = {}
        self.last_t = {}
        self.value_label = None
        self._value_labels = {}      # 多曲线: 图上方彩色标签, key -> QLabel
        self._hover = None
        self._vline = None

        # 高度按内容(绘图区封顶 240), 不跟着滚动区被拉长(用户 2026-11-11)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 7, 8, 5)
        v.setSpacing(2)
        head = QHBoxLayout()
        title = QLabel(group["name"])
        self.title_label = title
        title.setStyleSheet("font-size:11px;font-weight:600;color:#475569;")
        head.addWidget(title)
        head.addStretch(1)
        if len(self.series) == 1:
            self.value_label = QLabel("--")
            self.value_label.setStyleSheet(
                "font-size:11px;font-weight:700;color:#1e293b;")
            head.addWidget(self.value_label)
        else:
            cnt = QLabel(f"{len(self.series)} 条曲线")
            cnt.setStyleSheet("color:#b0b9c6;font-size:10px;")
            head.addWidget(cnt)
        v.addLayout(head)

        # ★ 多条曲线的实时值放在图**上方**的彩色标签里(用户 2026-11-11):
        #   以前写在图内图例里, 图例透明, 曲线会从文字上穿过导致数值看不清。
        #   标签按颜色与曲线一一对应, 自动换行; 单条曲线仍在右上角显示数值。
        if len(self.series) > 1:
            row = QWidget()
            self.values_flow = FlowLayout(row, margin=0, spacing=10)
            for i, curve in enumerate(self.series):
                name = curve.get("title") or curve["path"]
                lab = QLabel()
                lab.setTextFormat(Qt.RichText)
                lab.setStyleSheet("font-size:11px;color:#1e293b;")
                lab.setText(self._value_html(i, name, "--"))
                self.values_flow.addWidget(lab)
                self._value_labels[_series_key(curve)] = lab
            v.addWidget(row)

        if not self.series:
            empty = QLabel("曲线已隐藏（右键可恢复）")
            empty.setStyleSheet("color:#b0b9c6;font-size:11px;padding:14px 4px;")
            v.addWidget(empty, 1)
            self.plot = None
            self._legend = None
            return

        self.plot = pg.PlotWidget(background="w")
        # ★ 单行卡片时不要被拉满整屏: 绘图区固定 220px, Y 轴不再过长(用户 2026-11-11)
        self.plot.setFixedHeight(220)
        self.plot.showGrid(x=False, y=True, alpha=0.12)
        self.plot.setMouseEnabled(x=True, y=True)
        self.plot.setLabel("bottom", "时间(分钟)")
        units = {c.get("unit", "") for c in self.series}
        if len(units) == 1:
            unit = next(iter(units))
            if unit:
                self.plot.setLabel("left", unit)
        self.plot.getAxis("bottom").setPen(pg.mkPen("#cbd5e1"))
        self.plot.getAxis("left").setPen(pg.mkPen("#cbd5e1"))
        self.plot.getAxis("bottom").setTextPen(pg.mkPen("#94a3b8"))
        self.plot.getAxis("left").setTextPen(pg.mkPen("#94a3b8"))
        # 关掉 pyqtgraph 自带的英文右键菜单，改用下面自己的中文菜单
        self.plot.setMenuEnabled(False)
        self.plot.getViewBox().setMenuEnabled(False)
        self._legend = None      # 多曲线用上方标签显示数值, 图内不再放图例
        v.addWidget(self.plot, 1)

        for i, curve in enumerate(self.series):
            key = _series_key(curve)
            data = data_map.setdefault(key, deque(maxlen=HISTORY_MAX))
            self.series_data[key] = data
            self.last_t[key] = data[-1][0] if data else None
            name = curve.get("title") or curve["path"]
            line = self.plot.plot([], [], pen=_series_pen(i), name=name)
            # 历史点多了以后只画视口内的点并自动降采样，拖动/缩放才不卡
            line.setClipToView(True)
            line.setDownsampling(auto=True, method="peak")
            self._lines[key] = line
            if data:
                self.refresh(key)
        # ★ Y 轴由当前可见数据手动设范围: 顶部刻度就是数据最大值(用户 2026-11-11)。
        #   关掉自动 Y 范围, 避免它和"顶部=数据最大值"的刻度打架。
        self.plot.getViewBox().disableAutoRange(axis=pg.ViewBox.YAxis)

        # ── 鼠标悬停：竖线 + 参数名/数值 ──
        self._vline = pg.InfiniteLine(
            angle=90, movable=False, pen=pg.mkPen("#94a3b8", width=1))
        self._vline.setVisible(False)
        self.plot.addItem(self._vline, ignoreBounds=True)
        self._hover = QLabel(self.plot)
        self._hover.setStyleSheet(
            "background:rgba(255,255,255,238);border:1px solid #cbd5e1;"
            "border-radius:4px;padding:4px 6px;font-size:11px;color:#1e293b;")
        # 不拦截鼠标，否则提示框挡住光标后 move 事件会断
        self._hover.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._hover.hide()
        self.plot.scene().sigMouseMoved.connect(self._on_mouse_move)
        self.plot.viewport().installEventFilter(self)

    @staticmethod
    def _value_html(index, name, value):
        """彩色圆点 + 曲线名 + 当前值(名称/数值用正文色, 只有圆点用曲线色)。"""
        color = SERIES_COLORS[index % len(SERIES_COLORS)]
        return (f'<span style="color:{color};font-weight:700;">●</span> '
                f'{html.escape(str(name))}: '
                f'<b>{html.escape(str(value))}</b>')

    def refresh(self, key=None):
        """按历史数据重画（key=None 时重画全部）。"""
        for k, line in self._lines.items():
            if key is not None and k != key:
                continue
            data = self.series_data[k]
            if not data:
                continue
            xs = [p[0] for p in data]
            ys = [p[1] for p in data]
            self._xs[k] = xs
            self._ys[k] = ys
            line.setData(xs, ys)
            curve = next(c for c in self.series if _series_key(c) == k)
            if self.value_label is not None:
                self.value_label.setText(_fmt(ys[-1], curve))
            elif k in self._value_labels:
                idx = next(i for i, c in enumerate(self.series)
                           if _series_key(c) == k)
                self._value_labels[k].setText(self._value_html(
                    idx, curve.get("title") or curve["path"],
                    _fmt(ys[-1], curve)))

    def eventFilter(self, obj, ev):
        # 鼠标移出图区时收起提示（否则标签会一直停在原处）
        if ev.type() == QEvent.Leave and self.plot is not None \
                and obj is self.plot.viewport():
            self._hide_hover()
        return super().eventFilter(obj, ev)

    def _hide_hover(self):
        if self._hover is not None:
            self._hover.hide()
        if self._vline is not None:
            self._vline.setVisible(False)

    def _on_mouse_move(self, pos):
        if self._hover is None or not self._lines:
            return
        try:
            if not self.plot.sceneBoundingRect().contains(pos):
                self._hide_hover()
                return
            pt = self.plot.getViewBox().mapSceneToView(pos)
            x = float(pt.x())
            rows, snaps = [], []
            for key, _line in self._lines.items():
                xs = self._xs.get(key)
                ys = self._ys.get(key)
                if not xs:
                    continue
                i = bisect.bisect_left(xs, x)
                if i <= 0:
                    j = 0
                elif i >= len(xs):
                    j = len(xs) - 1
                else:
                    j = i if abs(xs[i] - x) < abs(x - xs[i - 1]) else i - 1
                curve = next(c for c in self.series
                             if _series_key(c) == key)
                name = curve.get("title") or curve["path"]
                rows.append(f"{name}: {_fmt(ys[j], curve)}")
                snaps.append(xs[j])
            if not rows:
                self._hide_hover()
                return
            self._vline.setPos(snaps[0])
            self._vline.setVisible(True)
            self._hover.setText(f"时间 {snaps[0]:.1f} 分钟\n" + "\n".join(rows))
            self._hover.adjustSize()
            p = self.plot.mapFromScene(pos)
            w = self._hover.width()
            h = self._hover.height()
            self._hover.move(max(4, min(p.x() + 14, self.plot.width() - w - 4)),
                             max(4, p.y() - h - 10))
            self._hover.show()
        except Exception:
            self._hide_hover()


class CurveView(QWidget):
    pathsChanged = Signal(list)

    def __init__(self, window_minutes=10, parent=None, settings=None):
        super().__init__(parent)
        self.window_minutes = float(window_minutes or 10)
        #: 当前曲线页(组)的跟随状态；每页各自记住自己的状态(用户 2026-11-11)
        self.follow = True
        self._page_follow = {}       # 页 key -> 是否跟随
        self._paused_page = None     # 触发 30 秒计时的那一页
        self.settings = settings
        self._t0 = None
        self._cards = []
        self._page_index = 0
        #: 用户从「实时参数」里添加的自定义曲线（每条=一张图）
        self._custom = self._load_custom()
        #: 追加到内置曲线图里的额外曲线
        self._extras = self._load_extras()
        #: 用户右键删掉的内置曲线（可恢复）
        self._hidden = self._load_hidden()
        # 曲线历史与当前可见页解耦：连接后所有曲线路径的数据都先存这里，
        # 切换曲线页时直接用历史回填，不会从切换那一刻才开始。
        # 键 = (属性路径, 字典字段)，同一路径可对应多条曲线（地检探头）。
        self._history = {}
        self._curve_defs = self._defs()
        #: 手动操作后 30 秒无再次操作 → 自动恢复当前这一组曲线图的自适应
        self._idle_timer = QTimer(self)
        self._idle_timer.setSingleShot(True)
        self._idle_timer.setInterval(AUTO_FIT_IDLE_MS)
        self._idle_timer.timeout.connect(self._auto_fit_after_idle)
        self._build()

    #: 随曲线一起保存的「值的解释方式」—— 少任何一个都会把曲线画错或画空:
    #: scale(里程计 mm→m)、bit(地检触发按位取)、decode(沿墙距离十六进制数组)、
    #: key(字典属性取哪个字段)、value_map(状态文本→数字)。
    VALUE_KEYS = ("key", "bit", "scale", "decode", "value_map", "default")

    # ── 自定义曲线（右键「添加到曲线」） ──
    @classmethod
    def _value_meta(cls, src):
        """从曲线定义里挑出解释值的字段(空值不存，保持旧格式兼容)。"""
        return {k: src[k] for k in cls.VALUE_KEYS
                if src.get(k) not in (None, "")}

    def _defs(self):
        defs = [
            (c["path"], c.get("key"), c)
            for page in catalog.CURVE_PAGES
            for group in page["groups"]
            for c in group["curves"]
        ]
        for chart in self._custom:
            for c in chart["curves"]:
                defs.append((c["path"], c.get("key"),
                             {"path": c["path"],
                              "title": c.get("series") or c["path"],
                              "unit": c.get("unit", ""),
                              **self._value_meta(c)}))
        for e in self._extras:
            defs.append((e["path"], e.get("key"),
                         {"path": e["path"], "title": e["series"],
                          "unit": e.get("unit", ""),
                          **self._value_meta(e)}))
        return defs

    @classmethod
    def _clean_curve(cls, c, meta=None):
        path = str(c.get("path") or "").strip()
        if not path:
            return None
        src = dict(meta or {})
        src.update(c)
        out = {"path": path,
               "series": str(c.get("series") or c.get("title") or path),
               "unit": str(c.get("unit") or "")}
        out.update(cls._value_meta(src))
        return out

    def _load_custom(self):
        """每张图 = {"title": 曲线图名称, "curves": [参数序列...]}。

        兼容旧版扁平格式（一条曲线一个 dict），加载时自动升成一张图。
        """
        raw = ""
        if self.settings is not None:
            raw = str(self.settings.value("apos/custom_curves", "") or "")
        try:
            data = json.loads(raw) if raw else []
        except Exception:
            data = []
        out = []
        for c in data if isinstance(data, list) else []:
            if not isinstance(c, dict):
                continue
            if isinstance(c.get("curves"), list):
                curves = [x for x in (self._clean_curve(v)
                                      for v in c["curves"]) if x]
                if curves:
                    out.append({"title": str(c.get("title") or curves[0]["path"]),
                                "curves": curves})
                continue
            one = self._clean_curve(c)
            if one is not None:
                out.append({"title": str(c.get("title") or one["path"]),
                            "curves": [one]})
        return out

    def _save_custom(self):
        self._curve_defs = self._defs()
        if self.settings is not None:
            self.settings.setValue("apos/custom_curves",
                                   json.dumps(self._custom, ensure_ascii=False))

    def custom_curves(self):
        """扁平视图（一条曲线一项），便于测试/外部读取。"""
        return [{"path": c["path"], "title": chart["title"],
                 "series": c["series"], "unit": c["unit"]}
                for chart in self._custom for c in chart["curves"]]

    def custom_charts(self):
        return copy.deepcopy(self._custom)

    # ── 内置曲线图的额外曲线 ──
    def _load_extras(self):
        raw = ""
        if self.settings is not None:
            raw = str(self.settings.value("apos/custom_series", "") or "")
        try:
            data = json.loads(raw) if raw else []
        except Exception:
            data = []
        out = []
        for e in data if isinstance(data, list) else []:
            if not isinstance(e, dict) or not e.get("path"):
                continue
            if not e.get("page") or not e.get("group"):
                continue
            out.append({"page": str(e["page"]), "group": str(e["group"]),
                        "path": str(e["path"]),
                        "series": str(e.get("series") or e["path"]),
                        "unit": str(e.get("unit") or "")})
        return out

    def _save_extras(self):
        self._curve_defs = self._defs()
        if self.settings is not None:
            self.settings.setValue("apos/custom_series",
                                   json.dumps(self._extras, ensure_ascii=False))

    def extra_curves(self):
        return copy.deepcopy(self._extras)

    # ── 右键删除的内置曲线（可恢复） ──
    def _load_hidden(self):
        raw = ""
        if self.settings is not None:
            raw = str(self.settings.value("apos/hidden_curves", "") or "")
        try:
            data = json.loads(raw) if raw else []
        except Exception:
            data = []
        out = []
        for h in data if isinstance(data, list) else []:
            if isinstance(h, dict) and h.get("path"):
                out.append({"page": str(h.get("page") or ""),
                            "group": str(h.get("group") or ""),
                            "path": str(h["path"]),
                            "title": str(h.get("title") or ""),
                            "key": h.get("key"),
                            "series_id": h.get("series_id")})
        return out

    def _save_hidden(self):
        if self.settings is not None:
            self.settings.setValue("apos/hidden_curves",
                                   json.dumps(self._hidden, ensure_ascii=False))

    @staticmethod
    def _curve_ident(curve):
        return (curve.get("path"), curve.get("key"), curve.get("series_id"),
                curve.get("title"))

    def _is_hidden(self, page, group, curve):
        ident = self._curve_ident(curve)
        return any(h["page"] == page and h["group"] == group
                   and self._curve_ident(h) == ident for h in self._hidden)

    def _hide_curve(self, page, group, curve):
        if self._is_hidden(page, group, curve):
            return
        self._hidden.append({"page": page, "group": group,
                             "path": curve.get("path"),
                             "title": curve.get("title"),
                             "key": curve.get("key"),
                             "series_id": curve.get("series_id")})
        self._save_hidden()

    def _show_curve(self, hidden):
        self._hidden = [h for h in self._hidden
                        if not (h["page"] == hidden["page"]
                                and h["group"] == hidden["group"]
                                and self._curve_ident(h)
                                == self._curve_ident(hidden))]
        self._save_hidden()

    def curve_targets(self):
        """「添加到曲线」可选的已有图：自定义图 + 全部内置图。"""
        out = []
        for ci, chart in enumerate(self._custom):
            out.append({"kind": "custom", "index": ci,
                        "label": f'自定义「{chart["title"]}」'})
        for page in catalog.CURVE_PAGES:
            for group in page["groups"]:
                out.append({"kind": "builtin",
                            "page": page["name"], "group": group["name"],
                            "label": f'{page["name"]} · {group["name"]}'})
        return out

    def _merged_groups(self, page):
        """内置页的分组 + 用户追加的曲线。"""
        groups = copy.deepcopy(page.get("groups", []))
        for e in self._extras:
            if e["page"] != page["name"]:
                continue
            for g in groups:
                if g["name"] == e["group"]:
                    g["curves"].append({"title": e["series"], "path": e["path"],
                                        "unit": e.get("unit", ""),
                                        **self._value_meta(e)})
                    break
        # 右键删掉的曲线不再画（可右键恢复）
        for g in groups:
            g["curves"] = [c for c in g["curves"]
                           if not self._is_hidden(page["name"], g["name"], c)]
        return groups

    def add_series_to_group(self, page, group, path, series="", unit="",
                            meta=None):
        """把参数追加到某个内置曲线图里。返回 False=路径为空或已在图中。"""
        one = self._clean_curve({"path": path, "series": series,
                                 "unit": unit}, meta)
        if one is None:
            return False
        # 内置图里已经有这条路径就不再重复加（两条同 key 的线会重叠）
        page_def = next((p for p in catalog.CURVE_PAGES
                         if p["name"] == page), None)
        if page_def:
            for g in page_def["groups"]:
                if g["name"] == group and any(
                        c["path"] == one["path"] for c in g["curves"]):
                    return False
        one["series"] = (str(series or "").strip()
                         or one["path"].rsplit("/", 1)[-1])
        for e in self._extras:
            if (e["page"], e["group"], e["path"]) == (page, group, one["path"]):
                e["series"] = one["series"]
                e["unit"] = one["unit"] or e["unit"]
                e.update(self._value_meta(one))
                break
        else:
            entry = {"page": page, "group": group,
                     "path": one["path"], "series": one["series"],
                     "unit": one["unit"]}
            entry.update(self._value_meta(one))
            self._extras.append(entry)
        self._save_extras()
        if self._is_builtin_page(page):
            self.set_page(self._page_index)
        return True

    def remove_extra(self, page, group, path):
        before = len(self._extras)
        self._extras = [e for e in self._extras
                        if (e["page"], e["group"], e["path"])
                        != (page, group, path)]
        if len(self._extras) == before:
            return
        self._save_extras()
        if self._is_builtin_page(page):
            self.set_page(self._page_index)

    def _is_builtin_page(self, page_name):
        return any(p["name"] == page_name
                   for p in catalog.CURVE_PAGES) and \
            self._page_index < len(catalog.CURVE_PAGES) and \
            catalog.CURVE_PAGES[self._page_index]["name"] == page_name

    def add_custom_curve(self, path, title="", series="", unit="", meta=None):
        """新建一张自定义曲线图。返回 False 表示路径为空。"""
        one = self._clean_curve({"path": path, "series": series,
                                 "unit": unit}, meta)
        if one is None:
            return False
        name = str(title or "").strip() or one["series"]
        one["series"] = str(series or "").strip() or name
        self._custom.append({"title": name, "curves": [one]})
        self._save_custom()
        # 每张自定义图独占一个页, 页名就是用户设的「曲线图名称」
        self._rebuild_page_list(select=len(catalog.CURVE_PAGES) + len(self._custom) - 1)
        return True

    def add_series_to_chart(self, chart_index, path, series="", unit="",
                            meta=None):
        """把参数追加到已有曲线图里（同一张图多条曲线）。"""
        if not (0 <= chart_index < len(self._custom)):
            return False
        one = self._clean_curve({"path": path, "series": series,
                                 "unit": unit}, meta)
        if one is None:
            return False
        chart = self._custom[chart_index]
        one["series"] = str(series or "").strip() or one["path"].rsplit("/", 1)[-1]
        for c in chart["curves"]:
            if c["path"] == one["path"]:
                c["series"] = one["series"]
                c["unit"] = one["unit"] or c["unit"]
                c.update(self._value_meta(one))
                break
        else:
            chart["curves"].append(one)
        self._save_custom()
        self._rebuild_page_list(select=self._page_index)
        return True

    def remove_custom_curve(self, index):
        if not (0 <= index < len(self._custom)):
            return
        cur = self._page_index
        self._custom.pop(index)
        self._save_custom()
        if cur < len(catalog.CURVE_PAGES):
            sel = cur
        elif cur == len(catalog.CURVE_PAGES) + index:
            # 删除的正是当前页: 选中相邻的一张, 没有自定义图就回到内置页
            sel = (len(catalog.CURVE_PAGES)
                   + max(0, min(index, len(self._custom) - 1))
                   if self._custom else max(0, len(catalog.CURVE_PAGES) - 1))
        else:
            sel = min(cur, len(catalog.CURVE_PAGES) + len(self._custom) - 1)
        self._rebuild_page_list(select=sel)

    def _rebuild_page_list(self, select=None):
        """重建左侧页列表: 内置页 + 每个自定义图一页(页名=曲线图名称)。"""
        row = self.page_list.currentRow() if select is None else select
        self.page_list.blockSignals(True)
        self.page_list.clear()
        self._add_page_items()
        row = max(0, min(row, self.page_list.count() - 1))
        self.page_list.setCurrentRow(row)
        self.page_list.blockSignals(False)
        self.set_page(row)

    def _add_page_items(self):
        for page in catalog.CURVE_PAGES:
            n = sum(len(g["curves"]) for g in page["groups"])
            QListWidgetItem(f'{page["name"]}    {n} 条', self.page_list)
        for c in self._custom:
            QListWidgetItem(c["title"], self.page_list)

    def _build(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)
        self.page_list = QListWidget()
        self.page_list.setObjectName("caseList")
        self.page_list.setFixedWidth(172)
        # 内置页 + 自定义图页（实时参数右键「添加到曲线」, 页名=曲线图名称）
        self._add_page_items()
        root.addWidget(self.page_list)

        right = QVBoxLayout()
        right.setSpacing(6)
        bar = QHBoxLayout()
        self.title = QLabel()
        self.title.setStyleSheet("font-size:14px;font-weight:700;color:#1e293b;")
        bar.addWidget(self.title)
        bar.addSpacing(8)
        self.meta = QLabel()
        self.meta.setStyleSheet("color:#94a3b8;font-size:11px;")
        bar.addWidget(self.meta)
        bar.addStretch(1)
        # ★ 跟随/自适应按"曲线页(组)"独立: 风机、电池各自记住状态(用户 2026-11-11)
        self.follow_box = QCheckBox("跟随实时")
        self.follow_box.setToolTip("只影响当前这一组曲线图; 各页状态互不影响")
        self.follow_box.setChecked(True)
        self.follow_box.toggled.connect(self.set_follow)
        bar.addWidget(self.follow_box)
        self.follow_hint = QLabel("已暂停跟随 · 30 秒无操作自动自适应")
        self.follow_hint.setStyleSheet("color:#b45309;font-size:11px;")
        self.follow_hint.hide()
        bar.addWidget(self.follow_hint)
        self.reset_btn = QPushButton("自适应")
        self.reset_btn.setObjectName("chipBtn")
        self.reset_btn.setToolTip("恢复当前这一组的所有曲线图（不影响其它组）")
        self.reset_btn.clicked.connect(self._reset_view)
        bar.addWidget(self.reset_btn)
        right.addLayout(bar)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.holder = QWidget()
        self.grid = QGridLayout(self.holder)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(10)
        # 卡片按自身高度排在顶部, 不被拉伸去填满整个滚动区
        self.grid.setAlignment(Qt.AlignTop)
        self.scroll.setWidget(self.holder)
        right.addWidget(self.scroll, 1)
        root.addLayout(right, 1)

        self.page_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.page_list.customContextMenuRequested.connect(self._page_menu)
        self.page_list.currentRowChanged.connect(self.set_page)
        self.page_list.setCurrentRow(0)

    def _clear(self):
        while self.grid.count():
            item = self.grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()              # 立即从界面消失(deleteLater 是异步的, 会残留旧图)
                w.setParent(None)
                w.deleteLater()
        self._cards = []

    def _page_key(self, index):
        """曲线页的身份: 内置页用页名, 自定义图用 custom:标题。"""
        if index >= len(catalog.CURVE_PAGES):
            ci = index - len(catalog.CURVE_PAGES)
            if 0 <= ci < len(self._custom):
                return f'custom:{self._custom[ci]["title"]}'
            return f"custom-index:{index}"
        return catalog.CURVE_PAGES[index]["name"]

    def set_page(self, index):
        if index < 0 or index >= len(catalog.CURVE_PAGES) + len(self._custom):
            return
        self._page_index = index
        # ★ 每组曲线图单独记住跟随状态(用户 2026-11-11): 切换到哪组就恢复哪组
        self._page_key_current = self._page_key(index)
        self.follow = bool(self._page_follow.get(self._page_key_current, True))
        self.follow_box.blockSignals(True)
        self.follow_box.setChecked(self.follow)
        self.follow_box.blockSignals(False)
        self.follow_hint.setVisible(not self.follow)
        custom = index >= len(catalog.CURVE_PAGES)
        custom_index = index - len(catalog.CURVE_PAGES)
        if custom:
            chart = self._custom[custom_index]
            self.title.setText(chart["title"])
            groups = [{"name": chart["title"],
                       "curves": [{"title": c["series"], "path": c["path"],
                                   "unit": c.get("unit", ""),
                                   **self._value_meta(c)}
                                  for c in chart["curves"]]}]
            self.meta.setText(f"1 张图 · {len(chart['curves'])} 条曲线 · "
                              f"来自实时参数右键添加")
        else:
            page = catalog.CURVE_PAGES[index]
            self.title.setText(page["name"])
            groups = self._merged_groups(page)
            self.meta.setText(f"{len(groups)} 张图 · "
                              f"{sum(len(g['curves']) for g in groups)} 条曲线")
        self._clear()
        row = 0
        col = 0
        for gi, group in enumerate(groups):
            card = CurveCard(group, self._history)
            if card.plot is not None:
                # 组内任意一张图被缩放/拖动 → 这一组暂停跟随(其它组不受影响)
                card.plot.getViewBox().sigRangeChangedManually.connect(
                    self._manual_range)
            targets = [card]
            if card.plot is not None:
                targets.append(card.plot)
            if getattr(card, "title_label", None) is not None:
                targets.append(card.title_label)
            for target in targets:
                target.setContextMenuPolicy(Qt.CustomContextMenu)
                if custom:
                    target.customContextMenuRequested.connect(
                        lambda pos, w=target, c=card, i=custom_index:
                        self._custom_menu(w, pos, c, i))
                else:
                    target.customContextMenuRequested.connect(
                        lambda pos, w=target, c=card, p=page["name"],
                        g=group["name"]:
                        self._builtin_menu(w, pos, c, p, g))
            self.grid.addWidget(card, row, col)
            self._cards.append(card)
            col += 1
            if col >= 2:
                col = 0
                row += 1
        self._apply_follow()
        self._emit_paths()

    def clear_history(self):
        """换设备/重新连接时清空所有曲线历史与时间基准。"""
        self._history.clear()
        self._t0 = None
        if self._cards:
            self.set_page(self._page_index)

    def current_paths(self):
        """当前页需要采集的属性路径。"""
        if self._page_index >= len(catalog.CURVE_PAGES):
            ci = self._page_index - len(catalog.CURVE_PAGES)
            if 0 <= ci < len(self._custom):
                paths = []
                for c in self._custom[ci]["curves"]:
                    if c["path"] not in paths:
                        paths.append(c["path"])
                return paths
            return []
        page = catalog.CURVE_PAGES[self._page_index]
        paths = catalog.page_curve_paths(page)
        for e in self._extras:
            if e["page"] == page["name"] and e["path"] not in paths:
                paths.append(e["path"])
        return paths

    def _emit_paths(self):
        self.pathsChanged.emit(self.current_paths())

    def remove_series(self, chart_index, series_index):
        """从自定义曲线图里删掉一条曲线；图空了就整张删掉。"""
        if not (0 <= chart_index < len(self._custom)):
            return
        chart = self._custom[chart_index]
        if not (0 <= series_index < len(chart["curves"])):
            return
        chart["curves"].pop(series_index)
        if not chart["curves"]:
            self.remove_custom_curve(chart_index)
            return
        self._save_custom()
        self._rebuild_page_list(select=self._page_index)

    def _adaptive(self, card):
        if card.plot is not None:
            card.plot.enableAutoRange()
            card.plot.getViewBox().autoRange()

    def _custom_menu(self, widget, pos, card, index):
        menu = QMenu(self)
        fit = menu.addAction("自适应显示")
        menu.addSeparator()
        acts = []
        if 0 <= index < len(self._custom):
            for si, c in enumerate(self._custom[index]["curves"]):
                acts.append((menu.addAction(f"删除曲线：{c['series']}"), si))
        menu.addSeparator()
        remove_all = menu.addAction("删除整张曲线图")
        chosen = menu.exec(widget.mapToGlobal(pos))
        if chosen is fit:
            self._adaptive(card)
            return
        for act, si in acts:
            if chosen is act:
                self.remove_series(index, si)
                return
        if chosen is remove_all:
            self.remove_custom_curve(index)

    def _delete_visible_items(self, page, group, items):
        """右键删除：追加的曲线真正移除，内置曲线隐藏（可恢复）。

        一次性改完再重建页面，避免逐条触发 set_page。
        """
        for item in items:
            path = item.get("path")
            idx = next((i for i, e in enumerate(self._extras)
                        if e["page"] == page and e["group"] == group
                        and e["path"] == path
                        and e["series"] == item.get("title")), None)
            if idx is not None:
                self._extras.pop(idx)
            elif not self._is_hidden(page, group, item):
                self._hidden.append({
                    "page": page, "group": group, "path": path,
                    "title": item.get("title"), "key": item.get("key"),
                    "series_id": item.get("series_id")})
        self._save_extras()
        self._save_hidden()
        self.set_page(self._page_index)

    def _builtin_menu(self, widget, pos, card, page, group):
        """内置图右键：中文菜单，可删除/恢复每条曲线。"""
        menu = QMenu(self)
        fit = menu.addAction("自适应显示")
        acts = []
        visible = (card.group or {}).get("curves", [])
        del_all = None
        if visible:
            menu.addSeparator()
            for c in visible:
                acts.append((menu.addAction(
                    f"删除曲线：{c.get('title') or c.get('path')}"), "hide", c))
            menu.addSeparator()
            del_all = menu.addAction("删除整张曲线图")
        hidden = [h for h in self._hidden
                  if h["page"] == page and h["group"] == group]
        restore_all = None
        if hidden:
            menu.addSeparator()
            for h in hidden:
                acts.append((menu.addAction(f"恢复曲线：{h['title']}"),
                             "show", h))
            restore_all = menu.addAction("恢复整张曲线图")
        chosen = menu.exec(widget.mapToGlobal(pos))
        if chosen is fit:
            self._adaptive(card)
            return
        if chosen is del_all:
            self._delete_visible_items(page, group, visible)
            return
        if chosen is restore_all:
            for h in hidden:
                self._show_curve(h)
            self.set_page(self._page_index)
            return
        for act, kind, item in acts:
            if chosen is act:
                if kind == "hide":
                    self._delete_visible_items(page, group, [item])
                else:
                    self._show_curve(item)
                    self.set_page(self._page_index)
                return

    def _page_menu(self, pos):
        item = self.page_list.itemAt(pos)
        if item is None:
            return
        row = self.page_list.row(item)
        if row < len(catalog.CURVE_PAGES):
            return                            # 内置页不允许删
        ci = row - len(catalog.CURVE_PAGES)
        menu = QMenu(self)
        act = menu.addAction(f'删除曲线图「{self._custom[ci]["title"]}」')
        if menu.exec(self.page_list.viewport().mapToGlobal(pos)) is act:
            self.remove_custom_curve(ci)

    def _manual_range(self, *_):
        """当前这一组曲线图被缩放/拖动: 只暂停本组, 其它组状态不变。"""
        if self.follow:
            self.follow = False
            self._page_follow[self._page_key_current] = False
            self.follow_box.blockSignals(True)
            self.follow_box.setChecked(False)
            self.follow_box.blockSignals(False)
            self.follow_hint.setVisible(True)
            # 手动缩放期间交还刻度自动生成, 否则固定刻度可能覆盖不到新范围
            for c in self._cards:
                if c.plot is not None:
                    c.plot.getAxis("left").setTicks(None)
        # 30 秒内没再操作 → 只恢复这一组
        self._paused_page = self._page_key_current
        self._idle_timer.start()

    def _auto_fit_after_idle(self):
        """30 秒无操作: 恢复被暂停的那一组(用户 2026-11-11)。"""
        self._idle_timer.stop()
        key = self._paused_page
        self._paused_page = None
        if not key:
            return
        self._page_follow[key] = True
        if key == self._page_key_current:
            self._reset_view()

    def set_follow(self, on):
        """顶部开关只作用于当前这一组; 其它组的自适应状态不受影响。"""
        self.follow = bool(on)
        self._page_follow[self._page_key_current] = self.follow
        self.follow_hint.setVisible(not self.follow)
        if on:
            if self._paused_page == self._page_key_current:
                self._idle_timer.stop()
                self._paused_page = None
            for c in self._cards:
                if c.plot is not None:
                    c.plot.enableAutoRange()
            self._apply_follow()
        else:
            self._paused_page = self._page_key_current
            self._idle_timer.start()
            for c in self._cards:
                if c.plot is not None:
                    c.plot.getAxis("left").setTicks(None)

    def _reset_view(self):
        """顶部「自适应」: 恢复当前这一组的所有曲线图(其它组不受影响)。"""
        if self._paused_page == self._page_key_current:
            self._idle_timer.stop()
            self._paused_page = None
        self.follow = True
        self._page_follow[self._page_key_current] = True
        self.follow_box.blockSignals(True)
        self.follow_box.setChecked(True)
        self.follow_box.blockSignals(False)
        self.follow_hint.setVisible(False)
        for c in self._cards:
            if c.plot is not None:
                c.plot.enableAutoRange()
        self._apply_follow()

    def _fit_card_x(self, card):
        """把一张图的 X 轴拉到最近 window_minutes 的窗口(跟随实时用)。"""
        last = None
        for data in card.series_data.values():
            if data:
                last = data[-1][0] if last is None else max(last, data[-1][0])
        if last is None:
            return
        # ★ 刚连上时数据只有几秒, 不要一上来就铺 10 分钟(数据会挤在最左边
        #   看起来像一条竖线)。前 window_minutes 内让 X 轴贴着已有数据铺满
        #   (数据越多窗口越宽), 超过之后再按固定窗口往右滚动。
        if last <= self.window_minutes:
            left, right = 0.0, max(last * 1.05, 0.08)
        else:
            left, right = last - self.window_minutes, last
        card.plot.setXRange(left, right, padding=0.02)
        self._fit_card_y(card, left, right)

    @staticmethod
    def _nice_step(span, target=4):
        """把区间分成 target 段左右时的"整齐"步长(1/2/2.5/5/10 × 10^n)。"""
        if span <= 0:
            return 1.0
        raw = span / max(1, target)
        mag = 10 ** math.floor(math.log10(raw))
        for m in (1, 2, 2.5, 5, 10):
            if raw <= m * mag:
                return m * mag
        return 10 * mag

    @staticmethod
    def _tick_text(value):
        try:
            v = float(value)
        except (TypeError, ValueError):
            return str(value)
        if v.is_integer():
            return str(int(v))
        return f"{v:.6g}"

    def _fit_card_y(self, card, left=None, right=None):
        """Y 范围按当前窗口里的数据: 顶部刻度就是数据最大值(用户 2026-11-11)。"""
        if card.plot is None:
            return
        if left is None or right is None:
            left, right = card.plot.getViewBox().viewRange()[0]
        ymin = ymax = None
        for key, xs in card._xs.items():
            ys = card._ys.get(key)
            if not xs or not ys:
                continue
            i = bisect.bisect_left(xs, left)
            j = bisect.bisect_right(xs, right)
            if j <= i:
                continue
            seg = ys[i:j]
            lo, hi = min(seg), max(seg)
            ymin = lo if ymin is None else min(ymin, lo)
            ymax = hi if ymax is None else max(ymax, hi)
        if ymin is None:
            return
        if ymax == ymin:
            # 一条水平线: 给一点高度, 但顶部仍是数据值
            pad = abs(ymax) * 0.1 or 1.0
            ymin = ymax - pad
        # ★ 上下各留一点余量: 否则"数据最大/最小值"的刻度标签贴着绘图区边缘被裁掉
        #   (用户 2026-11-11: "最大值/最小值不会显示在 Y 轴上")。
        span = ymax - ymin
        headroom = span * 0.06 or abs(ymax) * 0.1 or 1.0
        footroom = span * 0.04 or abs(ymin) * 0.05 or 1.0
        card.plot.setYRange(ymin - footroom, ymax + headroom, padding=0)
        step = self._nice_step(ymax - ymin, target=5)
        ticks = []
        v = math.ceil(ymin / step) * step
        # 浮点误差: 留一点余量, 避免把顶部刻度重复加一次
        while v < ymax - step * 1e-9:
            ticks.append((v, self._tick_text(v)))
            v += step
        # ★ 底部刻度固定为当前数据最小值(与顶部最大值对应, 用户 2026-11-11)
        if not ticks or abs(ticks[0][0] - ymin) > 1e-9:
            ticks.insert(0, (ymin, self._tick_text(ymin)))
        ticks.append((ymax, self._tick_text(ymax)))   # ★ 顶部刻度 = 数据最大值
        # ★ 顶部最大值可能紧挨着上一个整齐刻度(数据 10400 时 10000 与 10400),
        #   两个标签会叠在一起 —— 按像素间距去掉过近的那个(用户 2026-11-11)。
        ticks = self._dedupe_ticks(ticks, ymin, ymax, card.plot.height())
        card.plot.getAxis("left").setTicks([ticks])

    @staticmethod
    def _dedupe_ticks(ticks, ymin, ymax, height_px, min_px=14):
        """去掉纵向间距不足 min_px 的刻度标签; 顶部数据最大值优先保留。"""
        if len(ticks) < 2:
            return ticks
        span = max(float(ymax) - float(ymin), 1e-12)
        min_gap = span * min_px / max(1.0, float(height_px or 220))
        out = []
        for v, label in ticks:
            if out and (v - out[-1][0]) < min_gap:
                if abs(v - float(ymax)) < 1e-9:
                    out.pop()           # 顶掉前一个, 保证最大值标签在
                else:
                    continue
            out.append((v, label))
        return out

    def _apply_follow(self):
        if not self.follow:
            return
        for c in self._cards:
            if c.plot is not None:
                self._fit_card_x(c)

    def update_snapshot(self, snapshot):
        """snapshot = ParamService.latest()。"""
        if not snapshot:
            return
        ts = float(snapshot.get("ts") or 0)
        if not ts:
            return
        if self._t0 is None:
            self._t0 = ts
        t = (ts - self._t0) / 60.0
        ok = snapshot.get("ok") or {}
        changed = False
        for path, key, curve in self._curve_defs:
            if path not in ok:
                continue
            raw = ok[path]
            # 字典类属性(地检): 取指定字段; 普通属性直接用
            value = raw.get(key) if (key and isinstance(raw, dict)) else raw
            # 状态类属性: 映射成数字(如 充电中/充满电 → 1)
            value_map = curve.get("value_map")
            if value_map is not None:
                value = value_map.get(str(value), curve.get("default", 0))
            value = catalog.decode_value(curve, value)
            if curve.get("bit") is not None:
                try:
                    value = (int(value) >> int(curve["bit"])) & 1
                except (TypeError, ValueError):
                    continue
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
            hist_key = _series_key(curve)
            hist = self._history.setdefault(hist_key, deque(maxlen=6000))
            if hist and t <= hist[-1][0]:
                continue
            hist.append((t, value))
            for c in self._cards:
                if hist_key in c.series_data:
                    c.series_data[hist_key] = hist
                    c.last_t[hist_key] = t
                    c.refresh(hist_key)
            changed = True
        if changed:
            self._apply_follow()
