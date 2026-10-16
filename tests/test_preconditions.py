# -*- coding: utf-8 -*-
"""前置条件: 可编辑/可新增(列表驱动) + 配置读写 + 通用文本检查。

2026-09-23 用户要求: 前置条件不止可选, 还要能改参数、能新增;
新增项要填「名称」和「操作内容」(检测文本→执行操作)。
"""
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import session  # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


class FakeDev:
    """可控设备桩: present 里的文本视为"存在", click/press 记录调用"""

    def __init__(self, present=()):
        self.present = set(present)
        self.pressed = []
        self.clicked = []

    def __call__(self, **kw):
        owner = self
        key = next((k for k in ("textContains", "text", "textMatches", "description")
                    if k in kw), None)

        class R:
            def exists(self, timeout=1):
                if key is None:
                    return False
                if key == "textMatches":     # 多文本走正则(见 session._any_present)
                    return any(re.search(kw[key], t) for t in owner.present)
                return kw[key] in owner.present

            def click(self):
                owner.clicked.append(kw.get(key))

        return R()

    def click(self, x, y):
        """设备级点击(坐标直点, 如前置条件「点击空白处」)"""
        self.clicked_at = (x, y)

    def press(self, key):
        self.pressed.append(key)

    def app_stop(self, pkg):
        pass

    def dump_hierarchy(self):
        # 多文本判定是读层级里 text 的子串(见 session._any_present) → 桩要能"看见" present
        return "".join(f'<node text="{p}"/>' for p in sorted(self.present))


# ── 配置读写(注释/换行符保留) ──

def test_save_preconditions_keeps_comments_and_crlf(tmp_path):
    """写前置条件段: 保留注释、保留 CRLF、不动其它键"""
    from core.driver import load_preconditions, save_preconditions
    p = tmp_path / "config.yaml"
    p.write_bytes("# 顶部注释\r\napp:\r\n  name: 涂鸦智能\r\n\r\ntarget_device: 111\r\n"
                  .encode("utf-8"))
    items = [{"type": "restart", "enabled": True},
             {"type": "battery", "enabled": True, "min_level": 80}]
    save_preconditions(items, str(p))
    text = p.read_bytes().decode("utf-8")
    assert "# 顶部注释" in text and "\r\n" in text, "注释与 CRLF 必须保留"
    assert "target_device: 111" in text, "其它键不受影响"
    assert load_preconditions(str(p)) == items


def test_save_preconditions_replaces_existing_section(tmp_path):
    """已有 preconditions 段 → 整段替换, 不重复追加"""
    from core.driver import load_preconditions, save_preconditions
    p = tmp_path / "config.yaml"
    p.write_text("app:\n  name: x\npreconditions:\n- type: restart\n  enabled: true\n"
                 "target_device: y\n", encoding="utf-8")
    save_preconditions([{"type": "charging", "enabled": True}], str(p))
    text = p.read_text(encoding="utf-8")
    assert text.count("preconditions:") == 1, "不能重复插入"
    assert "target_device: y" in text and "app:" in text
    assert load_preconditions(str(p)) == [{"type": "charging", "enabled": True}]


def test_load_preconditions_default_none(tmp_path):
    """未配置返回 None(调用方回退默认值)"""
    from core.driver import load_preconditions
    p = tmp_path / "c.yaml"
    p.write_text("app:\n  name: x\n", encoding="utf-8")
    assert load_preconditions(str(p)) is None


# ── 列表驱动执行 ──

def test_prepare_items_runs_in_order_and_reports(monkeypatch):
    """按列表顺序执行, 结果含 key/desc/ok; 未勾选(disabled)的被跳过"""
    calls = []
    monkeypatch.setattr(session, "restart_app",
                        lambda d, cfg, enter_page=True, should_cancel=None:
                        calls.append("restart"))
    monkeypatch.setattr(session, "ensure_charging",
                        lambda d, **kw: calls.append("charging") or True)
    monkeypatch.setattr(session, "ensure_battery",
                        lambda d, **kw: calls.append("battery") or False)
    monkeypatch.setattr(session.time, "sleep", lambda s: None)   # 执行间隔(默认5s)不真等
    items = [
        {"type": "restart", "enabled": True},
        {"type": "charging", "enabled": True},
        {"type": "battery", "enabled": True, "min_level": 80},
        {"type": "map_load", "enabled": False},        # 未勾选 → 跳过
    ]
    res = session.prepare_items(FakeDev(), {"target_device": "SE3L"}, items)
    assert calls == ["restart", "charging", "battery"], "应按顺序执行且跳过未勾选"
    assert len(res) == 3
    assert [r["ok"] for r in res] == [True, True, False]
    assert res[2]["desc"] == "电量门槛≥80%", f"展示名应带参数: {res[2]['desc']}"


def test_charging_flow_clicks_recharge_then_polls(monkeypatch):
    """★ 等待充电的流程(用户 2026-10-11 复述): 首次判断非充电 → 点击回充按钮 →
    之后**只循环判定状态**(按超时时间决定等多久), 进入充电即返回 True —— 且**只点一次**。
    """
    from core import session
    dev = FakeDev()
    clicks = []
    monkeypatch.setattr(session, "click_template",
                        lambda d, name, timeout=10, **kw: clicks.append(name) or (500, 600))
    monkeypatch.setattr(session, "get_battery_level", lambda d: 42)
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    seq = iter([False, True])          # 进入时非充电, 循环里第一次判定就充上了
    monkeypatch.setattr(session, "is_charging", lambda d, text=None: next(seq))
    assert session.ensure_charging(dev, timeout=60) is True
    assert clicks == ["开始回充.png"], f"回充按钮应只点一次: {clicks}"


def test_charging_waits_until_timeout_then_returns_false(monkeypatch):
    """一直没充上 → 按超时结束并返回 False(不假装成功)"""
    from core import session
    clicks = []
    monkeypatch.setattr(session, "click_template",
                        lambda d, name, timeout=10, **kw: clicks.append(name) or (1, 2))
    monkeypatch.setattr(session, "get_battery_level", lambda d: 42)
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    monkeypatch.setattr(session, "is_charging", lambda d, text=None: False)
    assert session.ensure_charging(FakeDev(), timeout=0) is False
    assert clicks, "连按钮都没点就去等, 流程不对"


def test_charging_skips_click_when_already_charging(monkeypatch):
    """已经在充电: 不点回充按钮, 直接通过"""
    from core import session
    clicks = []
    monkeypatch.setattr(session, "click_template", lambda d, name, timeout=10, **kw: clicks.append(name))
    monkeypatch.setattr(session, "is_charging", lambda d, text=None: True)
    assert session.ensure_charging(FakeDev(), timeout=60) is True
    assert clicks == []


@pytest.mark.parametrize("tpl,expect", [(None, "开始回充.png"), ("回充.png", "回充.png"),
                                        ("开始回充.png", "开始回充.png")])
def test_charging_recharge_template_is_configurable(monkeypatch, tpl, expect):
    """★ 回充按钮的**模板名可配**(用户要求: 有的组就是拿本组的模板图去点的)"""
    from core import session
    seen = []
    monkeypatch.setattr(session, "click_template",
                        lambda d, name, timeout=10, **kw: seen.append(name) or (1, 2))
    monkeypatch.setattr(session, "is_charging", lambda d, text=None: False)
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    session.ensure_charging(FakeDev(), timeout=0, recharge_tpl=tpl)
    assert seen == [expect]


def test_charging_text_first_then_template_fallback(monkeypatch):
    """配了文本就先按文本点; 文本命不中再落到模板(两条都要能走)"""
    from core import session
    seen = []
    monkeypatch.setattr(session, "click_template",
                        lambda d, name, timeout=10, **kw: seen.append(name) or (1, 2))
    monkeypatch.setattr(session, "is_charging", lambda d, text=None: False)
    monkeypatch.setattr(session.time, "sleep", lambda s: None)

    dev = FakeDev(present=("回充",))                 # 页面上有「回充」文本
    session.ensure_charging(dev, timeout=0, recharge_text="回充", recharge_tpl="回充.png")
    assert dev.clicked == ["回充"], f"应先按文本点: {dev.clicked}"
    assert seen == [], "文本已经点到了, 不该再走模板"

    dev2 = FakeDev(present=())                       # 文本命不中
    session.ensure_charging(dev2, timeout=0, recharge_text="回充", recharge_tpl="回充.png")
    assert dev2.clicked == []
    assert seen == ["回充.png"], f"文本没命中应落到模板: {seen}"


# ── 充电前置的留痕(用户 2026-10-16) ──

def _logs(caplog, level="INFO"):
    """把 caplog 里的消息拼成一段文本(断言只要能看到就行)"""
    return "\n".join(r.getMessage() for r in caplog.records
                     if r.levelname in ("INFO", "WARNING", "ERROR"))


def test_charging_miss_logs_searched_texts_and_page_sample(monkeypatch, caplog):
    """★ 用户 2026-10-16: "充电前置检测没有命中和未命中内容信息" +
    "我给了 充电,满电 两个文本为何都识别不到" ——
    未命中时必须写出**查了哪些词**和**页面上当时有什么**, 否则只能靠人复现去猜。
    """
    monkeypatch.setattr(session, "is_charging", lambda d, text=None: False)
    monkeypatch.setattr(session, "click_template", lambda d, n, **kw: None)
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    dev = FakeDev(present=("正在清洁", "干燥拖布中"))
    with caplog.at_level("INFO"):
        session.ensure_charging(dev, timeout=0, ready_text="充电，满电")
    txt = _logs(caplog)
    assert "未命中充电判定文本「充电、满电」" in txt, txt
    assert "正在清洁" in txt and "干燥拖布中" in txt, f"页面当时有什么要看得到: {txt}"


def test_charging_timeout_reports_battery_dropping(monkeypatch, caplog):
    """★ 超时要能自己说出"回充根本没生效"(用户日志: 等了 20 分钟, 电量 45%→34%) ——
    只写"超时"的话, 分不清是设备问题、模板问题还是状态文本没更新。"""
    monkeypatch.setattr(session, "is_charging", lambda d, text=None: False)
    monkeypatch.setattr(session, "click_template", lambda d, n, **kw: (966, 336))
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    seq = iter([45, 44, 34])
    monkeypatch.setattr(session, "get_battery_level",
                        lambda d: next(seq, 34))
    with caplog.at_level("INFO"):
        # timeout 给个极小值: sleep 被打桩成 no-op, 循环按**真实时钟**推进,
        # 0.2s 内轮几次就够走完"起点电量 → 超时」这条路(不 busy loop 太久)
        assert session.ensure_charging(FakeDev(), timeout=0.2) is False
    txt = _logs(caplog)
    assert "超时" in txt and "45% → 34%" in txt, txt
    assert "不升反降" in txt and "回充**没有生效**" in txt, txt


def test_charging_warns_when_recharge_template_score_is_low(monkeypatch, caplog):
    """★ 用户 2026-10-16 的真实事故: 回充模板只有 0.62 分(引擎阈值就是 0.62, 零余量),
    结果点在了"启动"按钮上 —— 机器人继续清扫(电量不升反降)。日志必须喊出来。"""
    monkeypatch.setattr(session, "is_charging", lambda d, text=None: False)
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    monkeypatch.setattr(session, "get_battery_level", lambda d: 50)

    def _click(d, name, timeout=10, should_cancel=None, detail=None):
        if detail is not None:
            detail.update(score=0.62, scale=1.2, source="ncc")
        return (966, 336)

    monkeypatch.setattr(session, "click_template", _click)
    with caplog.at_level("INFO"):
        session.ensure_charging(FakeDev(), timeout=0, recharge_tpl="开始回充.png")
    txt = _logs(caplog)
    assert "匹配分 0.62@尺度 1.20x" in txt, txt
    assert "置信度低" in txt and "裁剪模板" in txt, txt


def test_charging_recharge_settings_are_declared_and_pass_through(monkeypatch):
    """★ 两个参数要 ①在 PRECONDITION_TYPES 里声明(GUI 才显示输入框) ②被 _run_one 透传"""
    from core import session
    keys = {p["key"]: p for p in session.PRECONDITION_TYPES["charging"]["params"]}
    assert "recharge_tpl" in keys and "recharge_text" in keys, f"参数没声明: {list(keys)}"
    assert keys["recharge_tpl"]["type"] == "text"      # 对话框按 text 渲染输入框
    assert keys["recharge_tpl"]["default"] == "开始回充.png"
    assert keys["recharge_text"]["type"] == "text"

    seen = []
    monkeypatch.setattr(session, "click_template",
                        lambda d, name, timeout=10, **kw: seen.append(name) or (1, 2))
    monkeypatch.setattr(session, "is_charging", lambda d, text=None: False)
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    session._run_one(FakeDev(), {"target_device": "X"},
                     {"type": "charging", "timeout": 0, "recharge_tpl": "我的回充.png"},
                     on_progress=None, should_cancel=None)
    assert seen == ["我的回充.png"], f"_run_one 没把配置传下去: {seen}"


def test_charging_summary_shows_configured_button(monkeypatch):
    """展示名里要能看出自己配了什么(默认不占地方)"""
    from core import session
    assert session._item_label({"type": "charging", "timeout": 1200}) == "等待充电(20分钟)"
    lbl = session._item_label({"type": "charging", "timeout": 1200,
                               "recharge_tpl": "回充.png", "recharge_text": "回充"})
    assert "回充.png" in lbl and "文本:回充" in lbl, lbl


def test_item_label_distinguishes_instances():
    """同类型多实例的展示名要能区分(报告里靠它识别)"""
    assert session._item_label({"type": "battery", "min_level": 50}) == "电量门槛≥50%"
    assert session._item_label({"type": "battery", "min_level": 80}) == "电量门槛≥80%"
    a = session._item_label({"type": "text_check", "name": "首页就绪",
                             "wait_text": "首页"})
    assert a == "首页就绪(出现「首页」)", f"实际: {a}"
    assert session._item_label({"type": "restart"}) == "重启 APP"


# ── 通用文本检查(用户新增的自定义前置项) ──

def test_ensure_map_loaded_logs_hit_text(monkeypatch, caplog):
    """★ 地图加载成功日志要带**命中的就绪文本**(用户 2026-10-13): 配了多个就绪词时,
    只写"已加载"看不出是按哪个判的 —— 与断言的命中留痕同一思路。"""
    import logging
    dev = FakeDev(present=("地图编辑", "正在清扫"))
    lg = session.log
    recs = []

    class _H(logging.Handler):
        def emit(self, record):
            recs.append(record.getMessage())

    h = _H(level=logging.INFO)
    lg.addHandler(h)
    old = lg.level
    lg.setLevel(logging.INFO)
    try:
        assert session.ensure_map_loaded(dev, timeout=1, rounds=1) is True
    finally:
        lg.removeHandler(h)
        lg.setLevel(old)
    hit = [m for m in recs if "地图已加载" in m]
    assert hit, recs
    assert "命中「地图编辑」" in hit[0], hit[0]


def test_text_check_passes_when_text_appears(monkeypatch):
    """等待文本出现 → 已出现即通过"""
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    dev = FakeDev(present=("首页",))
    assert session.ensure_text_check(dev, wait_text="首页", timeout=10) is True


def test_text_check_passes_when_text_absent(monkeypatch):
    """等待文本消失 → 已不存在即通过"""
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    dev = FakeDev(present=())          # 页面上没有该文本
    assert session.ensure_text_check(dev, absent_text="加载中", timeout=10) is True


def test_text_check_fault_state_fails_fast(monkeypatch):
    """★ 故障哨兵(用户 2026-10-13): 故障词 + 连续 N 次(默认 3, 误触防护)→ 抛
    FaultStateError 带整段原文, 不等满 timeout。本测试把 streak 设成 1 验证
    "立即抛"路径; 默认 3 的多次累计见下一条。"""
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    session.set_status_words("风机故障", "", fault_streak=1, idle_streak=1)
    dev = FakeDev(present=("风机故障, 请重启设备",))
    with pytest.raises(session.FaultStateError) as ei:
        session.ensure_text_check(dev, wait_text="首页", timeout=3600)
    assert "风机故障, 请重启设备" in str(ei.value), str(ei.value)
    session.set_status_words("", "", fault_streak=3, idle_streak=3)  # 还原默认


def test_prepare_items_reports_fault_reason(monkeypatch):
    """★ 前置失败原因进结果(2026-10-13): 撞故障词的前置项带**整段原文**, 进结果表
    /报告/阻断详情 —— 不再只有干巴巴的"超时或失败"。streak=1 时立即失败, dump
    次数是个位, 不轮询到超时。"""
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    session.set_status_words("风机故障", "", fault_streak=1, idle_streak=1)

    class _CountDev(FakeDev):
        dumps = 0

        def dump_hierarchy(self):
            _CountDev.dumps += 1
            return super().dump_hierarchy()

    _CountDev.dumps = 0
    dev = _CountDev(present=("风机故障, 请重启设备",))
    res = session.prepare_items(
        dev, {}, [{"type": "text_check", "name": "首页就绪",
                   "wait_text": "首页", "enabled": True, "timeout": 3600}])
    assert res[0]["ok"] is False
    assert "风机故障, 请重启设备" in res[0].get("reason", ""), res[0]
    assert _CountDev.dumps <= 3, \
        f"检测到故障应立即失败(dump 了 {_CountDev.dumps} 次)"
    session.set_status_words("", "", fault_streak=3, idle_streak=3)


# ── 阻断即停 + 执行间隔(用户 2026-10-16) ──

#: 前置类型 → 它真正调用的那个 session 函数(打桩用)
_ITEM_FUNC = {"restart": "restart_app", "charging": "ensure_charging",
              "battery": "ensure_battery", "map_load": "ensure_map_loaded",
              "enter_device": "ensure_device_page"}


def _stub_items(monkeypatch, outcomes):
    """把各前置类型替换成可编排的桩: outcomes = {"charging": False, ...} → 调用序列"""
    calls = []

    def _mk(name):
        def _f(d, *a, **kw):
            calls.append(name)
            return outcomes[name]
        return _f

    for t, val in outcomes.items():
        monkeypatch.setattr(session, _ITEM_FUNC[t], _mk(t))
    return calls


def test_prepare_items_stops_at_first_blocking_failure(monkeypatch):
    """★ 用户 2026-10-16 的日志: "等待充电"超时后, 后面的"电量门槛"接着跑了 6 分钟,
    直到人手动停止 —— 前置项之间是有依赖的, 继续跑只会把失败拖长、把日志搅浑。
    现在: 第一条未通过(且不是"失败不阻断")→ 后面的项记成**未执行**(中性显示),
    立刻返回, 由调用方弹窗暂停。
    """
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    calls = _stub_items(monkeypatch, {"restart": True, "charging": False,
                                      "battery": True, "map_load": True})
    items = [{"type": "restart", "enabled": True},
             {"type": "charging", "enabled": True},
             {"type": "battery", "enabled": True},
             {"type": "map_load", "enabled": True}]
    res = session.prepare_items(FakeDev(), {}, items)
    assert calls == ["restart", "charging"], f"充电失败后不该再跑后面的: {calls}"
    assert [r["ok"] for r in res] == [True, False, False, False]
    assert res[1].get("skipped") is not True, "失败项本身不是'未执行'"
    assert res[2].get("skipped") and res[3].get("skipped"), \
        f"后面的项该记成'未执行'(中性): {res[2:]}"
    assert all(r["ok"] is False for r in res[2:]), "未执行项不是'通过'"


def test_prepare_items_continues_after_soft_failure(monkeypatch):
    """勾了「失败不阻断」的项(如地图加载): 失败也继续跑后面的前置项"""
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    calls = _stub_items(monkeypatch, {"map_load": False, "battery": True})
    items = [{"type": "map_load", "enabled": True, "soft": True},
             {"type": "battery", "enabled": True}]
    res = session.prepare_items(FakeDev(), {}, items)
    assert calls == ["map_load", "battery"], f"不阻断的项失败后应继续: {calls}"
    assert [r["ok"] for r in res] == [False, True]


def test_enter_device_and_map_load_default_to_not_blocking(monkeypatch):
    """★ 默认值要按**类型**给: 这两项从设计上就是"确认不了也继续执行"(见函数注释),
    不能因为新加的阻断语义把原来能跑完的流程改成卡在弹窗上等人。
    而"等待充电/电量门槛"这类默认就是阻断的(用户 2026-10-16 的诉求)。"""
    assert session.item_is_soft({"type": "enter_device"}) is True
    assert session.item_is_soft({"type": "map_load"}) is True
    assert session.item_is_soft({"type": "charging"}) is False
    assert session.item_is_soft({"type": "battery"}) is False
    assert session.item_is_soft({"type": "charging", "soft": True}) is True, "显式勾选优先"
    assert session.item_is_soft({"type": "map_load", "soft": False}) is False, "显式取消优先"


def test_prepare_items_announces_every_item_before_running_it(monkeypatch):
    """★ 每条前置项**开始**都要吭一声(用户 2026-10-16: "充电检测已经通过了, 跑后面的
    电量检测时, 执行结果中为何没有显示电量检测的执行中状态")。

    原来只有「自定义步骤」型会发开始事件(它内部每一步各发一次), 等待充电/电量门槛/
    地图加载这些从头到尾不吭声 —— 那一行要等整项跑完才冒出来; 电量门槛这种"等几十分钟"
    的项, 期间结果表一片空白, 看着像卡死。

    两条判据: ① 非 steps 型**逐条**发开始事件, desc 与完成行的 desc 一致(item_label);
             ② steps 型**不发**父项的开始事件(它内部步骤行已经是"执行中", 再发会让
                完成时的父行另起一行 → 两行)。
    """
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    _stub_items(monkeypatch, {"restart": True, "charging": True, "battery": True})
    started = []
    session.prepare_items(FakeDev(), {}, [
        {"type": "restart", "enabled": True},
        {"type": "charging", "enabled": True},
        {"type": "steps", "enabled": True, "name": "结束烘干",
         "steps": [{"desc": "点", "back": True}]},
        {"type": "battery", "enabled": True},
    ], on_step_start=lambda desc, attempt=1: started.append(desc))
    assert started[0:2] == ["重启 APP", "等待充电"], started
    # 第 3 项是 steps 型: 只有它**内部**那一步(带父项名前缀), 没有父项自己那一条
    assert started[2].startswith("结束烘干(1步): "), started
    assert "结束烘干(1步)" not in started, f"steps 型不该发父项开始事件: {started}"
    # 第 4 项(电量门槛)照旧要有 —— 这正是用户看不到"执行中"的那一项
    assert started[3] == "电量门槛≥50%", started


def test_prepare_items_waits_interval_between_items(monkeypatch):
    """★ 执行间隔(用户 2026-10-16: "每个前置检测的执行间隔设置成默认5s"): 两项之间等 5s,
    **第一项前不等**(那时还没有"上一项", 白等只是拖延); 每项都能自己改。

    ★ 盯的是 `session._sleep`(前置间隔真正走的那条), 不是 `time.sleep` ——
      后者是**全局 time 模块**的桩, CI 上别的后台线程(界面/扫描)在这期间睡一觉就会被
      记进来, 断言随机变红(实测: 本地怎么跑都过, CI 上收到一个 [2])。
    """
    slept = []
    monkeypatch.setattr(session, "_sleep", lambda s, cancel=None: (slept.append(s), True)[1])
    _stub_items(monkeypatch, {"restart": True, "charging": True, "battery": True})
    session.prepare_items(FakeDev(), {}, [{"type": "restart", "enabled": True},
                                          {"type": "charging", "enabled": True},
                                          {"type": "battery", "enabled": True}])
    assert slept == [5, 5], f"两条间隔, 第一项前不睡: {slept}"


def test_item_interval_is_configurable(monkeypatch):
    """间隔可配: 0 = 不等(旧配置/老用例不受影响)。同样盯 `session._sleep`(见上一条)"""
    slept = []
    monkeypatch.setattr(session, "_sleep", lambda s, cancel=None: (slept.append(s), True)[1])
    _stub_items(monkeypatch, {"charging": False, "battery": True})
    session.prepare_items(FakeDev(), {}, [{"type": "charging", "enabled": True,
                                           "interval": 0},
                                          {"type": "battery", "enabled": True}])
    assert slept == [], f"interval=0 不该等: {slept}"
    assert session.item_interval({}) == session.PRECONDITION_INTERVAL_DEFAULT == 5
    assert session.item_interval({"interval": "12"}) == 12
    assert session.item_interval({"interval": "abc"}) == 5      # 写错 → 回默认, 不炸


def test_streak_protection_requires_3_hits(monkeypatch):
    """★ 误触防护(用户 2026-10-13): 单帧命中不算故障(防止半秒消失的 toast /
    状态栏瞬闪的"等待"字样被误判), 必须连续 3 帧才触发 → 可避免误判; 同时
    验证: 1 次命中不抛, 2 次命中不抛, 第 3 次才抛 FaultStateError。"""
    from core.session import set_status_words, scan_status_words, _status_words
    # 用 set_status_words 后立即清掉 streak 计数, 干净起步
    set_status_words("MyFault", "", fault_streak=3, idle_streak=3)
    from core import session
    session._streak_counters.clear()
    xml = '<node text="MyFault 出现"/>'
    # 1 次 + 2 次: 不抛
    for i in range(2):
        kind, word, full = scan_status_words(xml=xml)
        assert kind == "fault"      # 词表命中, 但累计不到 3 也不该抛
        # text_present_detail 内部的 streak 防护: 累计到 2 也不抛
        from core.session import text_present_detail
        try:
            text_present_detail(_EmptyDev(), ["x"], xml=xml)
        except session.FaultStateError:
            pytest.fail(f"第 {i+1} 次就抛了, 应等 3 次")
    # 第 3 次: 抛
    with pytest.raises(session.FaultStateError) as ei:
        text_present_detail(_EmptyDev(), ["x"], xml=xml)
    assert "MyFault" in str(ei.value)
    # 中间隔一帧不命中 → 累计清零
    session._streak_counters.clear()
    xml2 = '<node text="干净页面"/>'
    from core.session import scan_status_words as _ss
    kind2, _, _ = _ss(xml=xml2)
    assert kind2 is None


class _EmptyDev:
    """最小设备桩: 满足 text_present_detail 的接口(不调用 d,xml 是显式传入)"""
    def dump_hierarchy(self):
        return ""


def test_prepare_items_step_start_callback():
    """★ 前置子步骤的"执行中"行(2026-10-13): 每步开始回调 desc 带**前置项名前缀**
    —— 与完成行的 desc(`前置-项名: 步骤名`)对得上, 界面才能就地改成 PASS/FAIL。"""
    seen = []
    items = [{"type": "steps", "name": "自定义步骤", "enabled": True,
              "steps": [{"desc": "等一下", "wait": 0.05}]}]
    session.prepare_items(FakeDev(), {"runner": {"step_interval": 0}}, items,
                          on_step_start=lambda desc, attempt=1:
                          seen.append((desc, attempt)))
    assert seen and seen[0][0] == "自定义步骤(1步): 等一下" and seen[0][1] == 1, seen


def test_text_check_timeout_runs_configured_action(monkeypatch):
    """超时后按配置执行操作: back=按返回键 / click:文本=点击该文本"""
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    dev = FakeDev(present=())          # 目标文本永不出现
    assert session.ensure_text_check(dev, wait_text="永远不出现", timeout=0.01,
                                     on_timeout="back") is False
    assert dev.pressed == ["back"], "超时应按返回键"
    dev2 = FakeDev(present=("重试",))
    session.ensure_text_check(dev2, wait_text="永远不出现", timeout=0.01,
                              on_timeout="click:重试")
    assert dev2.clicked == ["重试"], "超时应点击指定文本"
    dev3 = FakeDev(present=())
    session.ensure_text_check(dev3, wait_text="永不出现", timeout=0.01,
                              on_timeout="none")
    assert not dev3.pressed and not dev3.clicked, "none 不应有任何操作"


def test_text_check_skips_when_no_text_configured(monkeypatch):
    """未填任何文本 → 跳过并视为通过(不阻塞执行)"""
    assert session.ensure_text_check(FakeDev(), name="空检查") is True


# ── 地图加载的判断文本可配置 ──

def test_map_load_custom_texts(monkeypatch):
    """地图加载的就绪/加载中文案可配置(换 APP 时文案不同)"""
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    dev = FakeDev(present=("地图就绪了",))
    assert session.ensure_map_loaded(dev, timeout=0.01, rounds=1,
                                     ready_text="地图就绪了",
                                     loading_text="地图渲染中") is True
    dev2 = FakeDev(present=("地图就绪了", "地图渲染中"))     # 仍在加载
    assert session.ensure_map_loaded(dev2, timeout=0.01, rounds=1,
                                     ready_text="地图就绪了",
                                     loading_text="地图渲染中") is False


# ── 多文本判断(同一处文案在不同 APP/机型上不一样) ──

def test_split_texts_separators():
    """一个字段可填多个文本: 逗号/顿号/分号/换行 都能分隔, 空项丢弃"""
    assert session._split_texts("地图编辑,地图") == ["地图编辑", "地图"]
    assert session._split_texts("地图编辑，地图、清扫地图") == ["地图编辑", "地图", "清扫地图"]
    assert session._split_texts("地图编辑;地图\n清扫地图") == ["地图编辑", "地图", "清扫地图"]
    assert session._split_texts("  ") == []
    assert session._split_texts("") == []
    assert session._split_texts(["A", " B "]) == ["A", "B"]      # 已是列表也接受


def test_map_load_multi_texts(monkeypatch):
    """★ 就绪/加载中文本各支持多个: 就绪命中任一即可; 加载中任一仍在 ⇒ 未就绪。

    真机背景: 只填一个「地图编辑」时, 用户设备上 6 轮重进都判定未就绪。
    """
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    # 实际文案是「地图」, 配置里写了两个 → 应判定就绪
    ok = FakeDev(present=("地图",))
    assert session.ensure_map_loaded(ok, timeout=0.01, rounds=1,
                                     ready_text="地图编辑,地图",
                                     loading_text="地图正在加载") is True
    # 就绪文案命中, 但加载中文案命中(多个里任一个) → 仍未就绪
    loading = FakeDev(present=("地图", "加载中"))
    assert session.ensure_map_loaded(loading, timeout=0.01, rounds=1,
                                     ready_text="地图编辑,地图",
                                     loading_text="地图正在加载,加载中") is False
    # 就绪文案一个都没命中 → 未就绪
    miss = FakeDev(present=("首页",))
    assert session.ensure_map_loaded(miss, timeout=0.01, rounds=1,
                                     ready_text="地图编辑,地图",
                                     loading_text="地图正在加载") is False


def test_text_check_multi_texts(monkeypatch):
    """★ 文本检查同样支持多个: 等待出现=任一命中; 等待消失=全部不在"""
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    assert session.ensure_text_check(FakeDev(present=("主页",)), wait_text="首页,主页",
                                     timeout=0.01) is True
    assert session.ensure_text_check(FakeDev(present=("首页",)), wait_text="首页,主页",
                                     timeout=0.01) is True
    assert session.ensure_text_check(FakeDev(present=("首页",)), wait_text="主页,设置",
                                     timeout=0.01) is False
    # 等待消失: 只要还有任一个在, 就不算通过
    assert session.ensure_text_check(FakeDev(present=("请稍候",)),
                                     absent_text="加载中,请稍候", timeout=0.01) is False
    assert session.ensure_text_check(FakeDev(present=("首页",)),
                                     absent_text="加载中,请稍候", timeout=0.01) is True


# ── 类型定义完整性 ──

def test_precondition_types_cover_defaults():
    """默认前置项的每个 type 都要在可选类型表里(对话框才能编辑)"""
    for item in session.DEFAULT_PRECONDITIONS:
        assert item["type"] in session.PRECONDITION_TYPES, item["type"]
    # 「自定义步骤」类型必须有名称 + 步骤字段(用户要求: 像用例一样写步骤)
    st = session.PRECONDITION_TYPES["steps"]
    keys = [p["key"] for p in st["params"]]
    assert "name" in keys and "steps_yaml" in keys, f"字段: {keys}"
    # 原「检测文本」类型已按用户要求移除(自定义步骤已覆盖)
    assert "text_check" not in session.PRECONDITION_TYPES


# ── GUI: 前置条件设置对话框(2026-09-23) ──

def test_precondition_dialogs_do_not_auto_close(qapp, monkeypatch, tmp_path):
    """★ 对话框按钮不得响应回车/焦点变化(autoDefault), 否则点某个勾选框就会关窗"""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from gui import main_window as mw
    items = [{"type": "restart", "enabled": True},
             {"type": "battery", "enabled": True, "min_level": 50}]
    dlg = mw.PreconditionsDialog(items, None)
    dlg.show()
    try:
        qapp.processEvents()
        from PySide6.QtWidgets import QPushButton as _PB
        # 顺序列(第1列)是装着 ↑↓ 的容器, 里面两个按钮也不能是默认按钮
        arrows = dlg.table.cellWidget(0, 1).findChildren(_PB)
        assert len(arrows) == 2, f"顺序列应有 ↑↓ 两个按钮: {arrows}"
        assert all(not b.autoDefault() and not b.isDefault() for b in arrows), \
            "↑↓ 按钮不应是默认按钮"
        for col in (2, 3):            # 编辑/删除按钮
            b = dlg.table.cellWidget(0, col)
            assert not b.autoDefault() and not b.isDefault(), \
                f"表格第{col}列按钮不应是默认按钮"
        # ★ 第一列是表格原生 checkState(不用 cellWidget —— 后者会闪烁)
        assert dlg.table.cellWidget(0, 0) is None, "第一列不应嵌控件"
        it = dlg.table.item(0, 0)
        assert it is not None and (it.flags() & Qt.ItemIsUserCheckable), "应为原生勾选"
        it.setCheckState(Qt.Unchecked)          # 模拟取消勾选
        qapp.processEvents()
        assert dlg.isVisible(), "改动勾选不应关闭对话框"
        assert dlg.items[0]["enabled"] is False, "勾选变化应同步到 items"
        it.setCheckState(Qt.Checked)
        qapp.processEvents()
        assert dlg.items[0]["enabled"] is True
    finally:
        dlg.close()
    # 编辑对话框同样不能自动关闭
    from PySide6.QtWidgets import QPushButton
    ed = mw.PreconditionEditDialog({"type": "battery", "enabled": True,
                                    "min_level": 50}, None)
    ed.show()
    try:
        qapp.processEvents()
        btns = [b for b in ed.findChildren(QPushButton) if b.text() in ("确定", "取消")]
        assert btns, "编辑对话框应有确定/取消按钮"
        assert all(not b.autoDefault() for b in btns), "编辑对话框按钮不应是默认按钮"
    finally:
        ed.close()


def test_add_precondition_menu_lists_all_types(qapp, monkeypatch, tmp_path):
    """「添加前置条件」是下拉菜单, 列出全部可选类型(与添加步骤同风格)"""
    from PySide6.QtWidgets import QPushButton
    from gui import main_window as mw
    items = [{"type": "restart", "enabled": True}]
    dlg = mw.PreconditionsDialog(items, None)
    try:
        btn = next(b for b in dlg.findChildren(QPushButton)
                   if "添加前置条件" in b.text())
        menu = btn.menu()
        assert menu is not None, "添加按钮应带下拉菜单"
        labels = [a.text() for a in menu.actions()]
        assert len(labels) == len(session.PRECONDITION_TYPES), \
            f"菜单应列出全部类型: {labels}"
        assert any("自定义" in x for x in labels), "应含自定义类型"
        # 菜单项各自绑定到 _add_of_type(不在此调用 —— 它会弹出模态对话框,
        # 离屏下 exec() 会阻塞; 对话框行为由 PreconditionEditDialog 的测试覆盖)
    finally:
        dlg.close()


# ── 前置条件可以写「用例步骤」(用户要求: 和添加测试步骤一样) ──

def test_precondition_shots_get_prefix(tmp_path):
    """★ 前置自定义步骤的截图带「前置」文件名前缀(用户 2026-10-15: 与用例步骤同目录,
    都是 stepNN_描述.png 的话, 序号+描述恰好相同时会互相覆盖)。"""
    items = [{"type": "steps", "name": "自定义步骤", "enabled": True,
              "steps": [{"desc": "等一下", "wait": 0.05}]}]
    dev = _ShotDev()
    res = session.prepare_items(dev, {"runner": {"step_interval": 0}}, items,
                                shot_subdir="甲用例_r20260101_090000_第1次")
    shot = res[0]["sub"][0].get("screenshot", "")
    assert shot and os.path.basename(shot).startswith("前置step"), shot


def test_precondition_steps_carry_elapsed(tmp_path):
    """★ 前置子步骤也要有耗时(用户 2026-10-13: "前置执行时的if子步骤为何没有算耗时")。

    用例路径的耗时是 runner_thread 的 on_result 包装算的, 前置的 runner 原来没有
    这个包装 —— collect 到的是原始结果(根本没有 elapsed 字段), 结果表里一列空白。
    """
    items = [{"type": "steps", "name": "自定义步骤", "enabled": True,
              "steps": [{"desc": "等一下", "wait": 0.05},
                        {"desc": "再等一下", "wait": 0.05}]}]
    res = session.prepare_items(FakeDev(), {"runner": {"step_interval": 0}}, items)
    sub = res[0]["sub"]
    assert len(sub) == 2, sub
    for sr in sub:
        assert isinstance(sr.get("elapsed"), (int, float)) and sr["elapsed"] >= 0, \
            f"子步骤没有耗时: {sr}"


class _ShotDev(FakeDev):
    """带 screenshot 的设备桩: 记录发起的截图路径(验证前置步骤默认截图)"""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.shots = []

    def screenshot(self, path=None, format=None):
        self.shots.append(path)
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"png")


def test_precondition_steps_screenshot_by_default(tmp_path):
    """★ 前置的自定义步骤**默认截图**(用户 2026-10-13: "还有一些前置的自定义步骤
    没有截图"): 以前只有点击类/显式 screenshot 的步骤才截, wait/assert/if 这些
    子步骤什么都没有 —— 与"前置也默认截图"的初衷(2.0.6)相悖。"""
    items = [{"type": "steps", "name": "自定义步骤", "enabled": True,
              "steps": [{"desc": "等一下", "wait": 0.05}]}]
    dev = _ShotDev()
    res = session.prepare_items(dev, {"runner": {"step_interval": 0}}, items,
                                shot_subdir="甲用例-第1次")
    sr = res[0]["sub"][0]
    assert sr.get("screenshot"), f"非点击步骤也该有截图: {sr}"
    assert dev.shots, "runner 没有发起截图"


def test_precondition_steps_type_runs_steps(monkeypatch):
    """type=steps 的前置项 → 用 ActionRunner 顺序执行这些步骤"""
    calls = []

    class FakeRunner:
        def __init__(self, d, cfg, case_name="", **kw):
            calls.append(("init", case_name))
            self.results = [{"desc": "点击开始清扫", "passed": True, "error": "",
                             "screenshot": "shots/p1.png"}]

        def run_steps(self, steps):
            calls.append(("run", steps))
            return True

    monkeypatch.setattr("core.runner.ActionRunner", FakeRunner)
    steps = [{"desc": "点击开始清扫", "click": "开始清扫.png"},
             {"desc": "等待充电", "assert": "充电中", "timeout": 0}]
    items = [{"type": "steps", "enabled": True, "name": "前置准备", "steps": steps}]
    res = session.prepare_items(FakeDev(), {"runner": {"step_interval": 0}}, items)
    assert res[0]["ok"] is True
    assert ("init", "前置准备") in calls, "应用前置条件名创建 runner"
    assert ("run", steps) in calls, "应把步骤交给 runner 执行"
    assert res[0]["desc"] == "前置准备(2步)", f"展示名: {res[0]['desc']}"


def test_precondition_steps_type_reports_failure(monkeypatch):
    """步骤执行失败 → 该前置项判失败(供阻断逻辑使用)"""
    class FailRunner:
        def __init__(self, *a, **kw):
            self.results = [{"desc": "x", "passed": False, "error": "炸了", "screenshot": ""}]

        def run_steps(self, steps):
            return False

    monkeypatch.setattr("core.runner.ActionRunner", FailRunner)
    items = [{"type": "steps", "enabled": True, "name": "坏的步骤",
              "steps": [{"desc": "x", "click": "y"}]}]
    res = session.prepare_items(FakeDev(), {}, items)
    assert res[0]["ok"] is False


def test_precondition_steps_empty_is_skipped(monkeypatch):
    """没写步骤 → 视为通过(不阻塞), 且不建 runner"""
    def _boom(*a, **kw):
        raise AssertionError("空步骤不应创建 runner")

    monkeypatch.setattr("core.runner.ActionRunner", _boom)
    items = [{"type": "steps", "enabled": True, "name": "空", "steps": []}]
    res = session.prepare_items(FakeDev(), {}, items)
    assert res[0]["ok"] is True


def test_edit_dialog_exposes_interval_and_soft(qapp):
    """★ 通用参数要在编辑框里能改(用户 2026-10-16):
    「执行间隔」(默认 5s) + 「失败不阻断」—— 后者对"确认不了也继续"的老类型
    (进入设备页/地图加载)要**默认勾上**, 免得一编辑保存就改了老行为。"""
    from gui import main_window as mw
    dlg = mw.PreconditionEditDialog({"type": "charging", "enabled": True,
                                     "timeout": 1200}, None)
    try:
        assert "interval" in dlg._edits and "soft" in dlg._edits, dlg._edits.keys()
        w, _prm = dlg._edits["interval"]
        assert w.text() == "5", f"间隔默认 5s: {w.text()!r}"
        assert dlg._edits["soft"][0].isChecked() is False
        v = dlg.values()
        assert v["interval"] == 5 and v["soft"] is False
    finally:
        dlg.close()

    dlg2 = mw.PreconditionEditDialog({"type": "enter_device", "enabled": True}, None)
    try:
        assert dlg2._edits["soft"][0].isChecked() is True, \
            "进入设备页默认不阻断(老行为: 确认不了也继续执行)"
        assert dlg2.values()["soft"] is True
    finally:
        dlg2.close()


def test_steps_editor_is_card_based(qapp):
    """★ steps 类型用「卡片式步骤编辑器」(与用例编辑同一套组件), 不是 YAML 文本框"""
    from gui import main_window as mw
    dlg = mw.PreconditionEditDialog(
        {"type": "steps", "enabled": True, "name": "前置准备",
         "steps": [{"desc": "点击开始清扫", "click": "开始清扫.png"}]}, None)
    try:
        ed = dlg._edits["steps_yaml"][0]
        assert isinstance(ed, mw._StepsEditor), "应为卡片式编辑器"
        assert len(ed.steps()) == 1, "已有步骤应回填"
        # 添加步骤(与用例编辑相同的入口)
        ed.add_step("back")
        ed.add_step("sleep")          # 延时等待(真动作, 见 gui/schema.py)
        assert len(ed.steps()) == 3
        # 卡片已渲染(StepCard), 且末步自动展开
        cards = [ed.cards_lay.itemAt(i).widget() for i in range(ed.cards_lay.count())]
        cards = [c for c in cards if isinstance(c, mw.StepCard)]
        assert len(cards) == 3, f"应渲染 3 张卡片, 实际 {len(cards)}"
        # 移动/删除(与用例编辑同名同义)
        ed.host.move_step(0, 1)
        assert ed.steps()[1]["desc"] == "点击开始清扫"
        ed.host.del_step(0)
        assert len(ed.steps()) == 2
        # 取值写回
        v = dlg.values()
        assert v["type"] == "steps" and len(v["steps"]) == 2
    finally:
        dlg.close()


def test_steps_editor_supports_else_substeps(qapp):
    """★ 前置条件的步骤编辑也要支持 else 子步骤(与用例编辑一致, 用户要求)"""
    from gui import main_window as mw
    dlg = mw.PreconditionEditDialog({"type": "steps", "enabled": True}, None)
    try:
        ed = dlg._edits["steps_yaml"][0]
        ed.add_step("if")                      # 条件动作
        ed.host.steps[0]["if"] = "充电中"
        ed.host.add_sub(0, "click")            # 添加子步骤
        ed.host.steps[0]["else"][0]["click"] = "确认"
        cards = [ed.cards_lay.itemAt(i).widget() for i in range(ed.cards_lay.count())]
        cards = [c for c in cards if isinstance(c, mw.StepCard)]
        assert len(cards) == 2, "父卡片 + else 子卡片"
        v = dlg.values()
        assert v["steps"][0]["if"] == "充电中"
        assert v["steps"][0]["else"][0]["click"] == "确认", "else 子步骤应保留"
    finally:
        dlg.close()


def test_add_precondition_opens_edit_dialog(qapp, monkeypatch):
    """新增前置条件应弹出编辑框(用户要求), 而不是直接用默认值加进列表"""
    from gui import main_window as mw
    dlg = mw.PreconditionsDialog([{"type": "restart", "enabled": True}], None)
    opened = []

    class _FakeEdit:
        def __init__(self, item=None, parent=None, app_group=""):
            opened.append(item.get("type") if item else None)

        def exec(self):
            return 0            # 取消 → 不应加入列表

    # ★ 消费方是 gui.dialogs 里的 PreconditionsDialog(结构整理 2026-10-15):
    #   patch 必须打在它自己的模块上, 否则真对话框会 exec() 挂住整轮测试
    from gui import dialogs as _dlg
    monkeypatch.setattr(_dlg, "PreconditionEditDialog", _FakeEdit)
    before = len(dlg.items)
    dlg._add_of_type("battery")
    assert opened == ["battery"], "应弹出编辑框且预选该类型"
    assert len(dlg.items) == before, "取消后不应加入列表"


def test_steps_dialog_fits_cards(qapp):
    """★ 步骤编辑对话框要够大: 卡片不能比对话框还宽(用户实测被压/溢出)"""
    from gui import main_window as mw
    dlg = mw.PreconditionEditDialog({"type": "steps", "enabled": True,
                                     "name": "前置准备", "steps": []}, None)
    dlg.show()
    try:
        qapp.processEvents()
        assert dlg.width() >= 700, f"步骤对话框应更大: {dlg.width()}"
        ed = dlg._edits["steps_yaml"][0]
        ed.add_step("click")
        ed.add_step("assert")
        qapp.processEvents()
        cards = [ed.cards_lay.itemAt(i).widget() for i in range(ed.cards_lay.count())]
        cards = [c for c in cards if isinstance(c, mw.StepCard)]
        assert cards, "应渲染卡片"
        assert cards[0].width() <= dlg.width(), \
            f"卡片({cards[0].width()})不应超出对话框({dlg.width()})"
        # 字段表单列要能拉伸(否则步骤区被标签列挤窄)
        from PySide6.QtWidgets import QFormLayout
        assert dlg.form.fieldGrowthPolicy() == QFormLayout.AllNonFixedFieldsGrow
    finally:
        dlg.close()


def test_steps_editor_has_app_group_picker(qapp, monkeypatch, tmp_path):
    """★ 步骤编辑器要有「APP 组」选择 —— 模板/基准图下拉据此取对应组的模板"""
    import gui.main_window as mw
    # 造两个组目录, 各放一张模板
    root = tmp_path / "Test_cases"
    for g, tpl in (("组A", "涂鸦_模板A.png"), ("组B", "涂鸦_模板B.png")):
        tdir = root / g / "templates"
        tdir.mkdir(parents=True)
        (tdir / tpl).write_bytes(b"fake")
    monkeypatch.setattr(mw, "BASE_DIR", str(tmp_path), raising=False)
    # ★ 步骤编辑器(含组下拉)已拆到 gui/step_card.py(结构整理 2026-10-15) ——
    #   patch 必须打在**消费方**模块上, 打在 mw 上不再起作用
    from gui import step_card as _sc
    monkeypatch.setattr(_sc, "BASE_DIR", str(tmp_path), raising=False)
    from core import vision as _vision
    monkeypatch.setattr(_vision, "BASE_DIR", str(tmp_path), raising=False)
    # 前缀从 config 读; 这里直接固定, 否则读不到(测试目录没有 config)
    monkeypatch.setattr(_vision, "_template_prefix", lambda: "涂鸦", raising=False)

    dlg = mw.PreconditionEditDialog({"type": "steps", "enabled": True,
                                     "name": "x", "steps": []}, None,
                                    app_group="组A")
    try:
        ed = dlg._edits["steps_yaml"][0]
        items = [ed.group_combo.itemText(i) for i in range(ed.group_combo.count())]
        assert items == ["组A", "组B"], f"组下拉应列出全部组: {items}"
        assert ed.group_combo.currentText() == "组A"
        from core import vision
        assert "模板A.png" in vision.list_templates(), "应取到组A的模板"
        # 切到组B → 模板上下文跟着换
        ed.group_combo.setCurrentText("组B")
        qapp.processEvents()
        assert "模板B.png" in vision.list_templates(), "切换组后应取组B的模板"
    finally:
        dlg.close()


def test_steps_editor_syncs_context_on_first_open(qapp, monkeypatch, tmp_path):
    """★ 首次打开就要能取到模板(用户实测: 之前要手动切一次 APP 组才有)

    组下拉默认选中第一个组, 且初始化时就把模板上下文设好
    (setCurrentIndex 不触发 currentTextChanged, 必须显式同步)。
    """
    import gui.main_window as mw
    from core import vision
    root = tmp_path / "Test_cases"
    for g in ("组A", "组B"):
        tdir = root / g / "templates"
        tdir.mkdir(parents=True)
        (tdir / f"涂鸦_{g}模板.png").write_bytes(b"fake")
    monkeypatch.setattr(mw, "BASE_DIR", str(tmp_path), raising=False)
    from gui import step_card as _sc2
    monkeypatch.setattr(_sc2, "BASE_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(vision, "BASE_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(vision, "_template_prefix", lambda: "涂鸦", raising=False)
    vision.set_template_app_group("")          # 模拟: 主窗口还没设上下文

    dlg = mw.PreconditionEditDialog({"type": "steps", "enabled": True,
                                     "name": "x", "steps": []}, None,
                                    app_group="")
    try:
        ed = dlg._edits["steps_yaml"][0]
        assert ed.group_combo.currentText() == "组A", "应默认选中第一个组"
        assert vision.current_app_group() == "组A", "初始化就该同步模板上下文"
        assert "组A模板.png" in vision.list_templates(), "首次打开即可取到模板"
    finally:
        dlg.close()


# ── 前置条件: 点击空白处(清掉悬浮通知) ──

def test_tap_blank_uses_default_point_and_override():
    """不填坐标 → 用实测默认空白点; 填了 → 用填的坐标"""
    dev = FakeDev()
    assert session.tap_blank(dev) is True
    assert dev.clicked_at == session.DEFAULT_BLANK_POINT, dev.clicked_at
    dev2 = FakeDev()
    session.tap_blank(dev2, (100, 200))
    assert dev2.clicked_at == (100, 200)


def test_parse_point_accepts_full_and_half_width_comma():
    """坐标分隔符与其它多值字段一致: 中文输入法打出的全角逗号也要认"""
    assert session._parse_point("540,660") == (540, 660)
    assert session._parse_point("540，660") == (540, 660)      # 全角
    assert session._parse_point("540、660") == (540, 660)      # 顿号
    assert session._parse_point(" 540 , 660 ") == (540, 660)
    assert session._parse_point("") is None                    # 空 → 用默认点
    assert session._parse_point("abc") is None
    assert session._parse_point("1,2,3") is None               # 不是两个数 → 默认点
    # 非法坐标不能静默失效: 走默认点
    dev = FakeDev()
    session.tap_blank(dev, session._parse_point("abc"))
    assert dev.clicked_at == session.DEFAULT_BLANK_POINT


def test_tap_blank_runs_as_precondition_item():
    """★ 走前置条件列表执行: type=tap_blank → 点空白; 结果进 prepare_items 列表"""
    dev = FakeDev()
    items = [{"type": "tap_blank", "enabled": True, "point": "300，400"}]
    results = session.prepare_items(dev, {}, items)
    assert dev.clicked_at == (300, 400), dev.clicked_at
    assert len(results) == 1 and results[0]["ok"] is True
    assert "点击空白处" in results[0]["desc"], results[0]["desc"]
    assert "300,400" in results[0]["desc"], results[0]["desc"]


def test_tap_blank_type_is_editable_in_gui():
    """GUI「添加前置条件」里能选到它, 并且坐标可编辑"""
    assert "tap_blank" in session.PRECONDITION_TYPES
    spec = session.PRECONDITION_TYPES["tap_blank"]
    keys = [p["key"] for p in spec["params"]]
    assert "point" in keys, spec


# ── WebView 文本预热 ──

def test_warm_webview_swallows_device_errors():
    """dump 失败(设备抖动)不能拖垮判断"""
    class Boom:
        def dump_hierarchy(self):
            raise RuntimeError("设备连接抖动")
    assert session.warm_webview(Boom()) == ""


def test_map_load_and_text_check_warm_webview(monkeypatch):
    """前置的文本等待(地图加载/文本检查)同样要在轮询里预热 WebView"""
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    dev = FakeDev(present=("地图编辑",))
    calls = {"n": 0}
    # 计数同时要返回"能看到 present"的层级(判断类现在读层级)
    dev.dump_hierarchy = lambda: (
        calls.__setitem__("n", calls["n"] + 1)
        or "".join(f'<node text="{p}"/>' for p in sorted(dev.present)))
    assert session.ensure_map_loaded(dev, timeout=0.01, rounds=1,
                                     ready_text="地图编辑") is True
    assert calls["n"] >= 1, "地图加载没有预热"

    dev2 = FakeDev(present=("首页",))
    calls2 = {"n": 0}
    dev2.dump_hierarchy = lambda: (
        calls2.__setitem__("n", calls2["n"] + 1)
        or "".join(f'<node text="{p}"/>' for p in sorted(dev2.present)))
    assert session.ensure_text_check(dev2, wait_text="首页", timeout=0.01) is True
    assert calls2["n"] >= 1, "文本检查没有预热"


# ── 前置条件: 进入设备页(文本确认 + 失败重进) ──

class _SwitchDev(FakeDev):
    """点设备名后"页面切换": present 从列表页文本变成设备页文本"""

    def __init__(self, before=(), after=()):
        super().__init__(present=before)
        self._after = set(after)

    def __call__(self, **kw):
        r = super().__call__(**kw)
        owner = self
        orig_click = r.click

        def click():
            orig_click()
            owner.present = set(owner._after)      # 模拟进入设备页

        r.click = click
        return r


def test_enter_device_ok_when_already_on_device_page(monkeypatch):
    """已经在设备页(判定文本命中) → 直接通过, 不该乱点/乱按返回"""
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    dev = _SwitchDev(before=("设备控制", "扫地机器人0087"))
    assert session.ensure_device_page(dev, "扫地机器人0087",
                                      ready_text="设备控制", timeout=1) is True
    assert dev.pressed == [] and dev.clicked == [], "已在设备页不该再操作"


def test_enter_device_clicks_name_then_confirms(monkeypatch):
    """不在设备页 → 点设备名进入 → 用判定文本确认"""
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    dev = _SwitchDev(before=("主页", "收藏", "扫地机器人0087"),
                     after=("设备控制", "扫地机器人0087"))
    assert session.ensure_device_page(dev, "扫地机器人0087", ready_text="设备控制",
                                      timeout=1, rounds=3) is True
    assert "扫地机器人0087" in dev.clicked, f"应点击设备名进入: {dev.clicked}"


def test_enter_device_retries_then_fails(monkeypatch):
    """判定文本一直出现不了 → 每轮都重进, 用完轮数返回 False(会阻断本轮)"""
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    # 永远停在列表页(点完也不切换)
    dev = _SwitchDev(before=("主页", "收藏", "扫地机器人0087"), after=("主页",))
    assert session.ensure_device_page(dev, "扫地机器人0087", ready_text="设备控制",
                                      timeout=1, rounds=2) is False
    assert dev.clicked.count("扫地机器人0087") >= 1, "失败后必须再执行进入操作"


def test_enter_device_does_not_click_webview_title(monkeypatch):
    """⚠ 设备页上设备名是铺满全屏的 WebView 标题 —— 判定文本读不到时也不能去点它
    (盲点会点到页面中央的按钮, 如「暂停」)"""
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    dev = _SwitchDev(before=("扫地机器人0087",))        # 既无列表页标志也无设备页标志
    assert session.ensure_device_page(dev, "扫地机器人0087", ready_text="设备控制",
                                      timeout=1, rounds=1) is False
    assert dev.clicked == [], f"不确定在列表页时不能点设备名: {dev.clicked}"


def test_enter_device_ready_text_is_editable_param():
    """判定文本必须可编辑(GUI 前置条件里能改)"""
    spec = session.PRECONDITION_TYPES["enter_device"]
    keys = {p["key"]: p for p in spec["params"]}
    assert "ready_text" in keys, spec
    assert keys["ready_text"]["type"] == "text"
    assert keys["ready_text"]["default"] == session.DEFAULT_DEVICE_READY_TEXT
    # 自定义判定文本要真的生效
    import inspect
    assert "ready_text" in inspect.signature(session.ensure_device_page).parameters


def test_default_preconditions_include_enter_device_first():
    """★ 用户要求: 进设备页要在**默认执行项**里, 且排在 charging/map_load 之前
    (那两项都要在设备页上操作); 判定文本用默认值"""
    types = [it["type"] for it in session.DEFAULT_PRECONDITIONS]
    assert "enter_device" in types, f"默认执行项缺少进设备页: {types}"
    assert types.index("enter_device") < types.index("charging"), types
    assert types.index("enter_device") < types.index("map_load"), types
    item = next(it for it in session.DEFAULT_PRECONDITIONS if it["type"] == "enter_device")
    assert item.get("ready_text") == session.DEFAULT_DEVICE_READY_TEXT, item
    assert item.get("enabled", True) is True, "默认要启用"


# ── 前置条件顺序可手动调整(↑↓) ──

def test_precondition_dialog_reorder_updates_order(qapp):
    """★ 用户要求: 前置条件的执行顺序可手动调整 —— 列表顺序就是执行顺序"""
    from gui import main_window as mw
    items = [{"type": "restart", "enabled": True},
             {"type": "battery", "enabled": True, "min_level": 50},
             {"type": "tap_blank", "enabled": True, "point": "1,2"}]
    dlg = mw.PreconditionsDialog(items, None)
    try:
        assert [it["type"] for it in dlg.values()] == ["restart", "battery", "tap_blank"]
        dlg._move(2, -1)          # 第三条上移
        assert [it["type"] for it in dlg.values()] == ["restart", "tap_blank", "battery"]
        dlg._move(0, 1)           # 第一条下移
        assert [it["type"] for it in dlg.values()] == ["tap_blank", "restart", "battery"]
        # 边界: 首行上移/末行下移 都是 no-op
        dlg._move(0, -1)
        dlg._move(2, 1)
        assert [it["type"] for it in dlg.values()] == ["tap_blank", "restart", "battery"]
    finally:
        dlg.close()


def test_precondition_dialog_arrows_enabled_by_position(qapp):
    """首行的 ↑ 与末行的 ↓ 应置灰(避免点了没反应)"""
    from PySide6.QtWidgets import QPushButton as _PB
    from gui import main_window as mw
    items = [{"type": "restart", "enabled": True},
             {"type": "battery", "enabled": True, "min_level": 50}]
    dlg = mw.PreconditionsDialog(items, None)
    try:
        first = dlg.table.cellWidget(0, 1).findChildren(_PB)
        last = dlg.table.cellWidget(1, 1).findChildren(_PB)
        assert not first[0].isEnabled() and first[1].isEnabled(), "首行↑应置灰"
        assert last[0].isEnabled() and not last[1].isEnabled(), "末行↓应置灰"
    finally:
        dlg.close()


# ── 前置条件与 APP 组绑定 ──

def test_group_preconditions_saved_to_separate_dir(monkeypatch, tmp_path):
    """★ 用户要求: 前置条件按 APP 组存放, 且放**独立目录**(不能落在用例目录里,
    否则会被用例列表当成用例显示出来)"""
    from core import driver
    monkeypatch.setattr(driver, "BASE_DIR", str(tmp_path))
    items = [{"type": "restart", "enabled": True},
             {"type": "enter_device", "enabled": True, "ready_text": "设备控制"}]
    p = driver.save_group_preconditions("三星", items)
    assert p.endswith(os.path.join("Test_preconditions", "三星.yaml")), p
    assert "Test_cases" not in p, f"不能写进用例目录: {p}"
    assert driver.load_group_preconditions("三星") == items
    assert driver.load_group_preconditions("没配过的组") is None


def test_preconditions_file_never_counts_as_case():
    """兜底: 组目录里若残留 preconditions.yaml, 不能被当作用例扫出来"""
    from core.driver import is_case_file
    assert is_case_file("/x/Test_cases/三星/全局清扫.yaml") is True
    assert is_case_file("/x/Test_cases/三星/preconditions.yaml") is False
    assert is_case_file("/x/Test_cases/三星/preconditions.yml") is False


def test_preconditions_for_group_fallback_chain(monkeypatch, tmp_path):
    """取值优先级: 组配置 → config.yaml 全局 → 内置默认"""
    from core import driver
    monkeypatch.setattr(driver, "BASE_DIR", str(tmp_path))   # ★ 组配置路径基于 driver.BASE_DIR
    # ① 组里没配、全局也没配 → 内置默认项(注意要连全局一起隔离, 别读真实 config)
    monkeypatch.setattr(driver, "load_preconditions", lambda: None)
    items, src = session.preconditions_for_group("组A")
    assert src == "default" and items, (src, items)
    # ② 只有全局 → 用全局(老用户参数不丢)
    global_items = [{"type": "charging", "enabled": True, "timeout": 1200}]
    monkeypatch.setattr(driver, "load_preconditions", lambda: global_items)
    items, src = session.preconditions_for_group("组A")
    assert src == "global" and items == global_items
    # ③ 组里有配置 → 优先用组里的
    group_items = [{"type": "tap_blank", "enabled": True, "point": "9,9"}]
    driver.save_group_preconditions("组A", group_items)
    items, src = session.preconditions_for_group("组A")
    assert src == "group" and items == group_items, (src, items)


def test_group_preconditions_path_is_isolated_in_tests():
    """守护: 测试里组配置的落盘路径必须指向临时目录。

    conftest 的 `_isolate_group_preconditions` 钉住 group_preconditions_path 这个
    唯一出口 —— 一旦那条 fixture 被删/失效, 测试就会写进用户真实的
    Test_preconditions/(曾经因此覆盖过用户的 7 条配置), 这里当场报红。
    """
    from pathlib import Path as _P

    from core.driver import group_preconditions_path
    p = _P(group_preconditions_path("任意组")).resolve()
    real = (_P(__file__).resolve().parent.parent / "Test_preconditions" / "任意组.yaml")
    assert p != real.resolve(), "测试里指向了真实目录: conftest 的隔离失效了"
    assert "Test_preconditions" in p.name or p.name.endswith(".yaml")


def test_precondition_order_arrows_are_visible(qapp):
    """★ 箭头按钮必须真的放得下箭头。

    用户报过"顺序按钮没有箭头方向显示": 全局 QSS 给 QPushButton 设了
    `padding: 4px 12px`, 而箭头按钮只有 26px 宽 —— 左右内边距吃掉 24px,
    字被整个截掉。必须内联覆盖 padding + 固定尺寸。
    """
    from PySide6.QtWidgets import QPushButton as _PB
    from gui import main_window as mw
    dlg = mw.PreconditionsDialog([{"type": "restart", "enabled": True},
                                  {"type": "battery", "enabled": True}], None)
    try:
        for b in dlg.table.cellWidget(0, 1).findChildren(_PB):
            assert b.text() in ("↑", "↓"), b.text()
            need = b.fontMetrics().horizontalAdvance(b.text()) + 4
            assert b.width() >= need, \
                f"'{b.text()}' 按钮太窄({b.width()} < {need}), 箭头会被全局 padding 截掉"
            assert "padding" in b.styleSheet(), "小按钮必须内联覆盖全局 padding"
    finally:
        dlg.close()


# ── 2026-09-24 审查修复: 按组取前置 / 组前置写入安全 ──

def test_resolve_group_preconditions_empty_list_does_not_fall_back():
    """★ 某组前置「全部取消勾选」(空列表) 不得回退去执行别的组的前置。

    回归守护: 原实现是 `pre_by_group.get(group) or self.pre_items` —— 空列表 falsy,
    于是把「这组有意不做前置」当成「没配过」, 实际跑了当前界面组的前置。
    实测: ZZZ 组界面 0 条, 实际执行 1 条(可能是重启 APP / 等充电这类重动作)。
    """
    from gui.runner_thread import resolve_group_preconditions
    fallback = [{"type": "restart", "enabled": True}]
    by_group = {"AAA": [{"type": "charging", "enabled": True}], "ZZZ": []}

    assert resolve_group_preconditions(by_group, "ZZZ", fallback) == [], \
        "空列表被当成'没配过', 回退到了别组的前置"
    assert resolve_group_preconditions(by_group, "AAA", fallback) == by_group["AAA"]
    # 真的没配过的组才用 fallback
    assert resolve_group_preconditions(by_group, "未配过的组", fallback) == fallback
    # 旧调用方传空/None 也要兜住
    assert resolve_group_preconditions({}, "任意组", fallback) == fallback
    assert resolve_group_preconditions(None, "任意组", fallback) == fallback


def test_group_preconditions_save_failure_keeps_file():
    """★ 序列化失败时原组配置必须完好(不能只剩 0 字节)。

    回归守护: 原实现把 `yaml.safe_dump` 写在 `open(path, "w")` **之后** —— items 里
    混入 YAML 无法表示的对象时 safe_dump 抛 RepresenterError, 而此刻文件已被截断
    (实测 129 字节 → 0 字节); 异常抛出后用户并不知道内容已经没了。
    """
    from core import driver
    good = [{"type": "charging", "enabled": True, "ready_text": "充电"}]
    p = driver.save_group_preconditions("组安全", good)
    before = open(p, encoding="utf-8").read()
    assert before

    with pytest.raises(Exception):
        driver.save_group_preconditions("组安全", good + [{"type": "x", "obj": object()}])

    assert open(p, encoding="utf-8").read() == before, "序列化失败把原配置清空了"
    assert driver.load_group_preconditions("组安全") == good


def test_steps_precondition_gets_device_name_and_cancel():
    """★ 「自定义步骤」前置新建的 runner 必须拿到 device_name 与取消回调。

    回归守护:
      · 漏传 device_name → "插件页整页读不出文本就重进设备页"用不了, 同一条断言
        在前置里失败、在正式用例里却能过(实测拿到的是空串)。
      · 没有取消回调 → 用户点停止后这串前置会一路跑完(实测 4s 的等待步骤跑满 4.0s,
        因为新建的 runner 拿不到主 runner 的 stop())。
    """
    from core import registry as reg
    import core.actions        # noqa: F401

    seen = {}
    cancel = {"v": False}
    try:
        @reg.action("probe_pre_ctx", priority=1)
        def _probe(runner, step):
            seen["device_name"] = runner.device_name
            cancel["v"] = True          # 第 1 步之后请求停止

        @reg.action("probe_pre_after", priority=1)
        def _after(runner, step):
            seen["second_ran"] = True   # 这步不该被执行

        class _D:
            serial = "d"

            @property
            def info(self):
                return {}

            def dump_hierarchy(self):
                return '<hierarchy><node text="x"/></hierarchy>'

            def click(self, *a, **k):
                pass

        cfg = {"target_device": "我的扫地机",
               "runner": {"step_interval": 0, "default_timeout": 1}}
        session._run_one(_D(), cfg,
                         {"type": "steps", "name": "探针",
                          "steps": [{"desc": "查上下文", "probe_pre_ctx": True},
                                    {"desc": "停止后不该执行", "probe_pre_after": True}]},
                         None, lambda: cancel["v"])
    finally:
        reg.ACTIONS.pop("probe_pre_ctx", None)
        reg.ACTIONS.pop("probe_pre_after", None)

    assert seen.get("device_name") == "我的扫地机", \
        "steps 前置的 runner 漏传 device_name(与正式用例行为不一致)"
    assert "second_ran" not in seen, \
        "取消信号没穿透到 steps 前置(点停止后它还会跑完剩余步骤)"


def test_page_sample_falls_back_to_ocr_when_tree_is_sparse(monkeypatch):
    """★ 插件页/WebView 的无障碍树常常只有一两条文本 —— 只读树的话日志会写出
    "当前页面文本: []", 等于没留证据。树里太少时补一次截图识别(OCR 兜底本来就在
    文本判定链路上, 见 text_present 的 sparse 处理)。"""
    from core import session as _s, ocr
    seen = {}

    class D:
        def dump_hierarchy(self):
            return '<node text="正在清洁"/>'

    monkeypatch.setattr(ocr, "screen_texts",
                        lambda d, use_cache=True, exclude=None:
                        (seen.setdefault("exclude", exclude), [("干燥拖布中", (1, 2)),
                                                               ("电量 45%", (3, 4))])[1])
    got = _s.page_sample(D())
    assert "正在清洁" in got and "干燥拖布中" in got, got
    assert "exclude" in seen, "OCR 取样要带上系统 UI 屏蔽区域(状态栏不该被当成页面内容)"


def test_battery_wait_logs_only_when_level_changes(monkeypatch, caplog):
    """★ 用户 2026-10-16: "电量等待的打印太频繁了, 要按电量变化才打印, 不要按等待时间"
    + "但是等待时间也要同步打印出来"。

    判据两条: ① 电量不变的轮次**不打印**; ② 每次打印的那一行**带着已等秒数**。
    """
    from core import session as _s
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    monkeypatch.setattr(session, "get_battery_level",
                        lambda d: 40)          # 一直 40% → 除首行外不该再有行
    with caplog.at_level("INFO"):
        session.ensure_battery(FakeDev(), min_level=50, timeout=0.6, interval=1)
    lines = [r.getMessage() for r in caplog.records if "等待电量达标" in r.getMessage()]
    assert len(lines) == 1, f"电量没变不该反复打印: {lines}"
    assert "已等" in lines[0] and "40%" in lines[0], lines[0]

    caplog.clear()
    seq = iter([40, 40, 40, 39, 39, 38])       # 只在 39 / 38 变化时打印
    monkeypatch.setattr(session, "get_battery_level", lambda d: next(seq, 38))
    with caplog.at_level("INFO"):
        session.ensure_battery(FakeDev(), min_level=50, timeout=0.6, interval=1)
    lines = [r.getMessage() for r in caplog.records if "等待电量达标" in r.getMessage()]
    assert [l.split("电量 ")[-1].rstrip("%") for l in lines] == ["40", "39", "38"], lines
    assert all("已等" in l for l in lines), lines


def test_charging_wait_logs_only_when_level_changes(monkeypatch, caplog):
    """★ 等待充电同理(它的轮询已缩到 5s, 按时间打点会 12 行/分钟)"""
    monkeypatch.setattr(session, "is_charging", lambda d, text=None: False)
    monkeypatch.setattr(session, "click_template", lambda d, n, **kw: (1, 2))
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    monkeypatch.setattr(session, "get_battery_level", lambda d: 55)     # 一直不变
    with caplog.at_level("INFO"):
        session.ensure_charging(FakeDev(), timeout=0.6, interval=5)
    lines = [r.getMessage() for r in caplog.records if "等待充电中" in r.getMessage()]
    assert len(lines) == 1, f"电量没变不该反复打印: {lines}"
    assert "已等" in lines[0] and "55%" in lines[0], lines[0]


# ── 未命中留痕(用户 2026-10-16: "前置检测的测试项中还有哪些没加命中和未命中信息") ──

def _miss_lines(caplog, key):
    return [r.getMessage() for r in caplog.records if key in r.getMessage()]


def test_map_load_failure_logs_searched_texts_and_page_sample(monkeypatch, caplog):
    """地图加载失败: 要说清"就绪/加载中文本都未出现" + 当时页面上有什么"""
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    monkeypatch.setattr(session, "warm_webview", lambda d: '<node text="设置页"/>')
    monkeypatch.setattr(session, "text_present_detail",
                        lambda d, texts, xml=None, round_no=None: (None, "", "", None))
    with caplog.at_level("INFO"):
        session.ensure_map_loaded(FakeDev(), timeout=0, rounds=1,
                                  ready_text="地图编辑", loading_text="地图正在加载")
    line = _miss_lines(caplog, "仍未就绪")
    assert line and "就绪文本「地图编辑」" in line[0] and "当前页面文本" in line[0] \
        and "设置页" in line[0], line


def test_enter_device_failure_logs_searched_texts_and_page_sample(monkeypatch, caplog):
    """进入设备页失败: 确认文本都未出现 + 当时页面上有什么"""
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    monkeypatch.setattr(session, "_wait_texts", lambda d, texts, timeout, cancel=None: None)
    monkeypatch.setattr(session, "_enter_device_page", lambda d, name, cancel=None: False)
    monkeypatch.setattr(session, "warm_webview", lambda d: '<node text="设备列表"/>')
    with caplog.at_level("INFO"):
        session.ensure_device_page(FakeDev(), device_name="机器人", ready_text="设备控制",
                                   timeout=0, rounds=1)
    line = _miss_lines(caplog, "仍未确认进入设备页")
    assert line and "确认文本「设备控制」" in line[0] and "设备列表" in line[0], line


def test_text_check_timeout_logs_searched_texts_and_page_sample(monkeypatch, caplog):
    """文本检查超时: 说清查了什么(等待出现/消失) + 当时页面上有什么"""
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    monkeypatch.setattr(session, "warm_webview", lambda d: '<node text="首页"/>')
    with caplog.at_level("INFO"):
        session.ensure_text_check(FakeDev(), wait_text="清洁中,正在吸尘",
                                  absent_text="正在加载", timeout=0, name="清扫态")
    line = _miss_lines(caplog, "内未满足条件")
    assert line and "等待出现「清洁中、正在吸尘」" in line[0] \
        and "等待消失「正在加载」" in line[0] and "首页" in line[0], line
