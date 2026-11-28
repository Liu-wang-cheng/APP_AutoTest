# -*- coding: utf-8 -*-
"""机器人地图解析(纯函数, 不连设备)。"""
import json
import struct

import lz4.block
import pytest

from core.apos.map_data import parse_map_files
from gui.map_picker import room_at

META = {"width": 4, "height": 3, "resolution": 0.05,
        "x_min": 0.0, "y_min": 0.0}


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _segment(data, w=4, h=3, res=0.05, x_min=0.0, y_min=0.0):
    """按真机格式拼 segmentmap: 36 字节头 + lz4 压缩的 width*height 字节。"""
    head = struct.pack("<III", 1, w, h) + struct.pack("<fff", res,
                                                      x_min, y_min)
    head += b"\x00" * 12          # 实测数据从 36 字节开始, 其余头部保留
    return head + lz4.block.compress(bytes(data), store_size=False)


def test_parse_map_files_reads_grid_rooms_and_areas():
    seg = [1, 1, 0, 255, 1, 1, 0, 255, 2, 2, 0, 255]
    data = parse_map_files(
        bytes([127] * 12), json.dumps(META).encode(), b"",
        _segment(seg))
    assert (data["width"], data["height"]) == (4, 3)
    assert len(data["grid"]) == 12
    assert data["segment"] == bytes(seg)
    sm = data["segment_meta"]
    assert (sm["width"], sm["height"]) == (4, 3)
    assert abs(sm["resolution"] - 0.05) < 1e-6   # 头部是 float32
    assert (sm["x_min"], sm["y_min"]) == (0.0, 0.0)
    # 房间分割图: 像素中心落在哪一格就返回哪个房间号
    assert room_at(data, 50, 50) == 1          # 第 1 行第 1 列
    assert room_at(data, 50, 150) == 2         # 第 3 行第 1 列
    assert room_at(data, 150, 50) == 0         # 第 1 行第 3 列是 0(空)
    assert room_at(data, 250, 50) == 0         # 255(未分割) 也算没有房间
    assert room_at(data, 9000, 9000) == 0      # 地图外


def test_parse_map_files_keeps_areas_and_charger():
    area = {"areaValue": [{"id": 100, "cleanCount": 1,
                           "vertexs": [[0, 0], [1000, 0], [1000, 1000],
                                       [0, 1000]]}]}
    charger = {"charger_pose": [-0.75, -0.06]}
    data = parse_map_files(
        bytes([0] * 12), json.dumps(META).encode(),
        json.dumps(area).encode(), b"", json.dumps(charger).encode())
    assert data["areas"]["areaValue"][0]["id"] == 100
    assert data["charger_pose"] == [-0.75, -0.06]
    assert data["segment"] == b""


def test_parse_map_files_survives_bad_segment_map():
    """分割图损坏不能让整张地图打不开(房间只是没有而已)。"""
    data = parse_map_files(bytes([127] * 12), json.dumps(META).encode(),
                           b"", b"garbage" * 10)
    assert data["segment"] == b"" and data["segment_meta"] == {}
    assert len(data["grid"]) == 12


def test_room_centers_for_id_labels():
    """房间 ID 标注位置 = 该房间像素中心(用户 2026-11-28)。"""
    from gui.map_picker import room_centers

    seg = [1, 1, 0, 255, 1, 1, 0, 255, 2, 2, 0, 255]
    data = parse_map_files(bytes([127] * 12), json.dumps(META).encode(),
                           b"", _segment(seg))
    centers = room_centers(data)
    assert set(centers) == {1, 2}
    assert centers[1] == (0.5, 0.5)
    assert centers[2] == (0.5, 2.0)


def test_zone_rects_can_be_moved_on_map(qapp):
    """已画的划区矩形可以拖动位置, 松开后毫米坐标跟着更新(用户 2026-11-28)。"""
    from gui.map_picker import MapSelectorDialog

    data = parse_map_files(bytes([127] * 12), json.dumps(META).encode(), b"")
    dlg = MapSelectorDialog(data, mode="zones")
    dlg.zones = [[0, 0, 1000, 100]]
    dlg._render()
    item = dlg._zone_items[0]
    center = item.mapRectToScene(item.rect()).center()
    assert dlg._zone_item_at(center) is item
    item.setPos(10, 4)          # 10 格=500mm, 4 格=200mm(5cm/格)
    dlg._commit_zone(item)
    # y 轴上下翻转: 世界 y 向上, 移动后 100..0mm 变成 -100..-200mm
    assert dlg.zones == [[500, -200, 1500, -100]]


def test_loop_page_prefetches_map_after_connect(qapp, monkeypatch):
    """连接扫地机后就在后台读地图, 点“选择清扫区域”直接命中缓存(用户 2026-11-28)。"""
    import time
    from PySide6.QtCore import QObject, Signal
    import gui.area_cell as area_cell
    import gui.loop_clean_page as L

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    class Dev(QObject):
        connect_finished = Signal(bool, str, str)

    monkeypatch.setattr(area_cell, "fetch_map",
                        lambda ip: {"ip": ip, "loaded": True})
    dev = Dev()
    p = L.LoopCleanPage(device=dev)
    dev.connect_finished.emit(True, "192.168.10.9", "")
    assert p._map_thread is not None
    end = time.time() + 5
    while time.time() < end:
        qapp.processEvents()
        if "192.168.10.9" in p._map_cache:
            break
        time.sleep(0.02)
    assert p._map_cache["192.168.10.9"][1]["loaded"] is True
    # 已有缓存时不会重复起线程
    p._prefetch_map("192.168.10.9")
    assert "192.168.10.9" in p._map_cache


def test_selector_dialog_preloads_existing_selection(qapp):
    """重新打开地图时带回这一行已选的房间/区域, 不用重选(用户 2026-11-28)。"""
    from gui.map_picker import MapSelectorDialog

    data = parse_map_files(bytes([127] * 12), json.dumps(META).encode(), b"")
    rooms = MapSelectorDialog(data, mode="rooms", rooms=[2, 3])
    assert rooms.rooms == [2, 3]
    assert "房间2" in rooms.info.text() and "房间3" in rooms.info.text()
    zones = MapSelectorDialog(data, mode="zones",
                              zones=[[0, 0, 1500, 1500]])
    assert zones.zones == [[0, 0, 1500, 1500]]
    assert "区域1" in zones.info.text()


def test_selector_dialog_point_mode(qapp):
    """定点模式: 只点选一个位置, 可带回上次的定点(用户 2026-11-28)。"""
    from gui.map_picker import MapSelectorDialog

    data = parse_map_files(bytes([127] * 12), json.dumps(META).encode(), b"")
    dlg = MapSelectorDialog(data, mode="point", point=[500, -200])
    assert dlg.mode == "point" and not dlg.mode_combo.isEnabled()
    assert dlg.point == [500, -200]
    assert "定点" in dlg.info.text()
    dlg._accept()                  # 已有定点 → 直接可确定
    assert dlg.result() == 1
    empty = MapSelectorDialog(data, mode="point")
    empty._accept()                # 没选点 → 不关闭
    assert empty.result() == 0


def test_selector_dialog_locks_mode_by_clean_type(qapp):
    """组合表选了选区/划区后, 地图弹窗只能做对应的操作(用户 2026-11-28)。"""
    from gui.map_picker import MapSelectorDialog

    data = parse_map_files(bytes([127] * 12), json.dumps(META).encode(), b"")
    rooms = MapSelectorDialog(data, mode="rooms")
    assert rooms.mode == "rooms" and not rooms.mode_combo.isEnabled()
    zones = MapSelectorDialog(data, mode="zones")
    assert zones.mode == "zones" and not zones.mode_combo.isEnabled()
    # 不指定时保持原来的可切换行为
    free = MapSelectorDialog(data)
    assert free.mode_combo.isEnabled()
    free.mode_combo.setCurrentIndex(free.mode_combo.findData("zones"))
    assert free.mode == "zones"
