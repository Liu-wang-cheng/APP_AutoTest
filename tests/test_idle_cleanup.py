# -*- coding: utf-8 -*-
"""空闲闸门: 「产物封顶 / 更新残渣清理」只在**没有执行用例**时做(用户要求 2026-10-12)。

用户原话: "这里清理产物封顶和更新残渣这些操作时需要在工具没有执行用例时去做, 减少内存占用"。

两条不变量, 各有用例钉住:
  ① 执行用例期间 = 关闸: 这些清理**一个文件都不许删**, 只把自己记进待办;
  ② 本轮结束 = 开闸: 待办**必须被补做**(推迟不等于取消) —— 否则产物封顶在长时间跑用例
     时彻底失效, 那是把内存问题换成了磁盘问题。
"""
import inspect
import os
import time

import pytest

from core import bootstrap, idle


@pytest.fixture(scope="module")
def qapp():
    """端到端用例要一个 QApplication(RunWorker 是 QThread) —— 与 test_gui 同样离屏。

    ★ 只在这个文件里定义(而不是挪进 conftest): 本文件的其余用例是纯逻辑, 没有 Qt 也该
      能跑 —— 让 importorskip 只影响用到 qapp 的那一条。
    """
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _mkfiles(d, n, size=10, base=1000):
    """造 n 个 mtime 递增的文件(便于验证"留最新")"""
    d.mkdir(parents=True, exist_ok=True)
    out = []
    for i in range(n):
        f = d / f"f{i}.png"
        f.write_bytes(b"x" * size)
        os.utime(f, (base + i, base + i))
        out.append(f)
    return out


# ── 闸门本体 ──

def test_idle_when_not_running_runs_immediately(tmp_path):
    """空闲时照旧立刻执行, 且不排队(闸门不能把正常运行也拦掉)"""
    d = tmp_path / "reports" / "failures"
    _mkfiles(d, 3)
    assert idle.is_busy() is False
    n, _freed = bootstrap.trim_failures(str(d), keep=2)
    assert n == 1, "空闲时清理应当立刻动手, 而不是排进待办"
    assert idle.pending_names() == []


def test_busy_defers_instead_of_running():
    """执行期间 → 只记待办, 不执行"""
    calls = []
    idle.begin_run()
    try:
        assert idle.is_busy() is True
        idle.defer("甲", lambda: calls.append(1))
        assert calls == [], "执行用例期间不该真的去清理"
        assert idle.pending_names() == ["甲"]
    finally:
        idle.end_run()


def test_same_cleanup_queued_only_once():
    """同一件清理只排一次 —— 一次失败/一轮跑完可能连着调好几次, 不该堆成一串"""
    idle.begin_run()
    try:
        for _ in range(5):
            idle.defer("失败截图循环", lambda: None)
        assert idle.pending_names() == ["失败截图循环"]
    finally:
        idle.end_run()


def test_flush_stops_when_a_new_run_starts():
    """正在补做时用户又点了「执行」→ 立刻停手, 剩下的留到下一轮

    ★ 否则等于把清理挪进了新一轮的执行期间, 白折腾一遍。
    ★ 这里的"又开跑"由第一件清理自己触发(而不是先 end_run 再 begin_run): end_run 会
      起后台线程补做, 那样断言就跟它抢时序, 测试会时不时红一次。
    """
    ran = []

    def _first():
        ran.append(1)
        idle.begin_run()                  # 模拟: 补做进行到一半, 用户点了「执行」

    idle.begin_run()
    idle.defer("甲", _first)
    idle.defer("乙", lambda: ran.append(2))
    idle.end_run()                        # 触发后台补做
    for _ in range(50):                   # 等第一件做完(第二件必须被挡下)
        if ran:
            break
        time.sleep(0.02)
    assert ran == [1], f"新一轮执行期间不该继续补做: {ran}"
    assert idle.pending_names() == ["乙"], idle.pending_names()
    assert idle.is_busy() is True
    idle.end_run()                        # 这一轮也结束 → 剩下的补做掉
    idle.flush(background=False)
    assert ran == [1, 2] and idle.pending_names() == []


def test_flush_does_nothing_while_running():
    """执行期间手工 flush 也不许动手(闸门优先于调用者的意愿)"""
    ran = []
    idle.begin_run()
    try:
        idle.defer("甲", lambda: ran.append(1))
        idle.flush(background=False)
        assert ran == [] and idle.pending_names() == ["甲"]
    finally:
        idle.end_run()


def test_end_run_flushes_pending_in_background():
    """本轮结束会自动补做(不必手工 flush) —— 推迟≠取消"""
    ran = []
    idle.begin_run()
    idle.defer("甲", lambda: ran.append(1))
    assert ran == []
    idle.end_run()
    for _ in range(50):                   # 后台线程, 等它跑完
        if ran:
            break
        time.sleep(0.02)
    assert ran == [1], "本轮结束后没有补做推迟的清理"


def test_begin_end_are_refcounted():
    """嵌套/并发入口也不会把闸门永久焊死(少一次 end_run 不该让清理永远不做)"""
    idle.begin_run()
    idle.begin_run()
    idle.end_run()
    assert idle.is_busy() is True, "还有一层没结束就开闸了"
    idle.end_run()
    assert idle.is_busy() is False


# ── 逐个清理函数: 执行期间不删、结束后补删 ──

def test_prune_run_artifacts_deferred_while_running(tmp_path):
    """运行产物封顶: 执行期间一份不删, 结束后按上限收"""
    shots = tmp_path / "Test_cases" / "组甲" / "screenshots"
    for i in range(12):
        d = shots / f"甲用例-第{i + 1}次"
        d.mkdir(parents=True)
        (d / "step01.png").write_bytes(b"x")
        os.utime(d, (3000 + i, 3000 + i))

    idle.begin_run()
    try:
        assert bootstrap.prune_run_artifacts(str(tmp_path), keep_runs=10) == (0, 0, 0)
        assert len(list(shots.iterdir())) == 12, "执行用例期间不该删截图"
        assert "运行产物封顶" in idle.pending_names()
    finally:
        idle.end_run()
    idle.flush(background=False)
    left = sorted(p.name for p in shots.iterdir())
    assert len(left) == 10 and "甲用例-第1次" not in left, left


def test_prune_debug_artifacts_deferred_while_running(tmp_path):
    """调试产物封顶: 执行期间不删, 结束后按每个目录留最新 N 个"""
    d = tmp_path / "Test_img" / "debug" / "clicks"
    _mkfiles(d, 5)
    idle.begin_run()
    try:
        assert bootstrap.prune_debug_artifacts(str(tmp_path), keep_per_dir=2) == (0, 0)
        assert len(list(d.iterdir())) == 5
        assert "调试产物封顶" in idle.pending_names()
    finally:
        idle.end_run()
    idle.flush(background=False)
    assert sorted(p.name for p in d.iterdir()) == ["f3.png", "f4.png"]


def test_trim_failures_deferred_while_running(tmp_path):
    """失败截图循环覆盖: 执行期间不删(它按用例名存, 推迟不会堆文件), 结束后收"""
    d = tmp_path / "reports" / "failures"
    _mkfiles(d, 3)
    idle.begin_run()
    try:
        assert bootstrap.trim_failures(str(d), keep=2) == (0, 0)
        assert len(list(d.iterdir())) == 3
        assert "失败截图循环" in idle.pending_names()
    finally:
        idle.end_run()
    idle.flush(background=False)
    assert sorted(p.name for p in d.iterdir()) == ["f1.png", "f2.png"]


def test_trim_traces_deferred_while_running(tmp_path):
    """失败时序证据: 执行期间不删, 结束后目录与单张截图各留最新 N 个"""
    d = tmp_path / "Test_img" / "debug" / "failures"
    d.mkdir(parents=True)
    for i in range(3):
        sub = d / f"用例{i}_trace"
        sub.mkdir()
        os.utime(sub, (2000 + i, 2000 + i))
    idle.begin_run()
    try:
        assert bootstrap.trim_traces(str(d), keep=2) == (0, 0)
        assert len(list(d.iterdir())) == 3
        assert "失败证据循环" in idle.pending_names()
    finally:
        idle.end_run()
    idle.flush(background=False)
    assert sorted(p.name for p in d.iterdir()) == ["用例1_trace", "用例2_trace"]


def test_trim_case_screenshots_deferred_while_running(tmp_path):
    """用例截图循环: 执行期间不删, 结束后只留最新 N 次"""
    shots = tmp_path / "screenshots"
    for i in range(3):
        d = shots / f"甲用例-第{i + 1}次"
        d.mkdir(parents=True)
        os.utime(d, (3000 + i, 3000 + i))
    idle.begin_run()
    try:
        assert bootstrap.trim_case_screenshots(str(shots), case_name="甲用例", keep=1) == (0, 0)
        assert len(list(shots.iterdir())) == 3
        assert "截图循环" in idle.pending_names()
    finally:
        idle.end_run()
    idle.flush(background=False)
    assert sorted(p.name for p in shots.iterdir()) == ["甲用例-第3次"]


def test_cleanup_update_leftovers_deferred_while_running(tmp_path):
    """更新残渣: 执行期间不删(它是"上次更新中断留下的半截文件"), 结束后收"""
    for name in bootstrap._UPDATE_LEFTOVERS:
        (tmp_path / name).write_bytes(b"z")
    idle.begin_run()
    try:
        assert bootstrap.cleanup_update_leftovers(str(tmp_path)) == []
        assert (tmp_path / "_update_download.zip").exists()
        assert "清理更新残留" in idle.pending_names()
    finally:
        idle.end_run()
    idle.flush(background=False)
    for name in bootstrap._UPDATE_LEFTOVERS:
        assert not (tmp_path / name).exists(), name


def test_cleanup_old_backup_deferred_while_running(tmp_path):
    """旧程序备份(.bak 是回滚退路): 执行期间不删, 结束后收"""
    (tmp_path / "AutoTest.exe.bak").write_bytes(b"z")
    (tmp_path / "_update.bat").write_bytes(b"z")
    idle.begin_run()
    try:
        assert bootstrap.cleanup_old_backup(str(tmp_path)) == []
        assert (tmp_path / "AutoTest.exe.bak").exists()
        assert "清理更新残渣" in idle.pending_names()
    finally:
        idle.end_run()
    idle.flush(background=False)
    assert not (tmp_path / "AutoTest.exe.bak").exists()
    assert not (tmp_path / "_update.bat").exists()


# ── 结构性守护: 将来新增的清理不许漏掉闸门 ──

def test_每个清理入口都装了空闲闸门():
    """清理类函数**必须**先问闸门 —— 漏一个, 它就又会在跑用例时抢内存。

    ★ 用源码扫描而不是逐个调用: 新增清理函数时这条会立刻红, 提醒照做
      (与 test_conftest_guard 同一思路)。
    """
    entries = [
        bootstrap.cleanup_update_leftovers,
        bootstrap.cleanup_old_backup,
        bootstrap.prune_debug_artifacts,
        bootstrap.prune_run_artifacts,
        bootstrap.trim_failures,
        bootstrap.trim_traces,
        bootstrap.trim_case_screenshots,
    ]
    missing = [f.__name__ for f in entries
               if "idle.is_busy()" not in inspect.getsource(f)]
    assert not missing, f"这些清理函数没有空闲闸门(执行用例时会抢内存): {missing}"


def test_runworker_用空闲闸门包住整轮执行():
    """闸门的两端必须成对出现在 RunWorker.run 里 —— end 少一次, 清理就永远不做"""
    from gui import runner_thread as rt
    src = inspect.getsource(rt.RunWorker.run)
    assert "idle.begin_run()" in src, "开始执行时没有关闸"
    assert "idle.end_run()" in src, "结束执行时没有开闸(推迟的清理永远不会补做)"
    assert src.index("idle.begin_run()") < src.index("idle.end_run()"), "顺序反了"


# ── 端到端: 真的跑一轮用例, 期间闸门是关的 ──

def test_执行用例期间闸门是关的_结束后自动开(qapp, monkeypatch, tmp_path):
    """跑一轮 RunWorker: 前置/步骤执行期间 is_busy() 为真, 期间调用的清理被推迟,
    整轮结束后自动补做。
    """
    import types

    import uiautomator2 as u2

    from core.excel_report import ExcelReport
    from gui import runner_thread as rt

    case_dir = tmp_path / "Test_cases" / "组甲"
    case_dir.mkdir(parents=True)
    case_file = case_dir / "a.yaml"
    case_file.write_text("module: 组甲\ncases:\n  - name: 冒烟\n    steps:\n"
                         "      - desc: 等一下\n        wait: 0.1\n", encoding="utf-8")

    fake_d = types.SimpleNamespace(
        implicitly_wait=lambda t: None,
        dump_hierarchy=lambda: '<node text="100%"/>',
        screenshot=lambda path=None, format=None: None)
    monkeypatch.setattr(u2, "connect", lambda dev: fake_d)
    monkeypatch.setattr(rt, "ExcelReport",
                        lambda: ExcelReport(path=str(tmp_path / "rep.xlsx")))
    monkeypatch.setattr(rt, "load_config",
                        lambda: {"runner": {"step_interval": 0, "default_timeout": 1,
                                            "click_timeout": 1},
                                 "app": {"package": "p", "name": "x"},
                                 "target_device": "SE3L"})
    monkeypatch.setattr(rt.session, "get_battery_level", lambda d_: 50)

    # 前置里"顺手"调一次封顶清理 —— 模拟真实调用点(启动/关窗/写失败截图时都会调)
    shot_dir = tmp_path / "screenshots"
    for i in range(3):
        d = shot_dir / f"冒烟-第{i + 1}次"
        d.mkdir(parents=True)
        os.utime(d, (3000 + i, 3000 + i))
    seen = {}

    def _prepare(d_, cfg_, items, on_progress=None, should_cancel=None, on_item_done=None):
        seen["busy"] = idle.is_busy()
        bootstrap.trim_case_screenshots(str(shot_dir), case_name="冒烟", keep=1)
        seen["alive_while_running"] = len(list(shot_dir.iterdir()))
        return [{"key": "restart", "desc": "重启 APP", "ok": True, "elapsed": 0.1}]

    monkeypatch.setattr(rt.session, "prepare_items", _prepare)

    w = rt.RunWorker("fake-dev", [str(case_file)],
                     [{"type": "restart", "enabled": True}], 1)
    w.run()

    assert seen["busy"] is True, "执行用例期间闸门没关上"
    assert idle.is_busy() is False, "跑完了闸门还关着(清理就永远不做了)"
    assert seen["alive_while_running"] == 3, "执行期间不该删截图"
    for _ in range(50):                   # 结束后台补做
        if len(list(shot_dir.iterdir())) == 1:
            break
        time.sleep(0.02)
    assert sorted(p.name for p in shot_dir.iterdir()) == ["冒烟-第3次"], \
        "本轮结束后没有补做推迟的截图循环"
