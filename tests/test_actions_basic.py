# -*- coding: utf-8 -*-
"""basic 动作: click 坐标/文本/优先级, swipe 映射, if_click, back。FakeDevice 无设备。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import core.actions.basic  # noqa: F401 触发注册
from core import registry as reg
from core.runner import ActionRunner


class FakeElement:
    def __init__(self, exists=True, bounds=None):
        self._exists = exists
        self.bounds = bounds or {"left": 0, "top": 0, "right": 100, "bottom": 50}
        self.clicked = False

    def exists(self, timeout=1):
        return self._exists

    def click(self):
        self.clicked = True

    @property
    def info(self):
        return {"bounds": self.bounds}


class FakeDevice:
    serial = "fake"

    def __init__(self, present=("确认",)):
        self.present = set(present)
        self.last_kw = {}
        self.clicked_at = None
        self.pressed = None
        self.swiped = None

    def info(self):
        return {}

    def dump_hierarchy(self):
        # 判断类(if 条件/wait_for/wait_loading)现在读层级: 桩把 present 体现在 XML 里
        return "".join(f'<node text="{p}"/>' for p in sorted(self.present))

    def window_size(self):
        return (1080, 1920)

    def click(self, x, y):
        self.clicked_at = (x, y)

    def press(self, key):
        self.pressed = key

    def swipe(self, sx, sy, ex, ey, duration=None):
        self.swiped = (sx, sy, ex, ey)

    def __call__(self, **kw):
        self.last_kw = kw
        # textContains/text/description 命中 present 集合
        for k in ("text", "textContains", "description"):
            if k in kw:
                return FakeElement(exists=kw[k] in self.present)
        return FakeElement(exists=False)


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    """swipe 后会 runner._sleep(1)。

    只换 _sleep 方法本身,**不能**去 patch time.sleep —— _sleep 靠
    time.sleep 推进时间,替换成空操作后 remain 永不归零会死循环。
    """
    monkeypatch.setattr(ActionRunner, "_sleep", lambda self, s: None)


@pytest.fixture
def runner():
    return ActionRunner(FakeDevice(), {"step_interval": 0, "default_timeout": 1,
                                       "click_timeout": 1}, case_name="t")


# ── 故障/空闲哨兵(用户 2026-10-13): 断言轮询的原生路径 ──

def test_assert_text_fault_state_fails_fast(runner):
    """断言撞上故障词 → 直接 FaultStateError 带**整段原文**, 不等满超时"""
    from core import session as _session
    runner.d = FakeDevice(present=("滚刷被困, 请清理后重试",))
    with pytest.raises(_session.FaultStateError) as ei:
        runner._assert_locator("充电中", timeout=5)
    assert "滚刷被困, 请清理后重试" in str(ei.value), str(ei.value)


def test_assert_text_idle_state_fails_fast(runner):
    """设备待机/空闲/等待中 → 立即断言失败(目标不会再出现), 不傻等"""
    from core import session as _session
    runner.d = FakeDevice(present=("设备等待中",))
    with pytest.raises(AssertionError) as ei:
        runner._assert_locator("充电中", timeout=5)
    msg = str(ei.value)
    assert "空闲" in msg and "设备等待中" in msg, msg


class _SeqDev(FakeDevice):
    """记录操作顺序的桩: 验证"点击证据要在点击前拍"(用户 2026-10-13)"""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.ops = []

    def click(self, x, y):
        self.ops.append(("click", x, y))

    def screenshot(self, path=None, format=None):
        self.ops.append(("shot", path))
        from pathlib import Path as _P
        p = _P(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"png")


def test_click_screenshots_before_clicking(runner):
    """★ 点击证据要**点击前**拍: 点完再截页面已变, 标注的坐标对不上那一刻的界面。"""
    runner.d = _SeqDev()
    res = runner._execute({"desc": "点一下", "click": [10, 20]})
    assert runner.d.ops[0][0] == "shot", runner.d.ops
    assert runner.d.ops[1] == ("click", 10, 20), runner.d.ops
    assert res == runner.d.ops[0][1], "点击前那张就是本步的证据截图"


def test_click_explicit_screenshot_stays_after_click(runner):
    """显式 `screenshot:` 键 = 用户要点击后的状态 → 仍是点击后拍(老语义不变)"""
    runner.d = _SeqDev()
    runner._execute({"desc": "点完留证", "click": [10, 20], "screenshot": True})
    assert [o[0] for o in runner.d.ops] == ["click", "shot"], runner.d.ops


def test_click_coordinates(runner):
    runner._execute({"click": [531, 1900]})
    assert runner.d.clicked_at == (531, 1900)


def test_click_text(runner):
    runner._execute({"click": "确认"})
    # 引擎对非 "=" 前缀走 textContains(找不到才回退 description),不是精确 text
    assert runner.d.last_kw.get("textContains") == "确认"


def test_back_presses_key(runner):
    runner._execute({"back": True})
    assert runner.d.pressed == "back"


def test_swipe_direction_left(runner):
    runner._execute({"swipe": "left"})
    sx, sy, ex, ey = runner.d.swiped
    assert ex < sx and sy == ey


def test_swipe_fast_left(runner):
    runner._execute({"swipe": "fast-left"})
    sx, sy, ex, ey = runner.d.swiped
    # 从中心向两侧展开: 起点在中心右侧、终点在中心左侧,行程 = 2 * (屏宽 * 0.3)
    w = 1080
    dist = int(w * 0.3)
    assert (sx, ex) == (w // 2 + dist, w // 2 - dist)
    assert sy == ey


def test_swipe_quad(runner):
    runner._execute({"swipe": [82, 2101, 82, 1601]})
    assert runner.d.swiped == (82, 2101, 82, 1601)


def test_swipe_bad_direction(runner):
    with pytest.raises(ValueError):
        runner._execute({"swipe": "diagonal"})


def test_if_click_hits_first_present(runner):
    runner._execute({"if_click": "不存在,确认"})
    assert runner.d.last_kw.get("textContains") == "确认"


def test_if_click_absent_is_not_error(runner):
    runner._execute({"if_click": "不存在的按钮"})   # 不抛异常即通过


def test_find_click_raises_when_none(runner):
    with pytest.raises(AssertionError):
        runner._execute({"find_click": "全都没有1,全都没有2"})


def test_click_priority_over_assert(runner):
    """同一步里 click(100) 应先于 assert(95) 命中并屏蔽它"""
    assert reg.ACTIONS["click"].priority < reg.ACTIONS["assert"].priority
    order = [a.key for a in reg.dispatch_order()]
    assert order.index("click") < order.index("assert")


def test_if_else_branch_runs_when_condition_absent(runner):
    """条件不成立 → 执行 else 分支(源项目反直觉语义)"""
    runner.steps_hit = []
    runner._execute({
        "if": "不存在的文本",
        "else": [{"desc": "else步骤1", "click": [10, 20]}],
    })
    assert runner.d.clicked_at == (10, 20)
    # 子步骤描述带分支标记(用户要求 2026-10-12: 结果表里要分得清属于哪个分支)
    assert runner._pending_sub_results[0]["desc"] == "[条件不成立] else步骤1"


def test_if_branch_skips_when_condition_present(runner):
    """条件成立 → 直接跳过,不执行 else"""
    runner._execute({
        "if": "确认",
        "else": [{"desc": "不该执行", "click": [99, 99]}],
    })
    assert runner.d.clicked_at is None


# ── 坐标的两种写法(用户实测: 界面上写 "x,y" 点不动) ──

@pytest.mark.parametrize("value", ["531,1900", "531，1900", "531 1900", [531, 1900]])
def test_click_coordinates_accept_string_and_list(runner, value):
    """★ 界面上这些字段是**文本框**, 用户按提示写 "x,y" 得到的是字符串;

    实现原来只认 YAML 列表 [x, y](v1.4 Appium 用例转换来的写法), 字符串会掉进
    "按文本找元素"的分支 -> 轮询到超时报错(用户实测「点击坐标不能正常执行」)。
    """
    runner._execute({"click": value})
    assert runner.d.clicked_at == (531, 1900), runner.d.clicked_at


def test_click_non_coordinate_string_still_goes_text_path(runner):
    """含字母/中文的不是坐标 —— 不能被当成坐标解析"""
    runner._execute({"click": "确认"})
    assert runner.d.last_kw.get("textContains") == "确认"
    assert runner.d.clicked_at is None


@pytest.mark.parametrize("value", ["100,200", "100,200,3"])
def test_long_click_coordinates_accept_string(runner, value):
    runner._execute({"long_click": value})
    x1, y1, x2, y2 = runner.d.swiped          # 原地滑动 = 长按
    assert (x1, y1) == (100, 200) and (x2, y2) == (100, 200)


def test_swipe_coordinates_accept_string(runner):
    runner._execute({"swipe": "82,2101,82,1601"})
    assert runner.d.swiped == (82, 2101, 82, 1601)


def test_swipe_coordinates_wrong_count_says_so(runner):
    """数字个数不对时要报"坐标要 4 个", 别让人以为是"方向名不认识" """
    with pytest.raises(ValueError, match="4 个数字"):
        runner._execute({"swipe": "82,2101,82"})


def test_swipe_direction_still_works_after_coordinate_support(runner):
    """"left" 这类方向名不能被坐标解析吃掉"""
    runner._execute({"swipe": "left"})
    sx, sy, ex, ey = runner.d.swiped
    assert ex < sx and sy == ey


def test_empty_assert_value_fails_loudly_instead_of_passing(runner):
    """★ 空值的文本断言**必须报错**, 不能"不检测就通过"。

    真机实测(2026-10-11): `d(textContains="")` 匹配**任意**元素(exists=True) ——
    于是值为空的断言会立刻"通过", 用户看到的是"连续两个断言, 第二个没检测成功却通过了"。
    空值还更危险地影响 click: 会点到随机位置。所以保护加在 _find_element 入口,
    所有判断/点击一起受益。
    """
    # None 也要走同一条报错: 手写 YAML 的 `assert:`(空)解析出来就是 None,
    # 排在 startswith 之后会先撞 AttributeError(用户看到的是崩栈而不是中文报错)
    for bad in ("", "   ", "\t", None):
        with pytest.raises(ValueError, match="为空"):
            runner._find_element(bad)
    with pytest.raises(ValueError, match="为空"):
        runner._execute({"assert": ""})
    # 正常的断言/点击不受影响
    runner._execute({"assert": "确认"})
    runner._execute({"click": "确认"})


# ── if 的 then 分支(条件成立时执行的子步骤, 2026-10-11 用户要求) ──

def test_if_runs_then_branch_when_condition_holds(runner):
    """条件成立 → 执行 then 子步骤(以前只有 else 分支有子步骤)"""
    ran = []
    runner._execute({"if": "确认",
                     "then": [{"desc": "成立时做", "click": [10, 20]}],
                     "else": [{"desc": "不该走", "click": [99, 99]}]})
    assert runner.d.clicked_at == (10, 20), runner.d.clicked_at
    assert [r["desc"] for r in runner._pending_sub_results] == ["[条件成立] 成立时做"]


def test_if_runs_else_branch_when_condition_fails(runner):
    """条件不成立 → 走 else(原语义不变); then 不执行"""
    runner._execute({"if": "不存在的文本",
                     "then": [{"desc": "不该走", "click": [10, 20]}],
                     "else": [{"desc": "不成立时做", "click": [77, 88]}]})
    assert runner.d.clicked_at == (77, 88), runner.d.clicked_at
    assert [r["desc"] for r in runner._pending_sub_results] == ["[条件不成立] 不成立时做"]


@pytest.mark.parametrize("key,cond", [("if", "确认"), ("if not", "不存在的文本")])
def test_if_without_then_still_skips(runner, key, cond):
    """★ 向后兼容: 没写 then 时, **取反后**的条件成立仍然是"直接跳过"(if 与 if not 都要)。

    (if not 要挑一个页面上**不存在**的条件, 取反后才成立 —— 这是它本来的语义)
    """
    step = {key: cond, "else": [{"desc": "不该走", "click": [99, 99]}]}
    runner._execute(step)
    assert runner.d.clicked_at is None
    assert runner._pending_sub_results is None, "空分支必须清掉子结果(否则重试会留下幽灵行)"


def test_if_not_then_branch_uses_negated_condition(runner):
    """★ if not: 条件**取反后**成立 → 走 then(与 if 同一套实现, 必须同步生效)"""
    runner._execute({"if not": "不存在的文本",
                     "then": [{"desc": "取反后成立", "click": [33, 44]}]})
    assert runner.d.clicked_at == (33, 44), runner.d.clicked_at
    assert [r["desc"] for r in runner._pending_sub_results] == ["[条件成立] 取反后成立"]


def test_if_then_branch_failure_propagates(runner):
    """then 子步骤失败要抛出(由主循环按重试/失败处理), 并留下失败子行"""
    with pytest.raises(AssertionError):
        runner._execute({"if": "确认",
                         "then": [{"desc": "会失败", "click": "不存在的按钮"}]})
    assert runner._pending_sub_results and runner._pending_sub_results[0]["passed"] is False


# ── 点击必须留落点 + 点击类动作强制截图(用户要求 2026-10-11) ──

@pytest.mark.parametrize("step,label", [
    ({"click": "100,200"}, "坐标"),
    ({"click": "确认"}, "文本"),
    ({"long_click": "100,200"}, "长按"),
    ({"if_click": "确认"}, "if_click"),
    ({"find_click": "确认"}, "find_click"),
])
def test_every_click_records_where_it_clicked(runner, step, label):
    """★ 每种点击都要记下落点(供截图标注"点在哪") —— 坐标/模板原来有, 文本/if_click/
    find_click/长按是空白, 报告里就看不出点在哪"""
    runner._execute(dict(step))
    lc = runner.last_click
    assert lc, f"{label} 点击没有记落点: {lc}"
    assert lc[0] > 0 and lc[1] > 0, f"{label} 落点坐标不对: {lc}"


def test_click_step_always_screenshots_even_if_disabled(runner, tmp_path):
    """★ 点击类动作**强制截图**(不可关闭): 写 screenshot: False 也照样截, 且带点击标注"""
    shots = []
    runner.d.screenshot = lambda path=None, format=None: shots.append(path)
    out = runner._execute({"click": "100,200", "screenshot": False})
    assert shots, "点击步骤必须截图(哪怕步骤里写了 screenshot: False)"
    assert out and "screenshots" in out.replace("\\", "/"), out
    assert runner.last_click is None, "落点应已被截图消费(标记画在图上)"


def test_non_click_step_still_respects_screenshot_flag(runner):
    """非点击动作不受影响: 没勾自动截图就不截"""
    shots = []
    runner.d.screenshot = lambda path=None, format=None: shots.append(path)
    # ★ 注意: 引擎是看"screenshot 键在不在"(GUI 保存 False 时会把键整个省掉),
    #   所以这里用"根本没有这个键"的步骤
    out = runner._execute({"assert": "确认"})
    assert shots == [] and out == "", (shots, out)


def test_click_template_step_also_forced_screenshots(runner, monkeypatch, tmp_path):
    """点击模板(下拉)同样强制截图 —— 它也是「点击操作」"""
    from core import vision
    monkeypatch.setattr(vision, "find_in_gray", lambda s, t, **kw: (300, 400))
    monkeypatch.setattr(vision, "resolve_template", lambda n: n)
    import numpy as np
    shots = []

    def _shot(path=None, format=None):
        shots.append(path)
        return np.zeros((1920, 1080, 3), dtype=np.uint8)   # 模板匹配要真图

    runner.d.screenshot = _shot
    out = runner._execute({"click_template": "开始清扫.png", "screenshot": False})
    assert shots and out, (shots, out)


def test_precondition_click_steps_do_not_force_screenshots(runner):
    """★ 只有**用例步骤**里的点击强制截图; 前置条件里的自定义步骤不截。

    用户要求(2026-10-11): "测试步骤的点击操作才需要截图" —— 前置是准备动作, 不留证据。
    """
    from core.runner import ActionRunner
    r = ActionRunner(runner.d, {"step_interval": 0, "default_timeout": 1, "click_timeout": 1},
                     case_name="前置步骤", click_shots=False)
    r._sleep = lambda s: None
    shots = []
    r.d.screenshot = lambda path=None, format=None: shots.append(path)
    out = r._execute({"click": "100,200"})
    assert shots == [] and out == "", (shots, out)
    assert r.last_click, "前置里的点击仍然要记落点(日志/排查用), 只是不截图"


def test_preconditions_also_force_click_screenshots():
    """结构性守护: 前置里的自定义步骤**也**要强制截图(用户 2026-10-12 改的要求:
    "前置也改为默认截图, 并放入报告中") —— 所以 session 里不该再关掉 click_shots。"""
    import os as _os
    src = open(_os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                             "core", "session.py"), encoding="utf-8").read()
    assert "click_shots=False" not in src, "前置又把它关掉了(用户要求前置也截图)"


def test_step_start_event_fires_once_per_attempt(runner, monkeypatch):
    """★ 步骤开始事件**一步一次**(重试则每轮各一次), 执行过程中**不再发**。

    界面靠它插一行「执行中」, 跑完就地改成 PASS/FAIL(用户 2026-10-12 提的需求)。
    ★ 如果哪天有人让它在执行期间周期性地发, 结果表就会一直重绘 → 用户看到的"闪动"
      (用户当场就问过"执行中时会一直刷新吗, 是不是会闪动")。这条守住这个性质。
    """
    from core import registry as reg
    starts = []
    runner.on_step_start = lambda desc, attempt=1: starts.append((desc, attempt))
    assert runner.run_steps([{"desc": "点一下", "click": "确认"}]) is True
    assert starts == [("点一下", 1)], f"正常一步只该发一次开始事件: {starts}"

    # 重试: 前两次失败、第三次成功 → 3 次开始事件(界面复用同一行显示"执行中(第N次)")
    calls = {"n": 0}

    def _flaky(r, s):
        calls["n"] += 1
        if calls["n"] < 3:
            raise AssertionError("先失败")

    monkeypatch.setattr(reg.ACTIONS["click"], "fn", _flaky)
    starts.clear()
    assert runner.run_steps([{"desc": "重试步骤", "click": "确认", "retry": 2}]) is True
    assert starts == [("重试步骤", 1), ("重试步骤", 2), ("重试步骤", 3)], starts


def test_screenshots_are_split_per_execution(runner):
    """★ 同一个用例跑多轮时, 截图必须落到**不同的目录**(否则后一轮覆盖前一轮)。

    真机现象(用户 2026-10-12): "多轮用例的测试截图没有区分开, 报告里每次截图都是同一个"
    —— 报告里的截图是**嵌进去的图片**, 而每轮的路径都是 screenshots/<用例名>/stepNN.png,
    后一轮把文件覆盖掉, 于是每轮嵌的都是同一份。
    """
    outs = []
    runner.d.screenshot = lambda path=None, format=None: outs.append(path)
    assert runner.run_steps([{"desc": "点一下", "click": "确认", "screenshot": True}])
    runner.shot_subdir = "甲用例-第2次"          # worker 每次执行会设成"用例名-第N次"
    assert runner.run_steps([{"desc": "点一下", "click": "确认", "screenshot": True}])
    # 只要"有路径"的那几次(TraceRecorder 每步也会截一帧 format="opencv", path=None)
    paths = [p for p in outs if p]
    assert len(paths) == 2, outs
    first, second = [p.replace("\\", "/") for p in paths]
    assert first != second, f"两轮截图路径不能相同: {first}"
    assert f"screenshots/{runner.case_name}/" in first, first
    assert "screenshots/甲用例-第2次/" in second, second


# ── if 条件的检测节奏: 两轮 × 每轮五次(= 10 次), 第 5/10 次走 OCR 兜底(用户 2026-10-12) ──

def test_if_condition_checks_ten_times_by_default(runner):
    """★ if 的四种条件都按"两轮 × 五次 = 10 次"检测, 不受 timeout=5 秒支配。

    用户: "都要按两轮查询, 每轮五次, 前四次普通检测后1次上OCR兜底" + "5s 容错太低了"。
    """
    import core.actions.basic as basic
    assert (basic.IF_ROUNDS, basic.IF_CHECKS_PER_ROUND, basic.IF_TOTAL_CHECKS) == (2, 5, 10)

    calls = []
    runner._text_present = lambda *a, **k: (calls.append(k), False)[1]
    runner._poll_sleep = lambda n: None
    runner._execute({"if": "永远不会出现的文本", "else": []})
    assert calls and calls[0].get("rounds") == 10, calls     # 文本: 按 10 次轮询

    # 显式写 timeout 时取更大值(向后兼容, 秒数≈每次间隔)
    calls.clear()
    runner._execute({"if": "永远不会出现的文本", "timeout": 30, "else": []})
    assert calls[0].get("rounds") == 30, calls


def test_if_text_condition_really_polls_rounds_times(runner, monkeypatch):
    """★ 回归(审查 2026-10-12): _text_present 的 rounds 模式必须**真查满 N 次**。

    2.0.7 曾把"查够"的判断放在 end 之后, 而 rounds 模式 end=None —— 首次失败即
    return, 文本条件实际只查 1 次(比 2.0.6 的 5 秒轮询还差), 上面那条只 mock 方法
    断言 kwargs, 管不住真循环。这里不 mock _text_present, 直测它内部的循环。
    """
    import core.runner as cr
    seq = {"n": 0}

    def _poll(n):
        seq.setdefault("sleeps", []).append(n)

    monkeypatch.setattr(runner, "_poll_sleep", _poll)
    # 第 10 次(最后一次)才出现 → 仍应判"成立", 且恰好查 10 次、只睡 9 次
    monkeypatch.setattr(cr, "text_present",
                        lambda d, texts, round_no=None:
                        (seq.__setitem__("n", seq["n"] + 1), seq["n"] >= 10)[1])
    assert runner._text_present("确认", rounds=10) is True
    assert seq["n"] == 10, seq
    assert len(seq["sleeps"]) == 9, seq          # 末次已定局, 不再白睡(审查 2026-10-12)

    # 一直不出现 → 恰好 10 次, 不多不少; OCR 兜底轮次落在 5/10(round_no = 次数)
    seq["n"] = 0
    seq["sleeps"] = []
    seen_rn = []
    monkeypatch.setattr(cr, "text_present",
                        lambda d, texts, round_no=None:
                        (seq.__setitem__("n", seq["n"] + 1),
                         seen_rn.append(round_no), False)[2])
    assert runner._text_present("确认", rounds=10) is False
    assert seq["n"] == 10 and len(seq["sleeps"]) == 9, seq
    assert seen_rn == list(range(1, 11)), seen_rn


def test_if_checks_capped_at_120(runner):
    """timeout 重载成"次数"后大值要有上限(审查 2026-10-12):
    timeout: 1800 → 1800 次 × 阶梯(1/2/5s) ≈ 2.5 小时, 整个队列像挂死"""
    calls = []
    runner._text_present = lambda *a, **k: (calls.append(k), False)[1]
    runner._poll_sleep = lambda n: None
    runner._execute({"if": "x", "timeout": 1800, "else": []})
    assert calls[0].get("rounds") == 120, calls


def test_if_image_condition_ignores_threshold_separation(runner, monkeypatch):
    """★ if 判断与图像对比**分为两个步骤**(用户 2026-10-13): if 里的 threshold 不再生效,
    图片条件一律"在页面里找这张模板图"; 按相似度对比是 compare 步骤的职责。"""
    from core import vision
    seq = {"n": 0}
    compares = []

    def _find(gray, tpl, **kw):
        seq["n"] += 1
        return None
    monkeypatch.setattr(vision, "find_in_gray", _find)
    monkeypatch.setattr(vision, "resolve_template", lambda n: f"tpl/{n}")
    monkeypatch.setattr("core.actions.asserts.compare_screen",
                        lambda *a, **k: compares.append(1))
    runner._poll_sleep = lambda n: None
    import numpy as np
    runner.d.screenshot = (lambda path=None, format=None:
                           np.zeros((1920, 1080, 3), dtype=np.uint8))
    runner._execute({"if": "按钮.png", "threshold": 1,
                     "else": [{"desc": "不成立时点", "click": [1, 2]}]})
    assert seq["n"] == 10, f"图片条件走模板匹配(两轮×五次): {seq['n']}"
    assert not compares, "if 步骤不得再做整屏相似度对比(那是 compare 步骤的职责)"
    assert runner._pending_sub_results, "没找到模板 → 条件不成立 → 走 else"


def test_if_template_condition_now_waits_instead_of_single_shot(runner, monkeypatch):
    """★ 模板条件原来**只查一次**(晚 1 秒出现就走进 else), 现在同样跑满节奏"""
    from core import vision
    seq = {"n": 0}

    def _find(screen, tpl, **kw):
        seq["n"] += 1
        return (300, 400) if seq["n"] >= 3 else None      # 第 3 次才出现
    monkeypatch.setattr(vision, "find_in_gray", _find)
    monkeypatch.setattr(vision, "resolve_template", lambda n: n)
    runner._poll_sleep = lambda n: None
    import numpy as np
    shot = []
    runner.d.screenshot = (lambda path=None, format=None:
                           shot.append(1) or np.zeros((1920, 1080, 3), dtype=np.uint8))
    ran = []
    runner._execute({"if": "某按钮.png", "else": [{"desc": "不该走"}]})
    assert seq["n"] == 3, f"应在第 3 次检测到(共查了 {seq['n']} 次)"
    assert not runner._pending_sub_results, "条件成立就不该走 else"


def test_if_numeric_condition_retries_until_it_holds(runner, monkeypatch):
    """★ 数值条件也从"取一次快照"改成按节奏重试(电量刚充到门槛之上才成立)"""
    import core.actions.data_ops as data_ops
    seq = {"n": 0}

    def _val(r, nodes, kw):
        seq["n"] += 1
        return "51" if seq["n"] >= 4 else "49"
    monkeypatch.setattr(data_ops, "extract_value", _val)
    runner._poll_sleep = lambda n: None
    runner._execute({"if": "电量 > 50", "else": [{"desc": "不该走"}]})
    assert seq["n"] == 4, seq
    assert not runner._pending_sub_results


def test_branch_substeps_are_labelled_in_results(runner):
    """★ then/else 子步骤的结果描述要带**分支标记**, 否则结果表/报告里分不清归属。

    用户 2026-10-12: "if 分支下 then/else 子分支执行的步骤没有在测试步骤中标注一下, 不好区分"。
    期望: 成立时子步骤显示 `[条件成立] xxx`, 不成立时显示 `[条件不成立] xxx`。
    """
    # 条件成立 → then 分支(带标记)
    runner._execute({"if": "确认",
                     "then": [{"desc": "成立时点一下", "click": [10, 20]}],
                     "else": [{"desc": "不该走", "click": [99, 99]}]})
    assert [r["desc"] for r in runner._pending_sub_results] == ["[条件成立] 成立时点一下"], \
        runner._pending_sub_results
    assert runner.d.clicked_at == (10, 20)

    # 条件不成立 → else 分支(带标记)
    runner._execute({"if": "不存在的文本",
                     "then": [{"desc": "不该走", "click": [10, 20]}],
                     "else": [{"desc": "不成立时点一下", "click": [77, 88]}]})
    assert [r["desc"] for r in runner._pending_sub_results] == ["[条件不成立] 不成立时点一下"], \
        runner._pending_sub_results
    assert runner.d.clicked_at == (77, 88)

    # if not 同样按"取反后"的分支标注
    runner._execute({"if not": "不存在的文本",
                     "then": [{"desc": "取反后成立", "click": [33, 44]}]})
    assert [r["desc"] for r in runner._pending_sub_results] == ["[条件成立] 取反后成立"], \
        runner._pending_sub_results
