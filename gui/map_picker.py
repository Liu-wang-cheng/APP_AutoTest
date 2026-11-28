# -*- coding: utf-8 -*-
"""清扫区域选择弹窗: 机器人地图 + 房间选择 + 矩形划区。

地图来源 core.apos.map_data.fetch_map(ip)。
  · 选区: 点击房间(segmentmap 解出的颜色块), 可多选
  · 划区: 拖拽画多个矩形(毫米坐标)
"""
from PySide6.QtCore import QPointF, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import (QColor, QFont, QFontMetricsF, QImage, QPainter,
                           QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import (QDialog, QGraphicsEllipseItem, QGraphicsItem,
                               QGraphicsPixmapItem, QGraphicsRectItem,
                               QGraphicsScene, QGraphicsView, QHBoxLayout,
                               QLabel, QPushButton, QSizePolicy,
                               QVBoxLayout)

from gui.widgets import NoWheelComboBox

ROOM_COLORS = [
    (231, 76, 60), (52, 152, 219), (46, 204, 113), (241, 196, 15),
    (155, 89, 182), (26, 188, 156), (230, 126, 34), (52, 73, 94),
]
SEG_UNKNOWN = 255
SEG_IGNORE = 15


def render_base(map_data):
    """占用栅格 → QImage（用 bytearray 一次填像素，比逐点 setPixelColor 快）。"""
    w, h = map_data["width"], map_data["height"]
    raw = map_data["grid"]
    buf = bytearray(w * h * 4)
    for row in range(h):
        base = row * w
        off = (h - 1 - row) * w * 4        # 上下翻转: 世界 y 向上, 图像 y 向下
        for col in range(w):
            v = raw[base + col]
            if v == 127:
                r, g, b = 32, 36, 44
            elif v == 0:
                r, g, b = 12, 14, 18
            else:
                g = 150 + (v % 5) * 18
                r, b = g, g - 14
                g = g - 6
            i = off + col * 4
            buf[i] = b
            buf[i + 1] = g
            buf[i + 2] = r
            buf[i + 3] = 255
    return QImage(bytes(buf), w, h, w * 4, QImage.Format_RGB32).copy()


def render_rooms(map_data, selected=None):
    """房间分割图 → 透明 QImage; selected 内的房间加深。

    一个像素只算一次，直接写 ARGB 缓冲区(Qt 的 Format_ARGB32 在小端上是
    BGRA)，避免几十万次 setPixelColor 调用。
    """
    seg = map_data.get("segment") or b""
    sm = map_data.get("segment_meta") or {}
    if not seg or not sm:
        return None
    w, h = sm["width"], sm["height"]
    selected = set(selected or ())
    buf = bytearray(w * h * 4)
    for y in range(h):
        base = y * w
        off = (h - 1 - y) * w * 4          # 与底图一致: 上下翻转
        for x in range(w):
            v = seg[base + x]
            if v in (0, SEG_UNKNOWN, SEG_IGNORE, 255):
                continue
            r, g, b = ROOM_COLORS[(v - 1) % len(ROOM_COLORS)]
            i = off + x * 4
            buf[i] = b
            buf[i + 1] = g
            buf[i + 2] = r
            buf[i + 3] = 170 if v in selected else 80
    return QImage(bytes(buf), w, h, w * 4, QImage.Format_ARGB32).copy()


def _data_bbox(data, w, h, skip):
    """非空像素的外接矩形 (x0, y0, x1, y1)，右下为开区间。"""
    x0, y0, x1, y1 = w, h, -1, -1
    for y in range(h):
        base = y * w
        for x in range(w):
            if data[base + x] in skip:
                continue
            if x < x0:
                x0 = x
            if x > x1:
                x1 = x
            if y < y0:
                y0 = y
            if y > y1:
                y1 = y
    if x1 < 0:
        return None
    return (x0, y0, x1 + 1, y1 + 1)


def content_rect(map_data):
    """地图有效内容的外接矩形(场景坐标, 已按世界 y 向上的方向翻转)。

    ★ 分割图/占用图里大片是"未知"(255/127)，直接按整张图开弹窗会显得
      弹窗比地图大很多(用户 2026-11-28)；这里只看真正有内容的范围。
    """
    sm = map_data.get("segment_meta") or {}
    seg = map_data.get("segment") or b""
    rects = []
    if seg and sm:
        b = _data_bbox(seg, sm["width"], sm["height"],
                       (0, SEG_UNKNOWN, SEG_IGNORE, 255))
        if b:
            # 图像上下翻转: 文件行 y ∈ [b1, b3) → 场景行 [h-b3, h-b1)
            rects.append((b[0], sm["height"] - b[3],
                          b[2], sm["height"] - b[1]))
    grid = map_data.get("grid") or b""
    if grid:
        b = _data_bbox(grid, map_data["width"], map_data["height"], (127,))
        if b:
            gh = map_data["height"]
            ox = oy = 0.0
            if sm:
                ox = (map_data["x_min"] - sm["x_min"]) / sm["resolution"]
                oy = ((sm["y_min"] + sm["height"] * sm["resolution"])
                      - (map_data["y_min"]
                         + map_data["height"] * map_data["resolution"])) \
                    / sm["resolution"]
            rects.append((b[0] + ox, gh - b[3] + oy,
                          b[2] + ox, gh - b[1] + oy))
    if not rects:
        return None
    return (min(r[0] for r in rects), min(r[1] for r in rects),
            max(r[2] for r in rects), max(r[3] for r in rects))


def room_at(map_data, x_mm, y_mm):
    """毫米坐标 → 房间 ID; 不在房间返回 0。"""
    seg = map_data.get("segment") or b""
    sm = map_data.get("segment_meta") or {}
    if not seg or not sm:
        return 0
    res = sm["resolution"]
    col = int((x_mm / 1000.0 - sm["x_min"]) / res)
    row = int((y_mm / 1000.0 - sm["y_min"]) / res)
    if not (0 <= col < sm["width"] and 0 <= row < sm["height"]):
        return 0
    v = seg[row * sm["width"] + col]
    return v if v not in (0, SEG_UNKNOWN, SEG_IGNORE, 255) else 0


def room_centers(map_data):
    """每个房间 ID 的中心像素(文件坐标); 用于在图上标注分区 ID。"""
    seg = map_data.get("segment") or b""
    sm = map_data.get("segment_meta") or {}
    if not seg or not sm:
        return {}
    w, h = sm["width"], sm["height"]
    sums = {}
    for y in range(h):
        base = y * w
        for x in range(w):
            v = seg[base + x]
            if v in (0, SEG_UNKNOWN, SEG_IGNORE, 255):
                continue
            acc = sums.setdefault(v, [0, 0, 0])
            acc[0] += x
            acc[1] += y
            acc[2] += 1
    return {v: (s[0] / s[2], s[1] / s[2]) for v, s in sums.items() if s[2]}


class ElidedLabel(QLabel):
    """单行提示: 宽度不够时末尾显示省略号, 不会被布局挤成截断。"""

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self._full = text
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

    def setText(self, text):
        self._full = str(text or "")
        self._refresh()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._refresh()

    def _refresh(self):
        fm = self.fontMetrics()
        text = fm.elidedText(self._full, Qt.ElideRight, max(0, self.width()))
        if text != super().text():       # 避免 resize 里反复 setText 造成重排
            QLabel.setText(self, text)


class MapLabel(QGraphicsItem):
    """地图标注: 深色圆角底 + 白字(分区 ID / 区域N), 在任何底色上都清楚。

    以自身中心为原点: setPos(中心点) 即可。
    """

    def __init__(self, text, parent=None):
        super().__init__(parent)
        self.text = str(text)
        self._font = QFont()
        self._font.setPointSize(8)
        self._font.setBold(True)
        fm = QFontMetricsF(self._font)
        w = fm.horizontalAdvance(self.text) + 8
        h = fm.height() + 2
        self._rect = QRectF(-w / 2, -h / 2, w, h)
        self.setAcceptedMouseButtons(Qt.NoButton)
        self.setZValue(30)

    def boundingRect(self):
        return self._rect

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(15, 23, 42, 200))
        painter.drawRoundedRect(self._rect, 3, 3)
        painter.setPen(QColor("#ffffff"))
        painter.setFont(self._font)
        painter.drawText(self._rect, Qt.AlignCenter, self.text)


class ChargerItem(QGraphicsEllipseItem):
    """充电桩: 绿色圆形 + 白色闪电标志。"""

    def __init__(self, cx, cy, r=9):
        super().__init__(cx - r, cy - r, 2 * r, 2 * r)
        self.setPen(QPen(QColor("#ffffff"), 1.2))
        self.setBrush(QColor("#16a34a"))
        self.setZValue(20)

    def paint(self, painter, option, widget=None):
        super().paint(painter, option, widget)
        r = self.rect()
        pts = [(0.58, 0.12), (0.27, 0.56), (0.46, 0.56),
               (0.40, 0.88), (0.75, 0.42), (0.55, 0.42)]
        path = QPainterPath()
        path.moveTo(r.left() + r.width() * pts[0][0],
                    r.top() + r.height() * pts[0][1])
        for px, py in pts[1:]:
            path.lineTo(r.left() + r.width() * px,
                        r.top() + r.height() * py)
        path.closeSubpath()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#ffffff"))
        painter.drawPath(path)


class MapSelectorDialog(QDialog):
    SCALE = 1                     # 场景像素/地图格(1 格=5cm); 弹窗再按地图大小自适应

    def __init__(self, map_data, parent=None, mode=None, rooms=None,
                 zones=None, point=None):
        super().__init__(parent)
        self.data = map_data
        # 打开时带上这一行已经选好的房间/区域/定点(用户 2026-11-28: 重新打开不丢)
        self.rooms = []
        for r in (rooms or []):
            try:
                rid = int(r)
            except (TypeError, ValueError):
                continue
            if rid > 0 and rid not in self.rooms:
                self.rooms.append(rid)
        self.zones = [list(z) for z in (zones or []) if len(z) == 4]
        self.point = None
        if isinstance(point, (list, tuple)) and len(point) >= 2:
            try:
                self.point = [int(point[0]), int(point[1])]
            except (TypeError, ValueError):
                self.point = None
        # mode 固定时(组合表里选了选区/划区/定点)只允许那一种操作
        self.mode = mode if mode in ("rooms", "zones", "point") else "rooms"
        self._fixed_mode = mode in ("rooms", "zones", "point")
        self._origin = None
        self._temp = None
        self._drag = None            # 拖动已有划区
        self._fitting = False        # fitInView 重入保护
        self._base_pix = None        # 底图只渲染一次(点击房间会重画)
        self._base_item = None
        self._room_item = None
        self._room_centers = None
        self._zone_items = []
        self._build()
        self._render()
        self._fit_window()

    # ── UI ──
    def _build(self):
        self.setWindowTitle("选择清扫区域")
        root = QVBoxLayout(self)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("模式"))
        self.mode_combo = NoWheelComboBox()
        self.mode_combo.addItem("选择房间（选区/分区）", "rooms")
        self.mode_combo.addItem("画矩形区域（划区）", "zones")
        self.mode_combo.addItem("点选位置（定点）", "point")
        self.mode_combo.setCurrentIndex(
            self.mode_combo.findData(self.mode))
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        if self._fixed_mode:
            # 模式由组合表决定: 只显示一行文字, 不给用户切换(也省弹窗宽度)
            self.mode_combo.setEnabled(False)
            self.mode_combo.setVisible(False)
            bar.addWidget(QLabel("模式：%s" % {
                "rooms": "选区/分区", "zones": "划区清扫",
                "point": "定点清扫"}.get(self.mode, self.mode)))
        else:
            bar.addWidget(self.mode_combo)
        bar.addStretch(1)
        clear = QPushButton("清空选择")
        clear.clicked.connect(self._clear)
        bar.addWidget(clear)
        root.addLayout(bar)
        # 提示单独一行, 长了自动省略, 不会被模式栏/按钮挤掉
        self.info = ElidedLabel("")
        self.info.setStyleSheet("color:#64748b;font-size:11px;")
        root.addWidget(self.info)

        self.scene = QGraphicsScene(self)
        self.view = QGraphicsView(self.scene, self)
        self.view.setRenderHint(QPainter.Antialiasing)
        self.view.setDragMode(QGraphicsView.NoDrag)
        self.view.mousePressEvent = self._on_press
        self.view.mouseMoveEvent = self._on_move
        self.view.mouseReleaseEvent = self._on_release
        root.addWidget(self.view, 1)

        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("确定")
        ok.setObjectName("runBtn")
        ok.clicked.connect(self._accept)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        root.addLayout(btns)

    def _fit_window(self):
        """弹窗大小跟随地图内容, 最多占屏幕 88%(用户 2026-11-28)。"""
        scale = self.SCALE
        b = content_rect(self.data)
        if b:
            pad = 8
            rect = QRectF(b[0] * scale - pad, b[1] * scale - pad,
                          (b[2] - b[0]) * scale + 2 * pad,
                          (b[3] - b[1]) * scale + 2 * pad)
        else:
            rect = self.scene.itemsBoundingRect()
        if rect.isEmpty():
            self.resize(620, 480)
            return
        self.scene.setSceneRect(rect)
        scr = self.screen()
        avail = (scr.availableGeometry() if scr is not None
                 else QRect(0, 0, 1280, 800))
        chrome_h = 148          # 顶部模式栏 + 提示行 + 底部按钮 + 边距
        w = min(int(rect.width()) + 28, int(avail.width() * 0.88))
        h = min(int(rect.height()) + chrome_h, int(avail.height() * 0.9))
        self.resize(max(400, w), max(360, h))
        QTimer.singleShot(0, self._fit_map)

    def _fit_map(self):
        """整张地图一次性放进视图(不再默认出现滚动条)。"""
        if getattr(self, "_fitting", False):
            return                    # fitInView 可能触发再次 resize, 防重入
        rect = self.scene.sceneRect()
        if rect.isValid() and not rect.isEmpty():
            self._fitting = True
            try:
                self.view.fitInView(rect, Qt.KeepAspectRatio)
            finally:
                self._fitting = False

    def resizeEvent(self, ev):
        # 弹窗被用户放大/缩小后, 地图继续按比例铺满视图
        super().resizeEvent(ev)
        if self.scene.sceneRect().isValid():
            self._fit_map()

    def _render(self):
        self.scene.clear()
        self._zone_items = []
        scale = self.SCALE
        # 底图
        if self._base_pix is None:
            img = render_base(self.data)
            self._base_pix = QPixmap.fromImage(img).scaled(
                img.width() * scale, img.height() * scale,
                Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        item = QGraphicsPixmapItem(self._base_pix)
        sm = self.data.get("segment_meta") or {}
        if sm:
            ox = (self.data["x_min"] - sm["x_min"]) / sm["resolution"]
            # 图像已上下翻转: 底图顶边 = 它的 y_max, 相对分割图顶边(y_max)
            oy = ((sm["y_min"] + sm["height"] * sm["resolution"])
                  - (self.data["y_min"]
                     + self.data["height"] * self.data["resolution"])) \
                / sm["resolution"]
            item.setPos(ox * scale, oy * scale)
        self._base_item = item
        self.scene.addItem(item)
        # 房间
        room_img = render_rooms(self.data, self.rooms)
        if room_img is not None:
            rp = QPixmap.fromImage(room_img)
            rp = rp.scaled(room_img.width() * scale,
                           room_img.height() * scale,
                           Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            self._room_item = QGraphicsPixmapItem(rp)
            self._room_item.setPos(0, 0)
            self.scene.addItem(self._room_item)
        # 保存区域/禁区/地毯
        self._draw_areas()
        # 房间分区 ID
        self._draw_room_labels()
        # 已画矩形
        for i, z in enumerate(self.zones):
            self._add_zone_item(z, i)
        # 定点位置
        self._draw_point()
        self._update_info()

    def _draw_point(self):
        """定点位置: 绿色十字 + 「定点」标注。"""
        if not self.point:
            return
        p = self._mm_to_scene(self.point[0], self.point[1])
        pen = QPen(QColor("#16a34a"), 2)
        for rect in (QRectF(p.x() - 10, p.y() - 1, 20, 2),
                     QRectF(p.x() - 1, p.y() - 10, 2, 20)):
            it = QGraphicsRectItem(rect)
            it.setPen(pen)
            it.setZValue(25)
            self.scene.addItem(it)
        dot = QGraphicsEllipseItem(p.x() - 3, p.y() - 3, 6, 6)
        dot.setBrush(QColor("#16a34a"))
        dot.setPen(QPen(Qt.NoPen))
        dot.setZValue(26)
        self.scene.addItem(dot)
        label = MapLabel("定点")
        label.setPos(p.x(), p.y() - 18)
        self.scene.addItem(label)

    def _meta(self):
        """场景使用的栅格元数据(有房间分割图就用它, 否则用占用栅格)。"""
        sm = self.data.get("segment_meta") or {}
        if sm:
            return sm
        return {"width": self.data["width"],
                "height": self.data["height"],
                "resolution": self.data["resolution"],
                "x_min": self.data["x_min"], "y_min": self.data["y_min"]}

    def _mm_to_scene(self, x_mm, y_mm):
        m = self._meta()
        col = (x_mm / 1000.0 - m["x_min"]) / m["resolution"]
        # 世界 y 向上, 图像 y 向下 → 行 = (y_max - y) / res
        row = (m["y_min"] + m["height"] * m["resolution"]
               - y_mm / 1000.0) / m["resolution"]
        return QPointF(col * self.SCALE, row * self.SCALE)

    def _scene_mm(self, p):
        m = self._meta()
        col = p.x() / self.SCALE
        row = p.y() / self.SCALE
        x = m["x_min"] + col * m["resolution"]
        y = (m["y_min"] + m["height"] * m["resolution"]
             - row * m["resolution"])
        return round(x * 1000), round(y * 1000)

    def _draw_areas(self):
        areas = self.data.get("areas") or {}
        for key, color in (("areaValue", "#2563eb"),
                           ("forbidAreaValue", "#dc2626"),
                           ("extendValue", "#f59e0b")):
            for area in areas.get(key, []) or []:
                pts = area.get("vertexs") or []
                if len(pts) < 3:
                    continue
                sc = [self._mm_to_scene(x, y) for x, y in pts]
                rect = QGraphicsRectItem(
                    QRectF(sc[0], sc[2]).normalized())
                rect.setPen(QPen(QColor(color), 2))
                self.scene.addItem(rect)
        cp = self.data.get("charger_pose")
        if cp:
            p = self._mm_to_scene(cp[0] * 1000, cp[1] * 1000)
            self.scene.addItem(ChargerItem(p.x(), p.y()))

    def _draw_room_labels(self):
        """在每个分区中心标注分区 ID(和组合表的「房间N」对应)。"""
        sm = self.data.get("segment_meta") or {}
        if not sm:
            return
        if self._room_centers is None:
            self._room_centers = room_centers(self.data)
        for rid, (cx, cy) in sorted(self._room_centers.items()):
            p = QPointF(cx * self.SCALE,
                        (sm["height"] - 1 - cy) * self.SCALE)
            label = MapLabel(str(rid))
            label.setPos(p)
            self.scene.addItem(label)

    def _add_zone_item(self, zone, idx=None):
        x1, y1, x2, y2 = zone
        a = self._mm_to_scene(x1, y1)
        b = self._mm_to_scene(x2, y2)
        item = QGraphicsRectItem(QRectF(a, b).normalized())
        item.setPen(QPen(QColor("#7c3aed"), 2))
        item.setBrush(QColor(124, 58, 237, 40))
        item.setCursor(Qt.OpenHandCursor)
        zidx = len(self.zones) - 1 if idx is None else idx
        item.setData(0, zidx)
        label = MapLabel("区域%d" % (zidx + 1), item)
        label.setPos(item.rect().center())
        self.scene.addItem(item)
        self._zone_items.append(item)

    def _zone_item_at(self, p):
        """光标下的已有划区(可拖动)。"""
        for it in self.scene.items(p):
            if isinstance(it, QGraphicsRectItem) and it in self._zone_items:
                return it
        return None

    def _commit_zone(self, item):
        """拖动结束后把场景矩形写回毫米坐标。"""
        r = item.mapRectToScene(item.rect())
        x1, y1 = self._scene_mm(r.topLeft())
        x2, y2 = self._scene_mm(r.bottomRight())
        idx = item.data(0)
        if isinstance(idx, int) and 0 <= idx < len(self.zones):
            self.zones[idx] = [min(x1, x2), min(y1, y2),
                               max(x1, x2), max(y1, y2)]

    def _update_info(self):
        if self.mode == "rooms":
            picked = " ".join("房间%d" % i for i in self.rooms)
            txt = "已选: %s" % (picked or "点击地图上的房间进行选择")
            self.info.setToolTip("点击房间可多选，再次点击取消")
        elif self.mode == "point":
            if self.point:
                txt = "定点: (%d, %d)" % tuple(self.point)
            else:
                txt = "点击地图选择定点位置（1.5m 见方）"
            self.info.setToolTip("点一下地图放置定点；再点其它位置可移动")
        else:
            picked = " ".join("区域%d" % (i + 1)
                              for i in range(len(self.zones)))
            txt = "已画: %s" % (picked or "在空白处拖拽画清扫区域")
            self.info.setToolTip("空白处拖拽新建区域；按住已有区域可拖动位置")
        self.info.setText(txt)

    def _mode_changed(self, *_):
        self.mode = self.mode_combo.currentData()
        self._update_info()

    def _clear(self):
        self.rooms = []
        self.zones = []
        self.point = None
        self._render()

    # ── 交互 ──
    def _on_press(self, ev):
        if ev.button() != Qt.LeftButton:
            return
        p = self.view.mapToScene(ev.position().toPoint())
        if self.mode == "rooms":
            # ★ 选中房间要整张重画(scene.clear): 不在鼠标事件回调里直接删图元,
            #   否则视图内部还持有被删图元的指针 → 返回事件循环时可能崩溃。
            QTimer.singleShot(0, lambda: self._toggle_room(p))
            return
        if self.mode == "point":
            x, y = self._scene_mm(p)

            def _set_point():
                self.point = [x, y]
                self._render()
            QTimer.singleShot(0, _set_point)
            return
        # 按住已画的区域 → 拖动位置；空白处 → 新建矩形
        item = self._zone_item_at(p)
        if item is not None:
            self._drag = {"item": item, "start": p, "pos": item.pos()}
            self.view.setCursor(Qt.ClosedHandCursor)
            return
        self._origin = p
        self._temp = QGraphicsRectItem()
        self._temp.setPen(QPen(QColor("#7c3aed"), 2, Qt.DashLine))
        self.scene.addItem(self._temp)

    def _on_move(self, ev):
        p = self.view.mapToScene(ev.position().toPoint())
        if self._drag is not None:
            d = self._drag
            d["item"].setPos(d["pos"] + (p - d["start"]))
            return
        if self.mode != "zones" or self._origin is None:
            return
        self._temp.setRect(QRectF(self._origin, p).normalized())

    def _on_release(self, ev):
        if self._drag is not None:
            item = self._drag["item"]
            self._drag = None
            self.view.setCursor(Qt.ArrowCursor)
            self._commit_zone(item)
            self._update_info()
            return
        if self.mode != "zones" or self._origin is None:
            return
        p = self.view.mapToScene(ev.position().toPoint())
        r = QRectF(self._origin, p).normalized()
        self._origin = None
        if self._temp is not None:
            self.scene.removeItem(self._temp)
            self._temp = None
        if r.width() < 4 or r.height() < 4:
            return
        x1, y1 = self._scene_mm(r.topLeft())
        x2, y2 = self._scene_mm(r.bottomRight())
        self.zones.append([x1, y1, x2, y2])
        self._add_zone_item(self.zones[-1])
        self._update_info()

    def _toggle_room(self, p):
        x, y = self._scene_mm(p)
        room = room_at(self.data, x, y)
        if not room:
            return
        if room in self.rooms:
            self.rooms.remove(room)
        else:
            self.rooms.append(room)
        self._render()

    def _accept(self):
        if self.mode == "rooms" and not self.rooms:
            self.info.setText("请先在地图上点击要清扫的房间")
            return
        if self.mode == "zones" and not self.zones:
            self.info.setText("请先拖拽画出清扫区域")
            return
        if self.mode == "point" and not self.point:
            self.info.setText("请先在地图上点选定点位置")
            return
        self.accept()
