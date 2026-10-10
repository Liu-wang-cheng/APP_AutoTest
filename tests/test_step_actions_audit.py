# -*- coding: utf-8 -*-
"""清点所有"测试步骤操作": UI 能不能识别/添加、引擎有没有执行器、字段能不能往返。

★ 为什么加这一套(2026-10-10): 用户发现「延时等待」步骤在界面上显示「未知」——
  根因是它压根不是动作: 快捷按钮写的是 `{"desc": "延时等待", "wait": 10}`, 而 `wait`
  是"本步后等待"的元键(META), 卡片在 step 里找不到任何动作键 → chip 显示「未知」、
  字段/校验也认不出来。这类"schema 里有、引擎里没有"或"按钮造出来的步骤认不出"的
  问题, 单看某一处代码都发现不了 —— 得挨个动作过一遍。
"""
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, ROOT)

import core.actions                                              # noqa: E402,F401
from core import registry as reg                                 # noqa: E402
from gui import schema                                           # noqa: E402

#: 与主动作共存于一步、由 runner._execute 单独处理的键(不是独立动作)
COEXIST_KEYS = {"grab", "match", "screenshot", "wait_for"}
EXECUTED = {a.key for a in reg.dispatch_order()}


def _renderer_types():
    """从字段渲染器源码里抠出它支持的字段类型。

    ★ 不手写白名单: 我手写过一次, 当场就把 `template` 漏成"不支持"(假阳性)。
    """
    src = open(os.path.join(ROOT, "gui", "main_window.py"), encoding="utf-8").read()
    i = src.index("def _make_field_widget(")
    body = src[i:]
    j = body.find("\ndef ", 10)
    body = body[:j] if j > 0 else body
    # text 是那个函数的默认分支, group 由 _add_group_field 单独展开
    return set(re.findall(r'if t == "(\w+)"', body)) | {"text", "group"}


def _action_keys():
    return [a["key"] for a in schema.ACTIONS]


# ── ① 每个动作: 新建出来的步骤 UI 认得出、引擎跑得动 ──

def test_every_action_step_is_recognizable():
    """★ 卡片靠"step 里有 key ∈ ACTION_BY_KEY"认动作 —— 认不出就显示「未知」"""
    bad = []
    for key in _action_keys():
        step = schema.new_step(key)
        if key not in step or key not in schema.ACTION_BY_KEY:
            bad.append((key, sorted(step)))
    assert not bad, f"这些动作新建出来的步骤, 卡片会显示「未知」: {bad}"


def test_every_action_has_an_executor():
    """★ schema 里有、引擎里没有 = 用户能加、运行时静默什么都不做"""
    missing = [k for k in _action_keys() if k not in EXECUTED and k not in COEXIST_KEYS]
    assert not missing, f"这些动作没有执行器(加了也不会执行): {missing}"


def test_every_action_summary_is_not_raw_dict():
    bad = []
    for key in _action_keys():
        s = schema.step_summary(schema.new_step(key))
        if s.strip().startswith("{"):
            bad.append((key, s))
    assert not bad, f"摘要退化成原始字典: {bad}"


# ── ② 用户真正会点的入口: 快捷条 ──

def test_quick_actions_produce_recognizable_steps():
    """快捷条上的每个按钮都要造出"卡片认得出"的步骤(延时按钮踩过这个坑)"""
    from gui import main_window as mw
    bad = []
    for key in mw.QUICK_ACTIONS:
        step = schema.new_step(key)
        if step.get(_only_action_key(step)) is None or \
                _only_action_key(step) not in schema.ACTION_BY_KEY:
            bad.append((key, step))
    assert not bad, f"快捷条造出的步骤卡片认不出: {bad}"


def _only_action_key(step):
    for k in step:
        if k in schema.ACTION_BY_KEY:
            return k
    return None


# ── ③ 字段: 类型受支持 / 同一动作内不重名 ──

def test_field_types_are_supported_by_the_renderer():
    supported = _renderer_types()
    assert supported, "没能从渲染器里抠出任何字段类型(正则/函数名变了?)"
    bad = [(a["key"], f["key"], f["type"])
           for a in schema.ACTIONS for f in a["fields"] if f["type"] not in supported]
    assert not bad, f"这些字段类型渲染器不认(卡片会显示异常): {bad}"


def test_no_duplicate_field_keys_in_one_action():
    bad = []
    for a in schema.ACTIONS:
        keys = [f["key"] for f in a["fields"]]
        dup = {k for k in keys if keys.count(k) > 1}
        if dup:
            bad.append((a["key"], sorted(dup)))
    assert not bad, f"同一动作里字段 key 重复(编辑会互相覆盖): {bad}"


def test_serialize_step_keeps_the_action_key():
    bad = []
    for key in _action_keys():
        out = schema.serialize_step(dict(schema.new_step(key)))
        if key not in out:
            bad.append((key, out))
    assert not bad, f"序列化后动作键丢了: {bad}"


# ── ④ 具体那条: 「延时等待」必须是真动作 ──

def test_sleep_step_is_a_real_action():
    """延时等待: chip 显示「延时等待」、摘要是秒数、执行走可打断的 runner._sleep"""
    a = schema.ACTION_BY_KEY.get("sleep")
    assert a and a["label"] == "延时等待", "延时等待不是注册动作(界面会显示「未知」)"
    step = schema.new_step("sleep")
    assert step.get("sleep", 0) > 0, "新建的延时步骤没有默认秒数"
    assert schema.step_summary(step) == "10", schema.step_summary(step)
    assert "sleep" in EXECUTED, "延时等待没有执行器"


def test_sleep_action_waits_via_cancel_aware_sleep(monkeypatch):
    """执行延时必须走 runner._sleep(可被打断), 不是 time.sleep(停不下来)"""
    from core.actions import timer_ops
    slept = []

    class _R:
        def _sleep(self, s):
            slept.append(s)

    timer_ops.do_sleep(_R(), {"sleep": 2.5, "desc": "延时等待"})
    assert slept == [2.5], slept
    # 0 秒不睡; 非数字要报错而不是静默变成 0
    timer_ops.do_sleep(_R(), {"sleep": 0})
    assert slept == [2.5], slept
    with pytest.raises(ValueError, match="不是数字"):
        timer_ops.do_sleep(_R(), {"sleep": "三秒"})


def test_legacy_bare_wait_step_still_works():
    """老用例里那种"只有 wait 的步骤"仍要能摘要出「延时等待 N 秒」, 不能崩"""
    step = {"desc": "延时等待", "wait": 10}
    assert "延时等待" in schema.step_summary(step)
