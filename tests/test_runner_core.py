# -*- coding: utf-8 -*-
"""ActionRunner 主循环: 分发/重试/停止/失败处理,全用 FakeDevice,不碰真机。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import registry as reg
from core.runner import ActionRunner, UserStopped  # noqa: F401


class FakeDevice:
    def __init__(self, fail_first=0):
        self.serial = "fake-001"
        self.calls = []
        self._fail_first = fail_first

    def info(self):
        if self._fail_first > 0:
            self._fail_first -= 1
            raise RuntimeError("device offline")
        return {}


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    """retry 间隔是真实的 2s —— 换成"立即返回但仍响应停止标志"的 _sleep。

    不能 patch time.sleep: _sleep 靠它推进时间,替换后会死循环。
    也不能简单换成 no-op: test_stop_raises_and_marks 依赖 _sleep 在
    stopped 置位时抛 UserStopped。
    """
    def fast_sleep(self, seconds):
        if self.stopped:
            raise UserStopped("用户手动停止")
    monkeypatch.setattr(ActionRunner, "_sleep", fast_sleep)


@pytest.fixture(autouse=True)
def _clean_registry():
    backup = dict(reg.ACTIONS)
    reg.ACTIONS.clear()
    yield
    reg.ACTIONS.clear()
    reg.ACTIONS.update(backup)


@pytest.fixture
def runner():
    cfg = {"step_interval": 0, "default_timeout": 1, "click_timeout": 1}
    return ActionRunner(FakeDevice(), cfg, case_wait=None, case_name="测试用例")


def test_dispatch_by_priority(runner):
    hit = []

    @reg.action("fake_hi", priority=10)
    def _hi(r, s): hit.append("hi")

    @reg.action("fake_lo", priority=99)
    def _lo(r, s): hit.append("lo")

    runner._execute({"fake_hi": True, "fake_lo": True})
    assert hit == ["hi"]        # priority 小的先命中,屏蔽后面


def test_grab_coexists_with_action(runner, monkeypatch):
    """grab 与主动作共存于一步: 主表跑 click,grab 由 _execute 单独调用(data_ops)"""
    hit = []
    grabbed = []

    @reg.action("fake_click", priority=10)
    def _c(r, s): hit.append(1)

    import core.actions.data_ops as data_ops
    monkeypatch.setattr(data_ops, "do_grab", lambda r, kw: grabbed.append(kw))

    runner._execute({"fake_click": "x", "grab": "面积"})
    assert hit == [1] and grabbed == ["面积"]


def test_retry_then_success(runner):
    calls = {"n": 0}

    @reg.action("flaky", priority=10)
    def _f(r, s):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("boom")

    ok = runner.run_steps([{"flaky": True, "retry": 3}])
    assert ok is True and calls["n"] == 3


def test_final_failure_returns_false(runner):
    @reg.action("always_fail", priority=10)
    def _f(r, s):
        raise AssertionError("不行")

    ok = runner.run_steps([{"always_fail": True}])
    assert ok is False
    assert runner.results[-1]["passed"] is False
    assert "不行" in runner.results[-1]["error"]


def test_stop_raises_and_marks(runner):
    @reg.action("slow", priority=10)
    def _f(r, s):
        runner.stopped = True
        r._sleep(5)      # 内部轮询到 stopped 抛 UserStopped

    ok = runner.run_steps([{"slow": True}])
    assert ok is False
    assert runner.results[-1]["error"] == "用户手动停止执行"


def test_wait_for_coexists_with_click(runner, monkeypatch):
    """主分发只执行优先级最高的一个动作,wait_for 必须在之后单独补执行。

    用例里 "click: 安静 + wait_for: 安静,标准,强力" 是很自然的写法;
    如果 wait_for 被静默丢掉,用户以为在等,实际没等。
    """
    clicked = []
    waited = []

    @reg.action("fake_click2", priority=5)
    def _c(r, s): clicked.append(1)

    import core.actions.basic as basic
    monkeypatch.setattr(basic, "do_wait_for", lambda r, s: waited.append(s["wait_for"]))

    runner._execute({"fake_click2": "x", "wait_for": "安静,标准"})
    assert clicked == [1]
    assert waited == ["安静,标准"]


def test_wait_for_alone_runs_exactly_once(runner, monkeypatch):
    """整个步骤只有 wait_for 时由主分发命中,不能被补执行逻辑再跑一遍"""
    calls = []
    import core.actions.basic as basic
    monkeypatch.setattr(basic, "do_wait_for", lambda r, s: calls.append(1))

    @reg.action("wait_for", priority=110)
    def _wf(r, s):
        basic.do_wait_for(r, s)

    runner._execute({"wait_for": "清扫中"})
    assert len(calls) == 1


def test_resolve_locator_ref(runner):
    runner._locators = {"预约清扫": {"添加按钮": "添加预约.png"}}
    step = runner._resolve_step({"click": "${预约清扫.添加按钮}"})
    assert step["click"] == "添加预约.png"


# ── 2026-09-24 审查修复: if + retry 不得留下"幽灵 FAIL 行" ──

def test_if_retry_success_leaves_no_ghost_failure():
    """★ if 步骤 retry 后成功, 上一次走 else 留下的失败子行不得混进结果表。

    回归守护: `do_if_impl` 只在**失败分支**重置 `_pending_sub_results`; 重试成功时
    直接 return, 上一次的失败子行仍在 → `_execute` 把它当"本次结果"追加进结果表。
    后果不只是显示: report.add_result(module, False, ...) 让该用例 failed≥1,
    Excel 汇总于是把这条**整体通过**的用例标成 FAIL。
    """
    from core import registry as reg
    from core.actions.basic import do_if
    # ★ 必须显式注册: 本文件的 _clean_registry fixture 会把 ACTIONS 清空, 而
    #   `import core.actions` 是幂等的(不会重新执行注册) —— 不补这一步, `if` 动作
    #   根本没注册, _execute 找不到命中键就静默不做任何事, 测试会"假通过"。
    reg.action("if", priority=30)(do_if)
    without = '<hierarchy><node text="别的文本"/></hierarchy>'
    with_ = ('<hierarchy><node text="别的文本"/>'
             '<node text="目标文本"/></hierarchy>')
    state = {"went_else": False}

    class Dev:
        serial = "d"

        @property
        def info(self):
            return {}

        def dump_hierarchy(self):
            # 第一次尝试(含其内部轮询)全程不满足条件 → 走 else; 之后满足 → 重试成功
            return with_ if state["went_else"] else without

        def screenshot(self, *a, **k):
            return b""

        def __call__(self, **kw):
            class _E:
                def exists(self, timeout=None):
                    return False

                @property
                def info(self):
                    return {"bounds": {"left": 0, "top": 0, "right": 1, "bottom": 1},
                            "clickable": True, "text": ""}

                def click(self, *a, **k):
                    pass
            return _E()

    @reg.action("probe_else_step", priority=1)
    def _else_step(runner_, step_):
        state["went_else"] = True
        raise AssertionError("else 步骤故意失败")

    cfg = {"step_interval": 0, "default_timeout": 1, "click_timeout": 1}
    r = ActionRunner(Dev(), cfg, case_wait=None, case_name="幽灵行")
    r._sleep = lambda s: None
    r.interval = 0
    step = {"desc": "条件步骤", "if": "目标文本", "timeout": 1, "retry": 1,
            "else": [{"desc": "else分支步骤", "probe_else_step": True}]}

    ok = r.run_steps([step])
    assert ok is True, f"该用例整体应通过: {r.results}"
    ghosts = [x for x in r.results if not x["passed"]]
    assert not ghosts, f"整体通过却留下 FAIL 行(会让 Excel 把该用例标 FAIL): {ghosts}"
