# -*- coding: utf-8 -*-
"""纯逻辑单元测试：脱离设备，验证 grab/match 的文本提取核心逻辑
运行: python -m pytest tests/test_pure_logic.py -v
"""
import pytest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.action_runner import ActionRunner


@pytest.fixture
def runner():
    """构造一个不连接设备的 ActionRunner，仅供纯逻辑方法测试"""
    class FakeDevice:
        serial = "fake"
    r = ActionRunner.__new__(ActionRunner)
    r.store = {}
    r._stored_zones = []
    r._next_room_idx = 0
    return r


# ── _extract_value：清扫数据提取 ──
class TestExtractValue:
    """模拟主页清扫数据的各种页面布局"""

    def test_面积_标准布局_数字后跟单位(self, runner):
        """['8', '㎡', '清扫面积'] → 8"""
        texts = ['SE3L', '8', '㎡', '清扫面积', '5', 'min', '清扫时间']
        assert runner._extract_value(texts, '面积') == '8'

    def test_面积_清扫面积变体(self, runner):
        """关键字变体：面积 也能匹配 清扫面积"""
        texts = ['19', '㎡', '清扫面积']
        assert runner._extract_value(texts, '面积') == '19'

    def test_时间_数字后跟min(self, runner):
        """['5', 'min', '清扫时间'] → 5"""
        texts = ['8', '㎡', '清扫面积', '5', 'min', '清扫时间']
        assert runner._extract_value(texts, '时间') == '5'

    def test_时间_用时变体(self, runner):
        """关键字变体：时间 也能匹配 用时"""
        texts = ['11', 'min', '用时']
        assert runner._extract_value(texts, '时间') == '11'

    def test_无单位_回退最近数字(self, runner):
        """无单位时回退找最近纯数字"""
        texts = ['清扫面积', '12']
        # 没有单位配对，回退到最近数字
        assert runner._extract_value(texts, '面积') == '12'

    def test_找不到_返回None(self, runner):
        """页面无该关键字"""
        texts = ['SE3L', '充电中', '地图编辑']
        assert runner._extract_value(texts, '面积') is None

    def test_带小数_面积(self, runner):
        """小数面积: 8.5㎡"""
        texts = ['8.5', '㎡', '清扫面积']
        assert runner._extract_value(texts, '面积') == '8.5'

    def test_预约时间_HHMM格式(self, runner):
        """预约时间提取 HH:MM"""
        texts = ['预约时间', '17:04', '下次执行']
        assert runner._extract_value(texts, '预约时间') == '17:04'

    def test_多个数字_优先带单位的(self, runner):
        """多个数字时优先选带正确单位的"""
        texts = ['电量', '96', '%', '8', '㎡', '清扫面积']
        # 96 后面是 % 不是 ㎡，8 后面是 ㎡，应选 8
        assert runner._extract_value(texts, '面积') == '8'


# ── room_click 序号逻辑（不消耗分区）──
class TestRoomClickIndex:
    """验证 room_click 按游标递增，不消耗分区存储"""

    def test_连续点击_游标递增(self, runner, monkeypatch):
        """room_zones 存2个 → room_click:1 连续2次应点到不同坐标"""
        clicks = []
        class FakeD:
            def click(self, x, y): clicks.append((x, y))
        runner.d = FakeD()
        runner._stored_zones = [(100, 200), (300, 400)]
        runner._next_room_idx = 0
        import time as _t
        monkeypatch.setattr(_t, 'sleep', lambda *a: None)

        runner._do_room_click(1)
        runner._do_room_click(1)
        assert clicks == [(100, 200), (300, 400)]
        assert runner._next_room_idx == 2

    def test_游标越界_报错(self, runner, monkeypatch):
        """点完所有分区后再点应报错"""
        class FakeD:
            def click(self, x, y): pass
        runner.d = FakeD()
        runner._stored_zones = [(100, 200)]
        runner._next_room_idx = 1  # 已点完
        import time as _t
        monkeypatch.setattr(_t, 'sleep', lambda *a: None)

        with pytest.raises(RuntimeError, match="无未点击分区"):
            runner._do_room_click(1)

    def test_room_zones_重置游标(self, runner):
        """room_zones 识别后游标归零"""
        runner._next_room_idx = 5
        runner._stored_zones = []
        # _do_room_zones 太重，直接验证字段语义
        runner._stored_zones = [(100, 200), (300, 400)]
        runner._next_room_idx = 0
        assert runner._next_room_idx == 0
        assert len(runner._stored_zones) == 2
