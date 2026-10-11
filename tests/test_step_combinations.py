# -*- coding: utf-8 -*-
"""步骤「组合」与「交互」的守护。

★ 起因(2026-10-11 用户要求"检查测试步骤各种组合执行和交互是否有问题"):
  实测两条真问题 ——
    ① 一步里写**两个主动作**时, 引擎只跑优先级最高的那个, 另一个**静默丢掉**
       (25 个主动作两两组合 300 对, 全部如此; 真机上的表现是"设好时间却没设、
       直接去断言"——set_time 被 assert 顶掉)。GUI 造不出这种步骤(不能改动作
       类型), 但手写/导入的 YAML 可以。
    ② 卡片按 dict 键序挑动作、引擎按优先级挑 —— `{assert: .., click: ..}` 会
       **显示"断言"却执行"点击"**。
  这里把两条钉住, 并扫一遍仓库里所有用例/前置, 保证没有这种步骤存在。

  另外把用户问的"点击的按钮/模板/坐标是否都能生效"落成执行层守护:
  四种形态(文本 / 坐标 / 模板图 / 下拉选的模板)都必须真的点到目标。
"""
import glob
import os
import sys
from pathlib import Path

import numpy as np
import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core.actions                                              # noqa: E402,F401
from core import registry as reg                                 # noqa: E402
from core import vision                                          # noqa: E402
from core.runner import ActionRunner                             # noqa: E402
from gui import schema as S                                      # noqa: E402


# ── 桩 ──────────────────────────────────────────────────────────────

class _El:
    def __init__(self, clickable=True):
        self.clicked = False
        self._clickable = clickable

    def exists(self, timeout=1):
        return True

    def click(self):
        self.clicked = True

    @property
    def info(self):
        return {"clickable": self._clickable, "bounds": {"left": 0, "top": 0,
                                                         "right": 10, "bottom": 10}}


class ClickDevice:
    """记录"点在哪"的设备桩: 分得清 文本点击 / 坐标点击 / 模板匹配点击"""
    serial = "fake"

    def __init__(self):
        self.clicked_at = None          # 坐标/模板点击落点
        self.last_kw = {}               # 按文本查元素时的参数
        self.element = _El()
        self.frame = np.zeros((1920, 1080, 3), dtype=np.uint8)

    def __call__(self, **kw):
        self.last_kw = kw
        return self.element

    def screenshot(self, path=None, format=None):
        return self.frame

    def click(self, x, y):
        self.clicked_at = (x, y)

    def dump_hierarchy(self):
        return ""

    def window_size(self):
        return (1080, 1920)


def _runner(device):
    return ActionRunner(device, {"step_interval": 0, "default_timeout": 1,
                                 "click_timeout": 1}, case_name="t")


class Recorder:
    """把动作执行器换成记录器 —— 只验分发, 不产生任何副作用。

    ★ 必须**两条路都拦**: 主动作走注册表(reg.ACTIONS[key].fn);
      grab/match/wait_for 这三个共存键是 `_execute` 里**直接 import 模块函数**调用的
      (from core.actions.xxx import do_xxx), 只换注册表拦不住 —— 实测漏拦会让
      wait_for 真跑一遍 15s 超时(2026-07-11 写守护时当场踩到)。
    """

    def __init__(self):
        self.called = []

    def install_all(self, monkeypatch):
        rec = lambda key: (lambda *a, **k: self.called.append(key))   # noqa: E731
        for k in reg.ACTIONS:
            monkeypatch.setattr(reg.ACTIONS[k], "fn", rec(k))
        from core.actions import basic as _basic, data_ops as _data
        monkeypatch.setattr(_basic, "do_wait_for", rec("wait_for"))
        monkeypatch.setattr(_data, "do_grab", rec("grab"))
        monkeypatch.setattr(_data, "do_match", rec("match"))


# ── ① 多个主动作: 明确报错, 不得静默丢一个 ──────────────────────────

@pytest.mark.parametrize("step", [
    {"click": "确认", "assert": "清扫中"},
    {"assert": "清扫中", "click": "确认"},          # 键序反过来也一样
    {"set_time": 3, "assert": "清扫中"},            # 真机最危险的一种: 时间没设就断言
    {"back": True, "click": "确认"},
    {"click": "确认", "wait_loading": True},
])
def test_multiple_main_actions_raise_instead_of_dropping_one(monkeypatch, step):
    rec = Recorder()
    rec.install_all(monkeypatch)
    d = ClickDevice()
    r = _runner(d)
    with pytest.raises(ValueError, match="多个动作") as e:
        r._execute(dict(step))
    assert rec.called == [], f"报错前不该执行任何动作, 实际执行了 {rec.called}"
    # 报错必须**点名**这几个动作, 用户才知道要拆哪一步
    for k in reg.main_action_keys(step):
        assert k in str(e.value), f"报错没点名 {k}: {e.value}"


def test_coexisting_keys_are_not_main_actions(monkeypatch):
    """grab/match/screenshot/wait_for 与主动作共存是**设计**, 不能算"多个动作"报错"""
    rec = Recorder()
    rec.install_all(monkeypatch)
    r = _runner(ClickDevice())
    r._sleep = lambda s: None
    r._execute({"click": "确认", "wait_for": "清扫中"})
    assert rec.called == ["click", "wait_for"], rec.called        # 等待条件照样执行
    rec.called.clear()
    r._execute({"assert": "清扫中", "screenshot": True})          # 截图与断言共存
    assert rec.called == ["assert"], rec.called
    rec.called.clear()
    r._execute({"click_template": "进入设置.png", "grab": "面积"})
    assert rec.called == ["click_template", "grab"], rec.called   # 抓取与点击共存


def test_grab_and_match_in_one_step_raise(monkeypatch):
    """抓取与比对也不能写同一步(if/elif, 否则比对被静默吞掉)"""
    rec = Recorder()
    rec.install_all(monkeypatch)
    r = _runner(ClickDevice())
    with pytest.raises(ValueError, match="抓取"):
        r._execute({"grab": "面积", "match": "面积"})
    assert rec.called == [], rec.called
    # 各自单独成步照旧
    r._execute({"grab": "面积"})
    r._execute({"match": "面积"})
    assert rec.called == ["grab", "match"], rec.called


def test_wait_for_alone_still_runs(monkeypatch):
    """整个步骤只有 wait_for 时, 它自己就是主动作(不能被当成"共存键"跳过)"""
    rec = Recorder()
    rec.install_all(monkeypatch)
    r = _runner(ClickDevice())
    r._execute({"wait_for": "清扫中"})
    assert rec.called == ["wait_for"], rec.called


# ── ② 卡片显示的动作 == 实际执行的动作 ──────────────────────────────

@pytest.mark.parametrize("step", [
    {"assert": "清扫中", "click": "确认"},      # 曾经: 卡片显示断言、实际执行点击
    {"click": "确认", "assert": "清扫中"},
    {"set_time": 3, "assert": "清扫中"},
    {"click": "确认", "wait_for": "清扫中"},
    {"grab": "面积"},                            # 只有并存键: 卡片仍要认得出来
    {"match": "面积"},
])
def test_card_summary_and_engine_pick_the_same_action(step):
    from gui import main_window as mw
    engine = reg.primary_action_key(step)
    shown = S.primary_action_key(step)
    if engine:
        assert shown == engine, f"卡片挑的 {shown} != 引擎执行的 {engine}"
    # 卡片 chip 与摘要也走同一判据
    card = mw._StepsHost._find_action(step)
    desc = mw.MainWindow._find_action(object(), step)      # 方法体只用 schema, 不需真窗口
    assert card is desc
    assert card is (S.ACTION_BY_KEY.get(shown) if shown else None)
    # ★ 摘要里必须出现"会被执行的那个动作"的参数 —— 只看"不是原始 dict"抓不到回归:
    #   把 step_summary 退回按键序取动作时, 它会显示**另一个**动作的值(实测盲点)
    summary = S.step_summary(step)
    assert not summary.strip().startswith("{"), summary
    val = step.get(shown) if shown else None
    if isinstance(val, str) and val:
        assert val in summary, f"摘要显示的该是 {shown} 的参数 {val!r}, 实际: {summary!r}"


# ── ③ 仓库里的用例/前置: 不许出现这种步骤 ───────────────────────────

def _iter_steps(doc):
    """把用例文件里所有步骤(含 if 的 else 子步骤、前置里的自定义步骤)摊开"""
    def walk(steps, where):
        for s in steps or []:
            if not isinstance(s, dict):
                continue
            yield where, s
            yield from walk(s.get("else"), where)

    for case in (doc.get("cases") or []):
        if isinstance(case, dict):
            yield from walk(case.get("steps"), case.get("name") or "?")
    for pre in (doc.get("preconditions") or []):
        if isinstance(pre, dict) and pre.get("type") == "steps":
            yield from walk(pre.get("steps"), "前置:" + str(pre.get("name") or "?"))


def test_repo_cases_have_no_multi_action_step():
    """★ 仓库自带用例/前置必须干净: 有多个主动作的步骤一跑就报错"""
    files = (glob.glob(str(ROOT / "Test_cases" / "**" / "*.yaml"), recursive=True)
             + glob.glob(str(ROOT / "Test_preconditions" / "*.yaml")))
    assert files, "没扫到任何用例文件(路径变了?)"
    bad = []
    for path in files:
        try:
            doc = yaml.safe_load(open(path, encoding="utf-8")) or {}
        except Exception as e:
            bad.append((os.path.relpath(path, ROOT), f"YAML 读不了: {e}"))
            continue
        for where, step in _iter_steps(doc):
            mains = reg.main_action_keys(step)
            if len(mains) > 1:
                bad.append((os.path.relpath(path, ROOT), where, mains))
    assert not bad, f"这些步骤里有多个主动作(一跑就报错): {bad}"


# ── ④ 点击的四种形态都要真的点下去 ─────────────────────────────────

def test_click_by_text_targets_the_element():
    """① 按钮名: 按 textContains 找元素并点它"""
    d = ClickDevice()
    r = _runner(d)
    r._execute({"click": "开始清扫"})
    assert d.last_kw.get("textContains") == "开始清扫"
    assert d.element.clicked is True
    assert d.clicked_at is None          # 不是坐标点击


@pytest.mark.parametrize("value", ["531,1900", "531，1900", "531 1900", [531, 1900]])
def test_click_by_coordinates_targets_that_point(value):
    """② 坐标: 界面文本框里写的字符串与 YAML 列表写法都要认"""
    d = ClickDevice()
    _runner(d)._execute({"click": value})
    assert d.clicked_at == (531, 1900), d.clicked_at
    assert d.element.clicked is False    # 不该再去找文本


@pytest.mark.parametrize("step", [
    {"click": "开始清扫.png"},            # ③ click 直接写图片名
    {"click_template": "开始清扫.png"},   # ④ 下拉选模板的动作
])
def test_template_click_clicks_the_matched_point(monkeypatch, step):
    """模板点击: 匹配到就点在匹配位置(两条入口走同一套查找, 结果必须一致)"""
    d = ClickDevice()
    monkeypatch.setattr(vision, "find_in_gray", lambda screen, tpl: (300, 400))
    monkeypatch.setattr(vision, "resolve_template", lambda name: name)
    r = _runner(d)
    r._execute(dict(step))
    assert d.clicked_at == (300, 400), f"没点在匹配位置: {d.clicked_at}"
    assert r.last_click and r.last_click[2] == "开始清扫.png"


def test_template_click_reports_when_nothing_matched(monkeypatch):
    """模板匹配不到必须**报错**, 不能静默跳过(否则后面的断言会以错状态为基准)"""
    d = ClickDevice()
    monkeypatch.setattr(vision, "find_in_gray", lambda screen, tpl: None)
    monkeypatch.setattr(vision, "resolve_template", lambda name: name)
    r = _runner(d)
    r.click_timeout = 0                   # 不轮询, 立即判定
    with pytest.raises(AssertionError, match="未匹配到模板图"):
        r._execute({"click": "不存在的按钮.png"})
    assert d.clicked_at is None
