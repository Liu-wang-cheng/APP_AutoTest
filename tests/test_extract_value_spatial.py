# -*- coding: utf-8 -*-
"""_extract_value 的空间邻近匹配 + _get_all_nodes 的 bounds 解析

主页清扫数据版面(取自 Test_img/screenshots/全局清扫_10_主界面清扫数据.png,
1080x1920):

       8 m²                  5 min
    清扫面积                清扫时间

数值在上、标签在下。原来 _get_all_texts 把 XML 里的 bounds 全丢了,
_extract_value 只能靠"±3 个下标"猜数字——XML 文档顺序不等于视觉顺序,
列顺序一变就会静默取到隔壁指标的数字。这里验证改用真实坐标后的行为。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.action_runner import ActionRunner


def node(text, x1, y1, x2, y2):
    return (text, (x1, y1, x2, y2))


@pytest.fixture
def runner():
    class FakeD:
        serial = "fake"

    r = ActionRunner.__new__(ActionRunner)
    r.d = FakeD()
    r.store = {}
    return r


# 主页清扫数据:两列并排,数值在标签正上方
MAIN_PAGE = [
    node('SE3L', 460, 110, 620, 160),
    node('8', 232, 262, 270, 332),
    node('㎡', 285, 265, 330, 300),
    node('清扫面积', 205, 370, 325, 405),
    node('5', 768, 262, 806, 332),
    node('min', 820, 265, 870, 300),
    node('清扫时间', 745, 370, 880, 405),
]


class TestSpatialBasic:
    """真实主页版面"""

    def test_面积_取同列数字(self, runner):
        assert runner._extract_value(MAIN_PAGE, '面积') == '8'

    def test_时间_取同列数字(self, runner):
        assert runner._extract_value(MAIN_PAGE, '时间') == '5'

    def test_带小数面积(self, runner):
        page = [
            node('8.5', 226, 262, 276, 332),
            node('㎡', 285, 265, 330, 300),
            node('清扫面积', 205, 370, 325, 405),
        ]
        assert runner._extract_value(page, '面积') == '8.5'


class TestSpatialBeatsIndexOrder:
    """文档顺序被打乱时,空间匹配必须仍然正确

    RN/Flutter 用绝对定位时,树的遍历顺序和视觉顺序可以完全不一致。
    下面这组节点顺序里,面积的值排在时间之后——下标法会取到隔壁列的 96。
    """

    SCRAMBLED = [
        node('清扫面积', 205, 370, 325, 405),
        node('96', 768, 150, 830, 190),      # 另一列(顶部电量)
        node('清扫时间', 745, 370, 880, 405),
        node('5', 768, 262, 806, 332),       # 时间列的值
        node('min', 820, 265, 870, 300),
        node('8', 232, 262, 270, 332),       # 面积列的值(顺序靠后)
        node('㎡', 285, 265, 330, 300),
    ]

    def test_面积_取本列而非隔壁列(self, runner):
        """下标法会返回 96,空间法必须返回 8"""
        assert runner._extract_value(self.SCRAMBLED, '面积') == '8'

    def test_时间_取本列(self, runner):
        assert runner._extract_value(self.SCRAMBLED, '时间') == '5'


class TestSpatialEdgeCases:
    def test_标签上方没有数字_回退下标法(self, runner):
        """版面不匹配时不能返回 None 或错值,应退回旧逻辑"""
        labels_only = [node('清扫面积', 205, 370, 325, 405), '12']
        assert runner._extract_value(labels_only, '面积') == '12'

    def test_关键字不存在_返回None(self, runner):
        page = [node('充电中', 100, 100, 200, 140)]
        assert runner._extract_value(page, '面积') is None

    def test_多个数字同列_取最近的那个(self, runner):
        """同列上下叠着两个数字时,取离标签最近(下方)的那个"""
        page = [
            node('99', 232, 100, 270, 160),   # 更远
            node('8', 232, 262, 270, 332),    # 更近
            node('㎡', 285, 265, 330, 300),
            node('清扫面积', 205, 370, 325, 405),
        ]
        assert runner._extract_value(page, '面积') == '8'

    def test_无单位时仍取同列数字(self, runner):
        page = [
            node('12', 232, 262, 270, 332),
            node('清扫面积', 205, 370, 325, 405),
        ]
        assert runner._extract_value(page, '面积') == '12'


class TestGetAllNodes:
    """_get_all_nodes 保留 bounds —— 这是空间匹配的数据来源"""

    XML = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<hierarchy rotation="0">'
        '<node index="0" text="8" resource-id="" class="android.widget.TextView" '
        'package="com.x" content-desc="" bounds="[232,262][270,332]" />'
        '<node index="1" text="清扫面积" resource-id="" class="android.widget.TextView" '
        'package="com.x" content-desc="" bounds="[205,370][325,405]" />'
        '<node index="2" text="" resource-id="com.x:id/icon" class="android.widget.ImageView" '
        'package="com.x" content-desc="" bounds="[0,0][10,10]" />'
        '</hierarchy>'
    )

    def test_解析出文本与bounds(self, runner):
        class FakeD:
            serial = "fake"

            def dump_hierarchy(self):
                return TestGetAllNodes.XML

        runner.d = FakeD()
        nodes = runner._get_all_nodes()
        assert ('8', (232, 262, 270, 332)) in nodes
        assert ('清扫面积', (205, 370, 325, 405)) in nodes

    def test_bounds在text之前的属性顺序也能解析(self, runner):
        """不能依赖属性出现顺序"""
        xml = ('<hierarchy><node bounds="[1,2][3,4]" class="android.widget.TextView" '
               'text="7" /></hierarchy>')

        class FakeD:
            serial = "fake"

            def dump_hierarchy(self):
                return xml

        runner.d = FakeD()
        assert ('7', (1, 2, 3, 4)) in runner._get_all_nodes()

    def test_空文本节点也保留(self, runner):
        """不筛空文本,索引才能和 _get_all_texts 对齐"""
        class FakeD:
            serial = "fake"

            def dump_hierarchy(self):
                return TestGetAllNodes.XML

        runner.d = FakeD()
        nodes = runner._get_all_nodes()
        assert len(nodes) == 3
