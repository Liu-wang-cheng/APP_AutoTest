# -*- coding: utf-8 -*-
"""session 与 timer_ops 里能脱离真机验证的那部分逻辑。

这两个模块大量操作真机(起停 APP、等充电、OCR 滚轮),但里面有几段纯逻辑
一旦错了会静默给出错误结果 —— 设备解析选错机器、电量读成 0、
"最新记录"点歪 —— 所以单独拎出来测。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import session


class FakeElement:
    def __init__(self, exists=True):
        self._exists = exists
        self.clicked = False

    def exists(self, timeout=1):
        return self._exists

    def click(self):
        self.clicked = True

    @property
    def info(self):
        return {"bounds": {"left": 0, "top": 0, "right": 100, "bottom": 50},
                "clickable": True, "text": ""}


class FakeDevice:
    serial = "fake"

    def __init__(self, xml="", present=()):
        self._xml = xml
        self.present = set(present)
        self.clicked_at = None
        self.start_text = None

    def dump_hierarchy(self):
        return self._xml

    def click(self, x, y):
        self.clicked_at = (x, y)

    def __call__(self, **kw):
        for key in ("textContains", "text", "description"):
            if key in kw:
                # textContains 是**子串**匹配 —— 用相等判断会把 "充电" 这类
                # 部分匹配误判为不存在
                if key == "textContains":
                    return FakeElement(any(kw[key] in p for p in self.present))
                return FakeElement(kw[key] in self.present)
        return FakeElement(False)


# ── 设备解析三级策略 ──

def test_device_id_explicit_wins():
    """显式 --device 优先级最高,即使配置里写了别的"""
    cfg = {"device": {"default": "127.0.0.1:7555", "list": [{"id": "emulator-5554"}]}}
    assert session.get_device_id(cfg, "192.168.1.9:5555") == "192.168.1.9:5555"


def test_device_id_from_default_when_auto():
    cfg = {"device": {"default": "127.0.0.1:7555", "list": [{"id": "emulator-5554"}]}}
    assert session.get_device_id(cfg, "auto") == "127.0.0.1:7555"


def test_device_id_falls_back_to_first_in_list():
    """default 也是 auto 时,取列表第一个"""
    cfg = {"device": {"default": "auto", "list": [{"id": "emulator-5554"}]}}
    assert session.get_device_id(cfg, "auto") == "emulator-5554"


def test_device_id_missing_config_raises():
    """配置缺键直接 KeyError(源项目如此,不做防御性默认值)"""
    with pytest.raises(KeyError):
        session.get_device_id({"device": {"default": "auto"}}, "auto")


# ── 电量提取 ──

def test_battery_takes_max_percent():
    """页面可能同时出现多个 N%(如充电功率),取最大当电量"""
    xml = '<node text="5%"/><node text="85%"/><node text="20%"/>'
    assert session.get_battery_level(FakeDevice(xml=xml)) == 85


def test_battery_missing_returns_minus_one():
    """读不到电量返回 -1 —— 调用方据此跳过检查而不是当成 0%"""
    assert session.get_battery_level(FakeDevice(xml='<node text="充电中"/>')) == -1


def test_is_charging_checks_text():
    assert session.is_charging(FakeDevice(present=("充电中",))) is True
    assert session.is_charging(FakeDevice(present=("清扫中",))) is False


# ── add_timer 参数解析 ──

class FakeD:
    serial = "fake"

    def __init__(self, xml="", descs=()):
        self._xml = xml
        self._descs = set(descs)
        self.clicked = []
        self.long_clicked = []
        self.text_clicked = []

    def dump_hierarchy(self):
        return self._xml

    def click(self, x, y):
        self.clicked.append((x, y))

    def long_click(self, x, y, duration=None):
        self.long_clicked.append((x, y, duration))

    def screenshot(self, path=None, format=None):
        """模板匹配会调它 —— 给一张全黑图,匹配必然失败(这正是期望的)"""
        import numpy as np
        return np.zeros((1920, 1080, 3), dtype=np.uint8)

    def window_size(self):
        return (1080, 1920)

    def __call__(self, **kw):
        if "className" in kw:
            return []                                  # 没有 Switch 控件
        owner = self

        class E(FakeElement):
            def click(self):
                owner.text_clicked.append(kw)

        if "description" in kw:
            return E(kw["description"] in self._descs)
        if "text" in kw:
            return E(True)                             # 文本定位默认命中
        if "textContains" in kw:
            return E(True)
        return E(False)


def _runner(device):
    from core.runner import ActionRunner
    return ActionRunner(device, {"step_interval": 0, "default_timeout": 1,
                                 "click_timeout": 1}, case_name="t")


def test_add_timer_string_form():
    """add_timer 允许写成纯字符串(= 只给 add_text),照样能点到添加按钮"""
    from core.actions.timer_ops import do_add_timer
    d = FakeD()
    do_add_timer(_runner(d), {"add_timer": "添加"})
    assert d.text_clicked, "应该点到了添加按钮"
    assert d.text_clicked[0].get("text") == "添加"


def test_add_timer_deletes_old_when_at_limit():
    """任务数按 Timer_TimerCell* 的 content-desc 统计,达上限先长按删一条"""
    from core.actions.timer_ops import do_add_timer
    xml = ('<node content-desc="Timer_TimerCell0" bounds="[0,100][100,200]"/>'
           '<node content-desc="Timer_TimerCell1" bounds="[0,300][100,400]"/>'
           '<node content-desc="Timer_TimerCell2" bounds="[0,500][100,600]"/>')
    # max_tasks=2 → 删除目标为 Timer_TimerCell1
    d = FakeD(xml=xml, descs=("Timer_TimerCell1",))
    do_add_timer(_runner(d), {"add_timer": {"add_text": "添加", "max_tasks": 2}})

    assert d.long_clicked, "达到上限时应长按删除旧任务"
    _, _, duration = d.long_clicked[0]
    assert duration == 4, "删除用的长按时长应为 4 秒"
    assert d.text_clicked, "删除后应继续点击添加按钮"


def test_add_timer_skips_delete_when_under_limit():
    """没到上限就不该删任何任务"""
    from core.actions.timer_ops import do_add_timer
    xml = '<node content-desc="Timer_TimerCell0" bounds="[0,100][100,200]"/>'
    d = FakeD(xml=xml, descs=("Timer_TimerCell0",))
    do_add_timer(_runner(d), {"add_timer": {"add_text": "添加", "max_tasks": 5}})
    assert not d.long_clicked, "只有 1 条、上限 5 条,不该删东西"
    assert d.text_clicked


# ── latest_record 定位策略 ──

def test_latest_record_prefers_area_entry():
    """优先点「面积…」开头的条目(涂鸦记录列表首条的固定文案,最稳)"""
    from core.actions.timer_ops import do_latest_record
    xml = ('<node text="2026-09-15 08:30" bounds="[0,100][200,140]"/>'
           '<node text="面积 25.5㎡" bounds="[0,200][200,240]"/>')
    d = FakeD(xml=xml)
    do_latest_record(_runner(d), {})
    assert d.text_clicked, "应该点到某条记录"
    # 命中的应是「面积」那条(优先),不是时间那条
    assert "面积" in d.text_clicked[0].get("text", "")


def test_latest_record_raises_when_empty():
    """列表为空时必须报错,不能静默通过"""
    from core.actions.timer_ops import do_latest_record
    with pytest.raises(RuntimeError, match="未找到清扫记录条目"):
        do_latest_record(_runner(FakeD(xml="<hierarchy/>")), {})


# ── 启动自愈:闪退补拉 ──

def test_wait_foreground_polls_until_package_foreground(monkeypatch):
    from core import session
    monkeypatch.setattr(session.time, "sleep", lambda s: None)

    class D:
        n = 0

        def app_current(self):
            D.n += 1
            return {"package": "pkg" if D.n >= 2 else "other"}

    assert session._wait_foreground(D(), "pkg", timeout=5) is True


def test_wait_foreground_timeout_returns_false(monkeypatch):
    from core import session
    monkeypatch.setattr(session.time, "sleep", lambda s: None)

    class D:
        def app_current(self):
            return {"package": "other"}

    assert session._wait_foreground(D(), "pkg", timeout=1) is False


def test_restart_app_self_heals_on_startup_crash(monkeypatch):
    """启动即闪退 → 自动补拉,最多 3 次;第 3 次成功"""
    from core import session
    calls = {"stop": 0, "start": 0}
    results = iter([False, False, True])          # 前两次疑似闪退,第三次成功
    monkeypatch.setattr(session, "_wait_foreground",
                        lambda d, p, timeout=20: next(results))
    monkeypatch.setattr(session.time, "sleep", lambda s: None)

    class D:
        def app_stop(self, p):
            calls["stop"] += 1

        def app_start(self, p, act=None):
            calls["start"] += 1

    cfg = {"app": {"package": "pkg", "main_activity": "act"}, "target_device": "SE3L"}
    session.restart_app(D(), cfg, enter_page=False)
    assert calls["start"] == 3 and calls["stop"] == 3


def test_restart_app_normal_start_no_extra_restart(monkeypatch):
    """正常启动:一次到位,不触发多余的重启"""
    from core import session
    calls = {"stop": 0, "start": 0}
    monkeypatch.setattr(session, "_wait_foreground", lambda d, p, timeout=20: True)
    monkeypatch.setattr(session.time, "sleep", lambda s: None)

    class D:
        def app_stop(self, p):
            calls["stop"] += 1

        def app_start(self, p, act=None):
            calls["start"] += 1

    cfg = {"app": {"package": "pkg", "main_activity": "act"}, "target_device": "SE3L"}
    session.restart_app(D(), cfg, enter_page=False)
    assert calls["start"] == 1 and calls["stop"] == 1


def test_restart_app_raises_when_never_starts(monkeypatch):
    """连续 3 次都闪退:明确报错,不让用例在坏状态里继续跑"""
    from core import session
    monkeypatch.setattr(session, "_wait_foreground", lambda d, p, timeout=20: False)
    monkeypatch.setattr(session.time, "sleep", lambda s: None)

    class D:
        def app_stop(self, p):
            pass

        def app_start(self, p, act=None):
            pass

    cfg = {"app": {"package": "pkg", "main_activity": "act"}, "target_device": "SE3L"}
    with pytest.raises(RuntimeError, match="反复启动失败"):
        session.restart_app(D(), cfg, enter_page=False)


# ── 前置检查结果可见(2026-09-21:prepare 返回结果字典 → GUI 表格与报告) ──

class _FakeDeviceForPrepare:
    """最小桩: 充电中/电量100%/地图就绪"""

    def dump_hierarchy(self):
        return '<node text="100%" /><node text="地图编辑" />'

    def textContains(self, kw):
        class _R:
            def exists(self, timeout=1):
                return True
        return _R()

    def text(self, kw):
        return self.textContains(kw)


def test_prepare_returns_results_dict(monkeypatch):
    """prepare 返回四项结果:勾选的 True/False,未勾选 None"""
    from core import session
    monkeypatch.setattr(session, "restart_app", lambda d, cfg, enter_page=True: None)
    monkeypatch.setattr(session, "ensure_charging", lambda d, **kw: True)
    monkeypatch.setattr(session, "ensure_map_loaded", lambda d, **kw: False)
    monkeypatch.setattr(session, "ensure_battery", lambda d, **kw: True)
    cfg = {"target_device": "SE3L"}
    got = session.prepare(_FakeDeviceForPrepare(), cfg)
    assert got == {"restart": True, "charging": True,
                   "map_load": False, "battery": True}
    # 未勾选 → None(GUI 不写报告)
    got = session.prepare(_FakeDeviceForPrepare(), cfg,
                          charging=False, map_load=False, battery=False)
    assert got == {"restart": True, "charging": None,
                   "map_load": None, "battery": None}


def test_prepare_cancel_marks_pending_false(monkeypatch):
    """中断(received stop)时「勾选但未执行到」的项按未完成(False)落表;
    未勾选的保持 None(不检查,不算失败)"""
    from core import session
    monkeypatch.setattr(session, "restart_app", lambda d, cfg, enter_page=True: None)
    cfg = {"target_device": "SE3L"}
    got = session.prepare(_FakeDeviceForPrepare(), cfg,
                          charging=False, should_cancel=lambda: True)
    assert got == {"restart": True, "charging": None,
                   "map_load": False, "battery": False}


def test_pre_step_desc_with_battery():
    """前置行描述:充电/电量行带机器实际电量,其他行不带"""
    from gui.runner_thread import pre_step_desc
    assert pre_step_desc("battery", 78) == "前置-电量≥50%(当前电量 78%)"
    assert pre_step_desc("charging", 45) == "前置-等待充电(当前电量 45%)"
    assert pre_step_desc("battery", -1) == "前置-电量≥50%"
    assert pre_step_desc("restart", 78) == "前置-重启APP"
    assert pre_step_desc("map_load", 78) == "前置-地图加载"


def test_ensure_charging_not_charging_clicks_recharge(monkeypatch):
    """未充电 → 点回充模板 → 轮询到充电态返回 True(30s 轮询 sleep 全部 mock 掉)"""
    from core import session
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    clicks = []
    monkeypatch.setattr(session, "click_template",
                        lambda d, name, timeout=10: clicks.append(name) or (10, 10))
    states = [False, True, True]      # 预检 False → 点击后变 True
    monkeypatch.setattr(session, "is_charging", lambda d: states.pop(0) if states else True)

    class D:
        def textContains(self, kw):
            class R:
                def exists(self, timeout=1):
                    return False       # 无确认弹窗
            return R()

        def __call__(self, **kw):
            return self.textContains(kw.get("textContains", ""))

    assert session.ensure_charging(D(), timeout=60) is True
    assert clicks == ["开始回充.png"], "未充电应点击回充模板"


def test_ensure_charging_timeout_returns_false(monkeypatch):
    """始终不充电 → 超时返回 False(不无限等待;cancel 可提前退出)"""
    from core import session
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    monkeypatch.setattr(session, "is_charging", lambda d: False)
    monkeypatch.setattr(session, "click_template", lambda d, name, timeout=10: (1, 1))

    class D:
        def textContains(self, kw):
            class R:
                def exists(self, timeout=1):
                    return False
            return R()

        def __call__(self, **kw):
            return self.textContains(kw.get("textContains", ""))

        def dump_hierarchy(self):
            return ""               # 电量读取失败 → 跳过,不干扰超时路径

    assert session.ensure_charging(D(), timeout=0.1) is False
