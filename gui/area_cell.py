# -*- coding: utf-8 -*-
"""清扫区域(选区/划区)通用控件 + 机器人地图后台预取。

循环清扫页和续航测试页共用：组合表最后一列是「清扫区域」，
选区行显示「房间1 房间2」，划区行显示「区域1 区域2」，右侧按钮打开地图选择。
"""
import time

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QWidget

from core.apos.map_data import fetch_map
from core.loop_clean import parse_area_ids, parse_point, parse_zones

ZONE_TIP = "划区清扫：点「选择清扫区域」在机器人地图上拖出矩形区域"
POINT_TIP = "定点清扫：点「选择清扫区域」在机器人地图上点选位置"


def show_zones(edit, zones):
    """划区行显示「区域1 区域2」，矩形存控件属性里。"""
    zones = parse_zones(zones)
    edit.setText(" ".join("区域%d" % (i + 1)
                          for i in range(len(zones))) if zones else "")
    if zones:
        edit.setToolTip("划区（毫米坐标）：\n" + "\n".join(
            "区域%d. (%d, %d) → (%d, %d)"
            % (i + 1, z[0], z[1], z[2], z[3])
            for i, z in enumerate(zones)))
    else:
        edit.setToolTip(ZONE_TIP)


def rooms_text(value):
    """房间 ID → 界面显示「房间1 房间2」。"""
    return " ".join("房间%d" % i for i in parse_area_ids(value))


def zones_text(value):
    """矩形列表 → 界面显示「区域1 区域2」。"""
    return " ".join("区域%d" % (i + 1) for i in range(len(parse_zones(value))))


def point_text(value):
    """定点坐标 → 界面显示「定点(500,0)」。"""
    pt = parse_point(value)
    return "定点(%d,%d)" % tuple(pt) if pt else ""


class AreaCell(QWidget):
    """「清扫区域」单元格: 房间 ID 输入框 + 「选择清扫区域」按钮。

    按钮按该行清扫类型打开地图: 选区/分区只能选房间, 划区清扫只能画矩形
    (用户 2026-11-28)。
    """

    def __init__(self, edit, parent=None):
        super().__init__(parent)
        self.edit = edit
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        lay.addWidget(edit, 1)
        self.pick_btn = QPushButton("选择清扫区域")
        self.pick_btn.setObjectName("chipBtn")
        self.pick_btn.setToolTip("在机器人地图上选择房间或画出清扫区域")
        lay.addWidget(self.pick_btn)


class MapFetchThread(QThread):
    """后台读取机器人地图(连接后预取, 点击选择区域时直接命中缓存)。"""

    done = Signal(str, object)      # ip, 地图数据(失败为 None)

    def __init__(self, ip, parent=None):
        super().__init__(parent)
        self.ip = ip

    def run(self):
        try:
            self.done.emit(self.ip, fetch_map(self.ip))
        except Exception:
            self.done.emit(self.ip, None)


class MapCacheMixin:
    """页面级地图缓存：连接后后台预取; 只有重连/测试结束才重新读取。

    使用方需实现 `_ip()`，并在 __init__ 里调用 `_init_map_cache()`、连上
    device.connect_finished 调 `_on_robot_connected`。
    """

    def _init_map_cache(self):
        self._map_cache = {}          # ip -> (ts, 数据)
        self._map_thread = None

    def _on_robot_connected(self, ok, ip, _msg=""):
        if ok:
            self._map_cache.pop(ip, None)     # 重新连接 → 地图可能变了
            self._prefetch_map(ip)

    def _prefetch_map(self, ip=None):
        import os
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return          # 单测里不连真机
        ip = str(ip or self._ip() or "").strip()
        if not ip or ip in self._map_cache:
            return
        if self._map_thread is not None and self._map_thread.isRunning():
            return
        worker = MapFetchThread(ip, self)
        worker.done.connect(self._on_map_fetched)
        self._map_thread = worker
        worker.start()

    def _on_map_fetched(self, ip, data):
        if data:
            self._map_cache[ip] = (time.time(), data)

    def invalidate_map(self, ip=None):
        """地图可能变化时调用(一轮测试结束/重连)。"""
        ip = str(ip or self._ip() or "").strip()
        self._map_cache.pop(ip, None)
        self._prefetch_map(ip)

    def _wait_cached_map(self, ip, timeout=10.0):
        """等后台预取完成(不阻塞界面重绘), 返回缓存数据或 None。"""
        from PySide6.QtCore import QEventLoop
        from PySide6.QtWidgets import QApplication
        if ip in self._map_cache:
            return self._map_cache[ip][1]
        if self._map_thread is None:
            return None
        end = time.time() + timeout
        while (self._map_thread.isRunning() and time.time() < end
               and ip not in self._map_cache):
            QApplication.processEvents(QEventLoop.ExcludeUserInputEvents)
            time.sleep(0.05)
        cached = self._map_cache.get(ip)
        return cached[1] if cached else None
