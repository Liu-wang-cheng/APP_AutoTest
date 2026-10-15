# -*- coding: utf-8 -*-
"""basic 动作: click 坐标/文本/优先级, swipe 映射, if_click, back。FakeDevice 无设备。"""
import sys
from pathlib import Path

import threading
import time

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

    def swipe(self, sx, sy, ex, ey, duration=None):
        self.ops.append(("swipe", sx, sy, ex, ey))

    def screenshot(self, path=None, format=None):
        self.ops.append(("shot", path))
        from pathlib import Path as _P
        p = _P(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"png")


class _FrameDev(FakeDevice):
    """真画面的桩: `format="opencv"` 给帧, 给 path 就写一张真 PNG(对比/标注流程要用)"""

    def screenshot(self, path=None, format=None):
        import cv2
        import numpy as np
        frame = np.full((120, 80, 3), 200, dtype=np.uint8)
        if path:
            from pathlib import Path as _P
            p = _P(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(cv2.imencode(".png", frame)[1].tobytes())
            return path
        return frame


def test_click_screenshots_before_clicking(runner):
    """★ 点击证据要**点击前**拍: 点完再截页面已变, 标注的坐标对不上那一刻的界面。"""
    runner.d = _SeqDev()
    res = runner._execute({"desc": "点一下", "click": [10, 20]})
    assert runner.d.ops[0][0] == "shot", runner.d.ops
    assert runner.d.ops[1] == ("click", 10, 20), runner.d.ops
    assert res == runner.d.ops[0][1], "点击前那张就是本步的证据截图"


def test_click_explicit_screenshot_still_shoots_before_click(runner):
    """★ 勾了截图/写了 `screenshot:` 键的点击步骤**也要在点击前**拍(用户 2026-10-15:
    "点击截图还是没有先标记点位截图再进行点击，截到的图和点击位置对应不上")。

    以前这种步骤会走"点击后重拍" —— 那张既是**点击后**的页面(可能已跳转)、
    又没有点位标记, 与点击位置对不上。证据永远是"点击前 + 带标记"那张。
    """
    runner.d = _SeqDev()
    res = runner._execute({"desc": "点完留证", "click": [10, 20], "screenshot": True})
    kinds = [o[0] for o in runner.d.ops]
    assert kinds[:2] == ["shot", "click"], runner.d.ops
    assert res == runner.d.ops[0][1], "证据必须是点击前那张(而不是后置重拍)"


def test_prereq_click_shoots_before_click(runner):
    """★ 前置里的点击同样如此(用户 2026-10-15: "还有前置步骤的点击"): 前置 runner
    是 click_shots=False + always_screenshot=True —— 也要"先拍带标记的、再点"。"""
    runner.click_shots = False
    runner.always_screenshot = True
    runner.d = _SeqDev()
    res = runner._execute({"desc": "前置点一下", "click": [5, 6], "screenshot": True})
    kinds = [o[0] for o in runner.d.ops]
    assert kinds[:2] == ["shot", "click"], runner.d.ops
    assert res == runner.d.ops[0][1]


# ── 暂停/继续(用户 2026-10-13) ──
# ★ 测试设计原则: 不与墙钟竞速(OCR 懒加载等会让时序漂移), 分段验证机制本身。
_REAL_SLEEP = ActionRunner._sleep   # _fast_sleep(autouse) 换掉前的真实现(暂停测试要用)


def test_pause_suspends_between_steps_and_resume_completes(runner):
    """步骤 1 跑完 → 挂起(run_steps 步骤间) → 恢复后步骤 2 才开始"""
    fired = []
    orig = runner._execute

    def _rec(step):
        out = orig(step)
        fired.append(time.time())
        if len(fired) == 1:
            runner.pause()
            threading.Timer(0.5, runner.resume).start()
        return out
    runner._execute = _rec
    ok = runner.run_steps([{"desc": "等一下", "wait": 0.05},
                           {"desc": "再等一下", "wait": 0.05}])
    assert ok is True and len(fired) == 2, fired
    assert fired[1] - fired[0] >= 0.4,         f"步骤 2 应在恢复后才开始(挂起 0.5s), 实际间隔 {fired[1]-fired[0]:.2f}s"


def test_sleep_pauses_and_returns_paused_seconds(runner, monkeypatch):
    """_sleep 挂起: 挂起时长不计入等待, 返回挂起秒数(调用方据此顺延 timeout)。
    ★ 本文件 _fast_sleep(autouse)把 _sleep 换成了 no-op —— 这里恢复真实现再测。"""
    monkeypatch.setattr(ActionRunner, "_sleep", _REAL_SLEEP)
    runner.pause()
    threading.Timer(0.6, runner.resume).start()
    t0 = time.time()
    paused = runner._sleep(0.2)
    wall = time.time() - t0
    assert paused >= 0.35, f"应返回挂起秒数(≥0.4), 实际 {paused:.2f}"
    assert wall >= paused + 0.15,         f"实际耗时 = 0.2 等待 + 挂起, 实际 {wall:.2f}s (挂起 {paused:.2f}s)"


def test_assert_timeout_extends_by_pause(runner, monkeypatch):
    """timeout 顺延: _poll_sleep 每轮返回"挂起了 2s" → 1s 的断言在 2.5s 内不应超时
    (旧行为: 1s 即抛 AssertionError —— 该行为已随暂停引入而改变)"""
    monkeypatch.setattr(runner, "_poll_sleep", lambda n: 2.0)
    errors = []

    def _run():
        try:
            runner._assert_locator("永远不会出现", timeout=1)
        except AssertionError as e:
            errors.append(e)
    t = threading.Thread(target=_run, daemon=True)
    t0 = time.time()
    t.start()
    t.join(2.5)
    assert t.is_alive(),         f"1s 超时 + 每轮挂起 2s 顺延, 2.5s 内不该抛: {errors}"


def test_wait_for_timeout_extends_by_pause(runner, monkeypatch):
    """★ wait_for 轮询同样顺延(2026-10-14 审查 P1): _poll_sleep 每轮返回"挂起 2s"
    → 15s 的 wait_for 在 3s 墙钟内不该超时(旧实现必超时)"""
    monkeypatch.setattr(runner, "_poll_sleep", lambda n: 2.0)
    errors = []

    def _run():
        try:
            runner._execute({"wait_for": "永远不会出现", "timeout": 3})
        except AssertionError as e:
            errors.append(e)
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(3.0)
    assert t.is_alive(),         f"3s 超时 + 每轮挂起 2s 顺延, 3s 墙钟内不该抛: {errors}"


def test_stopped_step_row_is_marked_stopped(runner):
    """★ 被停止打断的步骤行带 stopped 标记(界面据此显示中性「⏹ 已停止」, 用户
    2026-10-15: 正常测试步骤也要检查同类问题) —— 未执行的后续步骤则**不落表**。"""
    seen = []
    runner.on_result = lambda res: (seen.append(dict(res)),
                                   runner.stop() if res["desc"] == "第一步" else None)
    ok = runner.run_steps([{"desc": "第一步", "wait": 0.01},
                           {"desc": "第二步(不该执行)", "wait": 0.01}])
    assert ok is False
    descs = [r["desc"] for r in seen]
    assert "第二步(不该执行)" not in descs, f"未执行的步骤不该落表: {descs}"
    stopped = [r for r in seen if r.get("stopped")]
    assert stopped, f"被停止打断的行要带 stopped 标记: {seen}"


def test_stop_wins_over_pause(runner):
    """挂起中点停止 → 立即 UserStopped(停止优先于暂停)"""
    from core.runner import UserStopped
    runner.pause()
    threading.Timer(0.3, runner.stop).start()
    with pytest.raises(UserStopped):
        runner._wait_if_paused()


# ── 滑动: 默认截图 + 路径标注 + 起止拆分(用户 2026-10-14) ──

def test_swipe_screenshots_before_swiping_with_path(runner):
    """★ 滑动与点击同权: **滑动前**截图(证据 = 本步 screenshot), 顺序 shot→swipe"""
    runner.d = _SeqDev()
    res = runner._execute({"desc": "滑一下", "swipe": [100, 500, 100, 1500]})
    kinds = [o[0] for o in runner.d.ops]
    assert kinds[0] == "shot" and kinds[1] == "swipe", runner.d.ops
    assert res == runner.d.ops[0][1], "滑动前那张就是本步证据截图"


def test_swipe_start_end_split_fields(runner):
    """★ 界面拆两框: swipe=开始点位 + swipe_end=结束点位 → 拼成一次滑动"""
    runner.d = _SeqDev()
    runner._execute({"desc": "滑", "swipe": "100,500", "swipe_end": "100,1500"})
    swipes = [o for o in runner.d.ops if o[0] == "swipe"]
    assert swipes == [("swipe", 100, 500, 100, 1500)], runner.d.ops
    assert runner.d.ops[0][0] == "shot", "拆分写法同样要先截图标注"


def test_swipe_direction_marks_path(runner, monkeypatch):
    """方向名滑动(left 等)也走标注: 证据槽被填, 坐标按窗口尺寸展开"""
    runner.d = _SeqDev()
    res = runner._execute({"desc": "左滑", "swipe": "left"})
    assert any(o[0] == "shot" for o in runner.d.ops), "方向滑动也要默认截图"
    assert res == runner.d.ops[0][1], "滑动前的标注图就是本步证据(采用后槽已消费)"


def test_swipe_legacy_full_string_still_works(runner):
    """旧用例整串写法 [sx,sy,ex,ey] 不动: 无 swipe_end 时按原解析"""
    runner.d = _SeqDev()
    runner._execute({"desc": "老写法", "swipe": "[100,500,100,1500]"})
    swipes = [o for o in runner.d.ops if o[0] == "swipe"]
    assert swipes == [("swipe", 100, 500, 100, 1500)], runner.d.ops


def test_sleep_step_screenshots_before_waiting(runner):
    """★ 延时等待步骤的证据在**等待开始前**拍(用户 2026-10-14: 设置 180s 延迟,
    截图也等了 180s —— 等待本身没有画面变化, 证据应记录"开始等待时的状态")"""
    runner.always_screenshot = True    # 前置/默认截图场合(生产: 前置 runner 恒 True)
    runner.d = _SeqDev()
    waits = []
    orig = runner._sleep

    def _rec(s):
        waits.append(("wait", s))
        return orig(s)
    runner._sleep = _rec
    res = runner._execute({"desc": "等180秒", "sleep": 180})
    kinds = [o[0] for o in runner.d.ops] + [w[0] for w in waits]
    assert kinds[0] == "shot" and "wait" in kinds, runner.d.ops
    assert kinds.index("shot") < kinds.index("wait"),         f"截图必须在等待开始前: {runner.d.ops} {waits}"
    assert res == runner.d.ops[0][1], "等待前那张就是本步证据截图"


# ── if 子步骤证据归属(用户 2026-10-15: 子步骤截图显示错乱) ──

def test_nested_if_keeps_all_rows_and_order(runner):
    """★ 嵌套 if(用户 2026-10-15 审查发现两个真 bug):
    · 外层已攒的子结果**不能被内层清掉** —— 以前 A 步直接从结果/报告里消失;
    · 容器型子步骤(内层 if 自己)**排在它的子步骤之前**。"""
    runner.d = _SeqDev()
    runner.run_steps([{"desc": "外层if", "if": "确认",
                       "then": [{"desc": "A步", "wait": 0.01},
                                {"desc": "内层if", "if": "确认",
                                 "then": [{"desc": "B步", "wait": 0.01}]},
                                {"desc": "C步", "wait": 0.01}]}])
    descs = [r["desc"] for r in runner.results]
    assert descs == ["外层if", "[条件成立] A步", "[条件成立] 内层if",
                     "[条件成立] B步", "[条件成立] C步"], descs


def test_if_screenshot_taken_before_branch_wait(runner):
    """★ if 步骤的截图在**判定那一刻**, 不被分支里的长等待拖后
    (用户 2026-10-15 真机日志: "if判断的步骤截图也是延迟180s之后的")。

    根因: 分支内每个子步骤都会清证据槽(隔离子步骤归属) —— 把父 if 判定前那张也
    清掉了, 后置块只好等分支(含 180s 等待)跑完重拍。现在分支前后保存/还原父证据。
    """
    ops = []
    dev = _SeqDev()
    orig_shot = dev.screenshot

    def _shot(path=None, format=None):
        if path:
            ops.append("shot")
        return orig_shot(path, format)
    dev.screenshot = _shot
    runner.d = dev
    runner.click_shots = True
    runner.always_screenshot = True
    real_sleep = _REAL_SLEEP
    runner._sleep = lambda s: (ops.append("wait"), real_sleep(runner, s))[1]
    runner._execute({"desc": "判断是否清洗拖布", "if": "确认", "screenshot": True,
                     "then": [{"desc": "等待180秒", "sleep": 0.3}]})
    assert ops.index("shot") < ops.index("wait"),         f"截图必须在分支等待之前: {ops}"


def test_if_substep_evidence_not_inherited(runner):
    """★ 分支子步骤的截图各归各(用户 2026-10-15: "if 分支子步骤的截图在执行结果
    和报告中显示不正确"): 点击子步骤拍的照片, 不得被后续断言子步骤、乃至父 if 行
    当成自己的继承下去。"""
    runner.d = _SeqDev()
    ok = runner.run_steps([
        {"desc": "分支", "if": "确认",
         "then": [{"desc": "点A", "click": [1, 2]},
                  {"desc": "断言B", "assert": "永远不会出现", "timeout": 0.1}],
         "else": []}])
    assert ok is False
    rows = {r["desc"]: r.get("screenshot", "") for r in runner.results}
    click_shot = rows.get("[条件成立] 点A")
    assert click_shot, f"点击子步骤应有自己的截图: {rows}"
    assert rows.get("[条件成立] 断言B", "x") != click_shot,         "断言子步骤不得继承点击子步骤的截图"
    assert rows.get("分支", "x") != click_shot,         "父 if 行不得继承子步骤的截图"


def test_failed_row_error_has_timestamp(runner):
    """★ 失败信息带发生时间(用户 2026-10-15): 报告/执行结果的 error 列要能看出
    "什么时候失败的" —— 与 [FAIL诊断] 日志同一时刻口径。"""
    import re
    runner._execute({"desc": "等一下", "wait": 0.01})
    ok = runner.run_steps([{"desc": "断言失败步", "assert": "不会有这个文本",
                            "timeout": 0.1}])
    assert ok is False
    err = runner.results[-1]["error"]
    assert re.match(r"^\[\d{2}:\d{2}:\d{2}\] ", err), err


def test_failure_shot_reuses_step_evidence_in_run_dir(runner, monkeypatch, tmp_path):
    """★ 失败截图统一存**用例失败时的那一张**, 和步骤截图同目录(用户 2026-10-15:
    "失败截图统一存用例失败时的那一张，并且和测试步骤截图放同一个目录，其他的什么失败
    时序和失败步骤都不需要了" + "测试步骤本身就包含了测试失败的步骤截图")。

    本步已经有自己的证据图(点击前的点位标记图 / 对比范围标注图)时**直接采用**它 ——
    失败行指向的就是这张, 不会再拍第二张同画面的图, 也不再落 Test_img/debug/。
    """
    import cv2
    import numpy as np
    import core.runner as runner_mod
    monkeypatch.setattr(runner_mod, "BASE_DIR", str(tmp_path))
    runner.shot_subdir = "甲用例_r20260101_090000_第1次"
    runner.d = _FrameDev()

    # 基准图与当前画面明显不同 → 对比步骤判 FAIL(它会先落一张带 FAIL 标注的证据图)
    base = tmp_path / "base.png"
    base.write_bytes(cv2.imencode(
        ".png", np.zeros((120, 80, 3), dtype=np.uint8))[1].tobytes())
    ok = runner.run_steps([{"desc": "对比一下", "compare": str(base), "threshold": 0.9,
                            "compare_area": "0%,0% 100%,100%"}])
    assert ok is False
    row = runner.results[-1]
    shot = row["screenshot"]
    assert "/screenshots/甲用例_r20260101_090000_第1次/" in shot.replace("\\", "/"), shot
    assert shot.endswith("step01_对比一下.png"), shot     # 本步证据图(带 FAIL 标注), 不是另拍的
    assert (tmp_path / "Test_cases" / "未分组" / "screenshots"
            / "甲用例_r20260101_090000_第1次" / "step01_对比一下.png").exists()
    assert not (tmp_path / "Test_img" / "debug").exists(), \
        "不该再有 Test_img/debug 下的失败截图/时序证据"


def test_failure_shot_added_when_step_has_no_evidence(runner, monkeypatch, tmp_path):
    """★ 纯断言类步骤失败时**补拍一张**, 名字固定 `失败截图.png`, 位置同样在本轮截图目录
    —— 保证"失败时一定有一张图", 且和步骤截图在一起(不再有时序证据/逐步失败图)。"""
    import core.runner as runner_mod
    monkeypatch.setattr(runner_mod, "BASE_DIR", str(tmp_path))
    runner.shot_subdir = "甲用例_r20260101_090000_第1次"
    runner.d = _FrameDev()

    ok = runner.run_steps([{"desc": "只断言", "assert": "不会有这个文本", "timeout": 0.1}])
    assert ok is False
    shot = runner.results[-1]["screenshot"]
    assert shot.endswith("失败截图.png"), shot
    assert "/screenshots/甲用例_r20260101_090000_第1次/" in shot.replace("\\", "/"), shot
    assert (tmp_path / "Test_cases" / "未分组" / "screenshots"
            / "甲用例_r20260101_090000_第1次" / "失败截图.png").exists(), shot
    assert not (tmp_path / "Test_img" / "debug").exists(), \
        "不该再有 Test_img/debug 下的失败截图/时序证据"


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
    # 直调 _execute 不落账, 列表累积(落账在最外层 if 结束的 run_steps) → 看最后一条
    assert runner._pending_sub_results[-1]["desc"] == "[条件成立] 成立时做"


def test_if_runs_else_branch_when_condition_fails(runner):
    """条件不成立 → 走 else(原语义不变); then 不执行"""
    runner._execute({"if": "不存在的文本",
                     "then": [{"desc": "不该走", "click": [10, 20]}],
                     "else": [{"desc": "不成立时做", "click": [77, 88]}]})
    assert runner.d.clicked_at == (77, 88), runner.d.clicked_at
    assert runner._pending_sub_results[-1]["desc"] == "[条件不成立] 不成立时做"


@pytest.mark.parametrize("key,cond", [("if", "确认"), ("if not", "不存在的文本")])
def test_if_without_then_still_skips(runner, key, cond):
    """★ 向后兼容: 没写 then 时, **取反后**的条件成立仍然是"直接跳过"(if 与 if not 都要)。

    (if not 要挑一个页面上**不存在**的条件, 取反后才成立 —— 这是它本来的语义)
    """
    step = {key: cond, "else": [{"desc": "不该走", "click": [99, 99]}]}
    runner._execute(step)
    assert runner.d.clicked_at is None
    # ★ 空分支**不新增**子结果(2026-10-15 改共享列表后); 幽灵行防线移到"每次重试前
    #   列表已被上一轮落账清空", 见 _run_branch 注释
    assert not [r for r in (runner._pending_sub_results or [])
                if r["desc"].startswith("[")], runner._pending_sub_results


def test_if_not_then_branch_uses_negated_condition(runner):
    """★ if not: 条件**取反后**成立 → 走 then(与 if 同一套实现, 必须同步生效)"""
    runner._execute({"if not": "不存在的文本",
                     "then": [{"desc": "取反后成立", "click": [33, 44]}]})
    assert runner.d.clicked_at == (33, 44), runner.d.clicked_at
    assert runner._pending_sub_results[-1]["desc"] == "[条件成立] 取反后成立"


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
    # 只要「有路径」的那几次
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
    assert runner._pending_sub_results[-1]["desc"] == "[条件不成立] 不成立时点一下", \
        runner._pending_sub_results
    assert runner.d.clicked_at == (77, 88)

    # if not 同样按"取反后"的分支标注
    runner._execute({"if not": "不存在的文本",
                     "then": [{"desc": "取反后成立", "click": [33, 44]}]})
    assert runner._pending_sub_results[-1]["desc"] == "[条件成立] 取反后成立", \
        runner._pending_sub_results
