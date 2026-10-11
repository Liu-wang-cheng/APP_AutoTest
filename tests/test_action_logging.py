# -*- coding: utf-8 -*-
"""动作执行的**日志完整性**守护(用户反馈: "坐标点击时是没有日志的", 2026-10-11)。

★ 为什么值得单独一套: 真机排查全靠 `reports/test.log` —— 动作执行了却不在日志里,
  事后只看到"用例在第 N 步失败", 看不到那一步到底点了谁、点在哪。
  加入这套时的状态很不均衡: set_time / 分区 / 合并 / 开关 这些复杂动作写得很全,
  而 click(文本/坐标) / long_click / swipe / input / back / find_click / assert /
  wait_for / if 这些**最常用**的反而一句都没有。

这里逐个动作跑一遍, 断言日志里能读出"做了什么、落在哪"。
(复杂动作 set_time/room_zones/spot_clean… 的日志由它们各自的测试负责, 不在这里重复。)
"""
import logging
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import core.actions                                              # noqa: E402,F401
from core import vision                                          # noqa: E402
from core.runner import ActionRunner                             # noqa: E402


class _El:
    def __init__(self, present=True, clickable=True, text="", bounds=None):
        self._present = present
        self._clickable = clickable
        self._text = text
        self._bounds = bounds or {"left": 100, "top": 300, "right": 300, "bottom": 340}
        self.clicked = False

    def exists(self, timeout=1):
        return self._present

    def click(self):
        self.clicked = True

    def set_text(self, t):
        self._text = t

    @property
    def info(self):
        return {"clickable": self._clickable, "text": self._text, "bounds": self._bounds}


class Device:
    """极简设备桩: 页面文本固定几条(避免判断类走 OCR 兜底)"""
    serial = "fake"

    def __init__(self, texts=("确认", "安静", "清扫中", "地图编辑", "开始清扫")):
        self.texts = set(texts)
        self.calls = []

    def dump_hierarchy(self):
        rows = [f'<node text="{t}" bounds="[100,{300 + i * 40}][400,{340 + i * 40}]"/>'
                for i, t in enumerate(sorted(self.texts))]
        rows += [f'<node text="填充{i}" bounds="[100,{800 + i * 40}][400,{840 + i * 40}]"/>'
                 for i in range(5)]          # 保持"可见文本 ≥5 条", 判断类不走 OCR
        return "<hierarchy>" + "".join(rows) + "</hierarchy>"

    def __call__(self, **kw):
        q = kw.get("textContains") or kw.get("text") or kw.get("description") or ""
        hit = bool(q) and any(q in t for t in self.texts)
        return _El(present=hit, text=q)

    def click(self, x, y):
        self.calls.append(("click", x, y))

    def press(self, k):
        self.calls.append(("press", k))

    def swipe(self, *a, **k):
        self.calls.append(("swipe",) + a)

    def window_size(self):
        return (1080, 1920)

    def screenshot(self, path=None, format=None):
        return np.zeros((1920, 1080, 3), dtype=np.uint8)


@pytest.fixture
def logs():
    """挂一个捕获 handler 到项目日志器(conftest 已把日志文件重定向到临时文件)"""
    from core.logger import get_logger

    recs = []

    class _H(logging.Handler):
        def emit(self, record):
            recs.append(record.getMessage())

    h = _H(level=logging.INFO)
    lg = get_logger()
    old_level = lg.level
    lg.setLevel(logging.INFO)
    lg.addHandler(h)
    try:
        yield recs
    finally:
        lg.removeHandler(h)
        lg.setLevel(old_level)


@pytest.fixture
def runner():
    r = ActionRunner(Device(), {"step_interval": 0, "default_timeout": 1,
                                "click_timeout": 1}, case_name="t")
    r._sleep = lambda s: None
    return r


def _has(logs, *parts):
    return any(all(p in m for p in parts) for m in logs)


# ── 点击的三种形态都要留痕(用户就是从坐标点击发现的问题) ──

def test_coordinate_click_logs_the_point(runner, logs):
    runner._execute({"click": "531,1900"})
    assert _has(logs, "[点击]", "531", "1900"), f"坐标点击没有日志: {logs}"


def test_text_click_logs_target_and_position(runner, logs):
    runner._execute({"click": "确认"})
    assert _has(logs, "[点击]", "确认"), f"文本点击没有日志: {logs}"
    assert _has(logs, "@", "("), f"文本点击没记下落在哪: {logs}"


def test_template_click_logs_match_position(runner, logs, monkeypatch):
    monkeypatch.setattr(vision, "find_in_gray", lambda s, t: (300, 400))
    monkeypatch.setattr(vision, "resolve_template", lambda n: n)
    runner._execute({"click": "开始清扫.png"})
    assert _has(logs, "点击", "开始清扫.png"), f"模板点击没有日志: {logs}"


def test_row_fallback_click_logs(runner, logs, monkeypatch):
    """文字节点点不动 → 点整行的回退路径也要留痕(否则日志里看不出是回退点的)"""
    d = runner.d
    monkeypatch.setattr(d, "dump_hierarchy",
                        lambda: '<node content-desc="Card_Row" bounds="[10,10][500,90]"/>'
                                '<node text="确认"/>')
    monkeypatch.setattr(type(d), "__call__", lambda self, **kw: _El(
        present=True, clickable=not (kw.get("textContains") == "确认"), text="确认"))
    runner._execute({"click": "确认"})
    assert _has(logs, "[点击]", "整行"), f"整行回退没有日志: {logs}"


# ── 其它基础交互 ──

def test_long_click_logs_what_and_how_long(runner, logs):
    runner._execute({"long_click": "100,200", "duration": 3})
    assert _has(logs, "[长按]", "100", "3"), f"长按没有日志: {logs}"


@pytest.mark.parametrize("value,expect", [("82,2101,82,1601", "2101"), ("left", "left")])
def test_swipe_logs_path_or_direction(runner, logs, value, expect):
    runner._execute({"swipe": value})
    assert _has(logs, "[滑动]", expect), f"滑动没有日志: {logs}"


def test_input_logs_the_text(runner, logs):
    runner._execute({"input": "客厅"})
    assert _has(logs, "[输入]", "客厅"), f"输入没有日志: {logs}"


def test_back_logs(runner, logs):
    runner._execute({"back": True})
    assert _has(logs, "[返回]"), f"返回没有日志: {logs}"


def test_find_click_logs_the_hit(runner, logs):
    runner._execute({"find_click": "不存在,确认"})
    assert _has(logs, "[find_click]", "确认"), f"find_click 没有日志: {logs}"


# ── 判断类的"通过"也要留痕(以前只有失败才有诊断) ──

def test_assert_logs_the_matched_text(runner, logs):
    runner._execute({"assert": "清扫中"})
    assert _has(logs, "[断言]", "清扫中"), f"断言命中没有日志: {logs}"


def test_assert_logs_what_element_actually_matched(runner, logs):
    """★ 断言必须记下**命中的那个元素**(类名/文本/描述/坐标)。

    断言走 `textContains` + content-desc 兜底, 页面上**按钮的无障碍描述**同样算命中 ——
    真机上出现过「断言: 暂停」瞬间通过, 其实是匹配到了暂停**图标按钮**的描述(2026-10-11)。
    只记查询值的话这种假通过永远看不出来。
    """
    runner._execute({"assert": "清扫中"})
    assert _has(logs, "[断言]", "元素[", "text="), f"没记下命中元素长什么样: {logs}"


def test_failed_assert_is_logged_with_the_step_name(runner, logs):
    """失败也要有日志(经 run_steps 的 FAIL诊断), 且带上步骤名 —— 否则报告里只有一行红

    ★ run_steps 是"记录失败并返回 False", 不抛异常(它是主循环), 别写成 pytest.raises
    """
    assert runner.run_steps([{"desc": "判断是否处于暂停状态",
                              "assert": "不会有这个文本"}]) is False
    assert _has(logs, "FAIL诊断", "判断是否处于暂停状态"), f"断言失败没留日志: {logs}"


def test_wait_for_logs_when_satisfied(runner, logs):
    runner._execute({"wait_for": "安静"})
    assert _has(logs, "[wait_for]", "安静"), f"wait_for 没有日志: {logs}"


def test_if_logs_which_branch_it_took(runner, logs):
    runner._execute({"if": "清扫中", "else": [{"desc": "不该走", "click": "确认"}]})
    assert _has(logs, "[if", "成立"), f"if 分支没有日志: {logs}"
    logs.clear()
    runner._execute({"if": "不存在的文本", "else": [{"desc": "走 else", "click": "确认"}]})
    assert _has(logs, "[if", "不成立"), f"if 走 else 没有日志: {logs}"
