# -*- coding: utf-8 -*-
# 由 gui/main_window.py 拆分而来(结构整理 2026-10-15):
# 通用小控件与列表: 可点击标签/框、流式布局、历史下拉、用例列表。
# ★ 拆分只为可读性 —— 这批类原先就在同一个模块里, 行为未改动。

# 只导入本模块用到的名字(结构整理 2026-10-15)
from PySide6.QtCore import QEvent, QPoint, QRect, QSize, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFontMetrics, QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QFrame,
                                QLabel, QLayout, QListView, QListWidget, QMenu,
                                QStyle, QStyleOptionViewItem, QStyledItemDelegate)
from core.logger import get_logger
from gui import schema
from gui.style import _ARROW_W, _CHECK_MIN_H

log = get_logger()


def mouse_xy(ev):
    """鼠标事件里的**整数坐标** —— Qt6 的正确写法。

    ★ 为什么要有这个小函数(2026-10-16): 老的 `ev.pos()` 在 PySide6 里已弃用,
      每次点击都刷一条 `DeprecationWarning: 'QMouseEvent.pos() const' is marked as
      deprecated` —— CI 的测试日志里攒了 26 条这种警告, 把真正的信号盖掉了。
      Qt6 的替代是 `ev.position()`(返回 QPointF, 浮点), 要整数坐标得再 `.toPoint()`。
      统一走这里: 以后再有 `ev.pos()` 一眼就能看出来(gui/ 里有守护测试盯着)。
    """
    return ev.position().toPoint()


class ClickableLabel(QLabel):
    """可点击的图片预览标签"""
    clicked = Signal()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class ClickableFrame(QFrame):
    """可点击的卡片头部行"""
    clicked = Signal()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class FlowLayout(QLayout):
    """流式布局: 窗口变窄时控件自动换行,顶部横条不再顶死最小宽度"""

    def __init__(self, parent=None, margin=6, spacing=6):
        super().__init__(parent)
        self.setContentsMargins(margin, margin, margin, margin)
        self.setSpacing(spacing)
        self._items = []

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations(Qt.Orientation(0))

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            wid = item.widget()
            if wid is not None and not wid.isVisible():
                continue  # 隐藏控件不占位
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _do_layout(self, rect, test_only):
        """两遍扫描: 先分行并记录行高,再把行内控件按垂直居中放置(跳过隐藏控件)"""
        m = self.contentsMargins()
        left = rect.x() + m.left()
        max_right = rect.right() - m.right()
        spacing = self.spacing()

        # 第一遍: 分行
        lines, cur, cur_h, row_x = [], [], 0, left
        for item in self._items:
            wid = item.widget()
            if wid is not None and not wid.isVisible():
                continue
            w, h = item.sizeHint().width(), item.sizeHint().height()
            if cur and row_x + w > max_right:  # 放不下 → 换行
                lines.append((cur, cur_h))
                cur, cur_h, row_x = [], 0, left
            cur.append((item, w, h))
            cur_h = max(cur_h, h)
            row_x += w + spacing
        if cur:
            lines.append((cur, cur_h))

        # 第二遍: 行内垂直居中
        y = rect.y() + m.top()
        for row, row_h in lines:
            x = left
            for item, w, h in row:
                if not test_only:
                    item.setGeometry(QRect(QPoint(x, y + (row_h - h) // 2), QSize(w, h)))
                x += w + spacing
            y += row_h + spacing
        return y - spacing + m.bottom() - rect.y()


def make_action_menu(parent, on_pick):
    """按分类构造动作选择菜单:一级=分类,二级=该分类下的动作。

    不做平铺长列表 —— 25+ 项一次展开会超出屏幕(用户反馈)。
    on_pick(action_key) 在选中动作时回调。
    """
    menu = QMenu(parent)
    for cat in schema.CATEGORY_ORDER:
        acts = [a for a in schema.ACTIONS if a["category"] == cat]
        if not acts:
            continue
        sub = QMenu(cat, menu)
        for a in acts:
            act = QAction(a["label"], sub)
            act.triggered.connect(lambda _, k=a["key"]: on_pick(k))
            sub.addAction(act)
        menu.addMenu(sub)
    sub = QMenu("其他", menu)
    wait_act = QAction("延时等待", sub)
    wait_act.triggered.connect(lambda: on_pick("sleep"))
    sub.addAction(wait_act)
    menu.addMenu(sub)
    return menu


class _FormCheckBox(QCheckBox):
    """步骤表单里的勾选框(自动截图 / 条件 等)。

    ⚠ 两个坑(真机抓屏实测):
    1. **不能被拉宽**: 表单曾把它拉成 200px 宽, 而 Qt 只认指示器那一小块命中 →
       右边一大片是死区(点不动); 若改成"整块都可点", 又会变成"点旁边空白也选中"
       (用户实测反馈)。所以**保持自然大小**(_add_field 对它跳过宽度限制):
       看到多少就能点多少, 两个毛病都没有。
    2. **悬停时"闪动"**: 真机抓屏(1.5x DPI)看到 —— 未悬停时方框四条边完整, 鼠标
       一悬停就只剩左边和上边, 右/下边整块缺失, 移开又恢复 ⇒ 一进一出闪两下。
       修法: 关掉该控件的 hover 属性, 让它不进入悬停态(勾选框没有需要 hover 的
       行为), 悬停前后像素完全一致(实测 0 差异), 方框两态都完整。
       ⚠ 离屏平台不执行真实绘制, **测不出**这类裁切/悬停态问题, 必须真机抓屏。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setMinimumHeight(_CHECK_MIN_H)
        self.setCursor(Qt.PointingHandCursor)
        self._no_hover()

    def _no_hover(self):
        self.setAttribute(Qt.WA_Hover, False)

    def event(self, ev):
        # ⚠ 构造时关掉 WA_Hover 不够: QSS 在 polish 阶段会把它重新打开, 悬停态就
        #   又画出来了(守护测试抓到)。所以在 polish / 样式变更之后再关一次。
        res = super().event(ev)
        if ev.type() in (QEvent.Polish, QEvent.StyleChange):
            self._no_hover()
        return res


class _HistComboDelegate(QStyledItemDelegate):
    """历史下拉项 delegate: 每项右侧绘制 ✕, 点击即删除该条历史(用户要求)。"""

    _W = 20                    # ✕ 命中区宽度(与 _HistCombo 共用命中判定)

    def paint(self, painter, option, index):
        super().paint(painter, option, index)   # 先画原生文本
        rect = option.rect
        painter.save()
        f = painter.font()
        f.setPixelSize(12)
        painter.setFont(f)
        hovered = bool(option.state & QStyle.State_MouseOver)
        painter.setPen(QColor("#dc2626" if hovered else "#b6c0cc"))
        painter.drawText(QRect(rect.right() - self._W, rect.top(),
                               self._W - 4, rect.height()),
                         Qt.AlignCenter, "\u2715")
        painter.restore()


class _FitCombo(QComboBox):
    """内容自适应的下拉框基类(带/不带历史删除功能的下拉都用它)。

    ★ 解决三个 Qt 坑(2026-09-23 逐个实测):
      1. editable combo 的 AdjustToContents 不随输入变化 → fit_width() 自算
      2. popup 宽度不采用 view.sizeHint/maxWidth → showPopup 后 setFixedSize
      3. sizeHintForRow 在 clear+addItems 后返回 -1 → 高度用首行高×行数
    """

    _POPUP_PAD = 48        # ✕/内边距/安全余量

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setView(_FitListView(self))

    def _font_metrics(self):
        """取用于测量的字体: 可输入下拉用 lineEdit, 只读下拉用自身"""
        le = self.lineEdit()
        return (le or self).fontMetrics()

    def longest_item_width(self):
        fm = self._font_metrics()
        return max([fm.horizontalAdvance(self.itemText(i))
                    for i in range(self.count())] or [0])

    def fit_width(self):
        """宽度 = max(当前值, 最长项) —— 输入型下拉(测试APP/设备名称)。

        ★ 取最长项: 这样输入框与弹出的历史列表**等宽**, 且长条目也能显示完整
        (用户要求: 两者长度一致; 之前按当前值算, 删除后输入框缩短而列表仍宽,
        出现"一个长一个短")
        """
        fm = self._font_metrics()
        w = max(fm.horizontalAdvance(self.currentText()),
                self.longest_item_width()) + 60
        self.setFixedWidth(max(70, min(w, 420)))

    def fit_width_to_items(self):
        """宽度按「最长项」自适应(选择型下拉: 模板/基准图/APP组)"""
        w = self.longest_item_width() + 60
        self.setFixedWidth(max(70, min(w, 420)))

    def _row_height(self):
        """列表行高(px)。

        ★ 不能用 fontMetrics().height()+8 估算: 真机行高由 delegate 的
        sizeHint 决定(约 24px, 受 QSS 字体与 DPI 影响), 比字体高多 4~8px;
        低估会让 popup 高度不足 → 出现滚动条(用户实测: 需要滑动才能看完)。
        """
        view = self.view()
        model = view.model()
        if model is None or model.rowCount() == 0:
            return view.fontMetrics().height() + 8
        # ① 首选「已布局后的真实行高」—— 含 QSS 内边距/DPI 缩放, 最准
        rect = view.visualRect(model.index(0, 0))
        if rect.height() > 0:
            return rect.height()
        # ② 退而求其次: Qt 按实际渲染算的行高
        h = view.sizeHintForRow(0)
        if h > 0:
            return h
        # ③ 最后: delegate.sizeHint(可能不含 QSS padding, 故只是兜底)
        opt = QStyleOptionViewItem()
        opt.font = view.font()
        opt.fontMetrics = QFontMetrics(opt.font)
        size = view.itemDelegate().sizeHint(opt, model.index(0, 0))
        return size.height() if size.height() > 0 else view.fontMetrics().height() + 8

    def fit_popup_now(self):
        """按内容调整 popup 宽度(仅当它正打开时)。

        ★ 只干预**宽度**, 高度交给 Qt —— QComboBoxPrivateContainer 自己会按
        view 内容算高度; 之前用 setFixedSize 连高度一起硬设, 因数不准导致
        「内容多了不变大 / 底部多一行空白」(用户真机实测)。最小干预更稳。
        """
        popup = self.view().window()
        if popup is None or not popup.isVisible():
            return
        # 高度: 先让 view 精确等于内容高, 再让 popup 自己算(含它自身的边框)。
        # ★ 直接给 popup 设"内容高"会少算 2px 边框 → view 差 2px → 出滚动条,
        #   视觉上就是底部多一条空白(用户实测)
        rows = self.count()
        if rows:
            self.view().setFixedHeight(self._row_height() * rows)
            popup.adjustSize()
        else:
            popup.setFixedHeight(30)
        # ★ 宽度必须放在 adjustSize **之后** —— adjustSize 会把宽度也算掉
        if popup.width() != self.width():
            popup.setFixedWidth(self.width())   # 与输入框等宽(用户要求)
        # 诊断日志(真机排查用): 项数 / 实际尺寸 / 行高
        log.info(f"[下拉] 项数={self.count()} 输入框宽={self.width()} "
                 f"popup={popup.width()}x{popup.height()} "
                 f"view={self.view().width()}x{self.view().height()} "
                 f"行高={self._row_height()} "
                 f"滚动条={self.view().verticalScrollBar().maximum()} "
                 f"可见={popup.isVisible()}")

    def set_refresh(self, cb):
        """登记一个"下拉每次打开前"的回调(内容会变的下拉用, 如模板目录)"""
        self._refresh_cb = cb

    def hidePopup(self):
        import time as _t
        dt = (_t.monotonic() - getattr(self, "_shown_at", 0)) * 1000
        if dt < 400:
            from core.logger import get_logger
            get_logger().info(f"[下拉] 展开后 {dt:.0f}ms 就被关闭了(疑似被抢焦点/重建)")
        super().hidePopup()

    def showPopup(self):
        # ★ 打开/关闭都记一笔(带毫秒): 用户反馈过"概率性点一下就收起", 而 offscreen
        #   复现不出来 —— 有这行日志就能看出是 show 之后极短时间内被 hide(自己关的),
        #   还是压根没 show 成功(被别处抢了焦点/重建了控件)。
        import time as _t
        self._shown_at = _t.monotonic()
        from core.logger import get_logger
        get_logger().debug(f"[下拉] showPopup (可见={self.view().isVisible()})")
        cb = getattr(self, "_refresh_cb", None)
        if cb:
            try:
                cb()
            except Exception as e:      # 刷新失败不能挡住下拉
                from core.logger import get_logger
                get_logger().debug(f"[ui] 下拉刷新失败(忽略): {e}")
        super().showPopup()
        self.fit_popup_now()


class _FitListView(QListView):
    """弹出列表: sizeHint 宽度按「最长项」计算。

    ★ combo 显示 popup 时是按 `view.sizeHint()` 决定宽度的, 设 minimumWidth
    无效(所以删除项后宽度不会缩) —— 必须重写 sizeHint 才真正跟随内容。
    """

    _PAD = 48      # ✕ 命中区(20) + 内边距 + 安全余量

    def sizeHint(self):
        s = super().sizeHint()
        model = self.model()
        if model is None:
            return s
        fm = self.fontMetrics()
        longest = 0
        for i in range(model.rowCount()):
            txt = model.data(model.index(i, 0)) or ""
            longest = max(longest, fm.horizontalAdvance(str(txt)))
        return QSize(max(s.width(), longest + self._PAD), s.height())


class _HistCombo(_FitCombo):
    """历史下拉框: 点击项右侧 ✕ 删除该条历史(继承 _FitCombo 的自适应尺寸)。

    ★ 必须用事件过滤器而非 delegate.editorEvent —— QAbstractItemView 处理
    普通点击时**不调用** editorEvent(那只在编辑场景触发), 所以点击会走
    Qt 默认行为把该项填进输入框(用户实测: 点 ✕ 反而填入该记录)。
    在 viewport 上消费 MouseButtonRelease 才能既删除又阻止选中。
    """

    removed = Signal(int)      # 被删项行号

    def __init__(self, parent=None):
        super().__init__(parent)
        self.view().viewport().installEventFilter(self)

    def eventFilter(self, obj, ev):
        if obj is self.view().viewport() and ev.type() == QEvent.MouseButtonRelease:
            pos = mouse_xy(ev)
            idx = self.view().indexAt(pos)
            if idx.isValid():
                rect = self.view().visualRect(idx)
                if pos.x() >= rect.right() - _HistComboDelegate._W:
                    self.removed.emit(idx.row())
                    return True            # 消费事件: 不选中该项
        return super().eventFilter(obj, ev)


class CaseListWidget(QListWidget):
    """用例列表:行内 ↑↓ 箭头 = delegate 绘制 + 命中计算。

    ★ item 保持完全原生(勾选/选中/拖拽/拖拽快照全走 Qt 管线)。
    ⚠ 历史教训:setItemWidget(行内真控件)与原生管线冲突 —— 盖 indicator、
    拦截选中/拖拽、按钮点不到,四连坑(2026-09-21 实测);自定义 QDrag
    重写 startDrag 也会断 InternalMove 管线,都别再碰。
    """

    moved = Signal()

    def __init__(self):
        super().__init__()
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setDefaultDropAction(Qt.MoveAction)
        self._arrow_w = 44          # 右侧箭头命中区宽(↑22 + ↓22)
        self.setItemDelegate(CaseItemDelegate(self))
        # ★ 右键菜单(2026-10-19 用户要求): 复制/粘贴右键选中的用例。
        #   菜单内容由主窗口构建(item 的 UserRole/CASE_STATE_ROLE 只有它认识)。
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)

    def _on_context_menu(self, pos):
        owner = getattr(self, "_owner", None)
        if owner is not None and hasattr(owner, "_show_case_context_menu"):
            owner._show_case_context_menu(pos)

    def keyPressEvent(self, event):
        """Ctrl+C / Ctrl+V 与右键菜单同源(焦点在用例列表上时生效)。"""
        owner = getattr(self, "_owner", None)
        if owner is not None and event.matches(QKeySequence.Copy):
            owner._copy_current_case()
            event.accept()
            return
        if owner is not None and event.matches(QKeySequence.Paste):
            owner._paste_current()
            event.accept()
            return
        super().keyPressEvent(event)

    def set_owner(self, win):
        self._owner = win

    def mousePressEvent(self, event):
        # 箭头命中:直接执行移动,不进基类(避免同时改选中)
        if event.button() == Qt.LeftButton and getattr(self, "_owner", None):
            row, delta = self.itemDelegate().arrow_hit(mouse_xy(event))
            if row is not None:
                item = self.item(row)
                if item is not None:
                    self.setCurrentRow(row)
                    self._owner._move_case(item.data(Qt.UserRole), delta)
                    event.accept()
                    return
        super().mousePressEvent(event)

    def dropEvent(self, event):
        if event.source() is not self:
            return super().dropEvent(event)
        super().dropEvent(event)
        event.accept()
        # ★ 必须延迟到下一拍:dropEvent 内同步 clear() 重建列表会弄坏 Qt
        #   拖拽循环的内部状态(表现为只能拖一次,之后再拖无反应)
        QTimer.singleShot(0, self.moved.emit)


class CaseItemDelegate(QStyledItemDelegate):
    """原生 item 之上补绘右侧 ↑↓ 箭头(纯绘制,不干扰原生列表管线)。
    状态灯不在 delegate 里 —— 画在名字后面需要字体度量,中文字体下会和
    文字重叠(实测);改用 item 的 icon 位:原生布局排在勾选框后、用例名前。"""

    def __init__(self, view):
        super().__init__(view)
        self._view = view

    def paint(self, painter, option, index):
        super().paint(painter, option, index)   # 原生:勾选框 + icon(灯) + 用例名
        rect = option.rect
        painter.save()
        f = painter.font()
        f.setBold(True)
        f.setPixelSize(14)
        painter.setFont(f)
        painter.setPen(QColor("#2563eb" if option.state & QStyle.State_Selected else "#64748b"))
        w = _ARROW_W // 2
        right = rect.right() - 2
        painter.drawText(QRect(right - 2 * w, rect.top(), w, rect.height()),
                         Qt.AlignCenter, "↑")
        painter.drawText(QRect(right - w, rect.top(), w, rect.height()),
                         Qt.AlignCenter, "↓")
        painter.restore()

    def arrow_hit(self, pos):
        """pos(视口坐标)落在哪个箭头上 → (row, ±1);未命中 → (None, 0)"""
        view = self._view
        idx = view.indexAt(pos)
        row = idx.row() if idx.isValid() else -1
        if row < 0:
            return None, 0
        rect = view.visualItemRect(view.item(row))
        if pos.x() < rect.right() - _ARROW_W:
            return None, 0
        mid = rect.right() - _ARROW_W // 2
        return row, (-1 if pos.x() < mid else +1)
