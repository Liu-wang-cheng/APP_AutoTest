# -*- coding: utf-8 -*-
"""extract_value 空间邻近匹配: 防 XML 文档顺序≠视觉顺序取错隔壁列。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.actions.data_ops import extract_value


def n(x1, y1, x2, y2):
    return (x1, y1, x2, y2)


def test_value_above_label_same_column():
    # 1080 宽两列布局: 数值在上,标签在下
    nodes = [
        ("8", n(150, 300, 250, 360)), ("12", n(650, 300, 750, 360)),
        ("清扫面积", n(140, 400, 260, 440)), ("清扫时间", n(640, 400, 760, 440)),
    ]
    assert extract_value(None, nodes, "清扫面积") == "8"
    assert extract_value(None, nodes, "时间") == "12"


def test_unit_attached_preferred():
    # 同列上方有两个数字,带正确单位(㎡)的优先
    nodes = [
        ("3", n(150, 100, 250, 160)),
        ("26.5", n(150, 300, 290, 360)), ("㎡", n(295, 310, 335, 350)),
        ("清扫面积", n(140, 400, 260, 440)),
    ]
    assert extract_value(None, nodes, "面积") == "26.5"


def test_neighbor_column_not_picked():
    """页面上的数字都带 bounds 时,同列没有就返回 None —— 不回退下标法

    回退会从隔壁列捞一个看起来完全正常的数字回来(静默错值),比取不到更糟。
    """
    nodes = [
        ("99", n(700, 300, 800, 360)),
        ("清扫面积", n(140, 400, 260, 440)),
    ]
    assert extract_value(None, nodes, "清扫面积") is None


def test_no_bounds_still_falls_back_to_index():
    """但数字节点解析不出 bounds 时,空间法无从下手,应退回旧的邻近取值

    否则 grab 会误报"未找到数据" —— 版面不是预期两列时不该直接放弃。
    """
    nodes = [("清扫面积", n(205, 370, 325, 405)), ("12", None)]
    assert extract_value(None, nodes, "面积") == "12"


def test_spatial_wins_over_scrambled_xml_order():
    """XML 里的节点顺序被打乱,空间法仍要取本列的数字(下标法会取到 96)"""
    nodes = [
        ("96", n(640, 300, 740, 360)),        # 隔壁列(时间)
        ("%", n(745, 305, 775, 355)),
        ("8", n(150, 300, 200, 360)),          # 本列(面积)
        ("㎡", n(205, 305, 245, 355)),
        ("清扫面积", n(140, 400, 260, 440)),
        ("清扫时间", n(640, 400, 760, 440)),
    ]
    assert extract_value(None, nodes, "面积") == "8"


def test_index_fallback_no_bounds():
    nodes = [("清扫面积", None), ("8", None)]
    assert extract_value(None, nodes, "清扫面积") == "8"


def test_reservation_time_hhmm():
    nodes = [("预约时间", n(100, 400, 260, 440)), ("2026-09-15 08:30", None)]
    assert extract_value(None, nodes, "预约时间") == "08:30"


def test_time_variant_match():
    """'时间' 应能匹配到 '用时'(模糊匹配变体)"""
    nodes = [
        ("18", n(150, 300, 250, 360)),
        ("用时", n(140, 400, 260, 440)),
    ]
    assert extract_value(None, nodes, "时间") == "18"


def test_no_match_returns_none():
    nodes = [("毫不相关", n(10, 10, 100, 50))]
    assert extract_value(None, nodes, "清扫面积") is None
