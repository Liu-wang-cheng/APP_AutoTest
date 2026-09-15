# -*- coding: utf-8 -*-
"""失败时的时序证据(环形缓冲)

现在失败只存一张截图(_failure_screenshot),那是一张"结果照"——
看不到失败前那几步页面是怎么变的。而失败诊断真正需要的是时序:
上一步在哪个页面、弹窗是什么时候出现的、哪一步开始偏的。

这里把最近 N 步的描述+截图留在内存里(JPEG 编码,避免几十 MB 的原始位图),
只在失败时落盘。落盘的 steps.txt 就是后续接 AI 归因时的输入格式。
"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.action_runner import ActionRunner

SCREEN = np.full((120, 80, 3), 200, dtype=np.uint8)


class FakeD:
    serial = "fake"
    fail_screenshot = False

    def screenshot(self, path=None, format=None):
        if FakeD.fail_screenshot:
            raise RuntimeError("device offline")
        return SCREEN.copy()


def make_runner(limit=3):
    r = ActionRunner.__new__(ActionRunner)
    r.d = FakeD()
    r.case_name = "用例A"
    r._trace = []
    r._trace_limit = limit
    r.store = {}
    return r


class TestTraceBuffer:
    def test_记录步骤描述与通过状态(self):
        r = make_runner()
        r._trace_capture("点击开始清扫", True)
        r._trace_capture("确认清扫中", False)
        assert [x["desc"] for x in r._trace] == ["点击开始清扫", "确认清扫中"]
        assert [x["passed"] for x in r._trace] == [True, False]

    def test_只保留最近N步(self):
        r = make_runner(limit=3)
        for i in range(6):
            r._trace_capture(f"步骤{i}", True)
        assert len(r._trace) == 3
        assert [x["desc"] for x in r._trace] == ["步骤3", "步骤4", "步骤5"]

    def test_limit为0时不记录(self):
        r = make_runner(limit=0)
        r._trace_capture("步骤", True)
        assert r._trace == []

    def test_截图失败不中断记录(self):
        """设备掉线时截不到图,但步骤描述仍要留下"""
        r = make_runner()
        FakeD.fail_screenshot = True
        try:
            r._trace_capture("点击开始清扫", True)
        finally:
            FakeD.fail_screenshot = False
        assert len(r._trace) == 1
        assert r._trace[0]["desc"] == "点击开始清扫"
        assert r._trace[0]["shot"] is None


class TestTraceDump:
    def test_落盘生成截图和steps文件(self, tmp_path):
        r = make_runner()
        r._trace_capture("步骤一", True)
        r._trace_capture("步骤二", False)

        out = r._dump_trace(1, out_root=str(tmp_path))

        assert out, "落盘应返回目录"
        files = sorted(os.listdir(out))
        assert "steps.txt" in files
        assert any(f.endswith(".jpg") for f in files)

    def test_steps文件含描述与通过状态(self, tmp_path):
        r = make_runner()
        r._trace_capture("点击开始清扫", True)
        r._trace_capture("确认清扫中", False)

        out = r._dump_trace(1, out_root=str(tmp_path))
        text = open(os.path.join(out, "steps.txt"), encoding="utf-8").read()

        assert "点击开始清扫" in text
        assert "确认清扫中" in text
        assert "PASS" in text
        assert "FAIL" in text

    def test_步骤顺序与截图文件对应(self, tmp_path):
        r = make_runner()
        r._trace_capture("第一步", True)
        r._trace_capture("第二步", True)

        out = r._dump_trace(1, out_root=str(tmp_path))
        text = open(os.path.join(out, "steps.txt"), encoding="utf-8").read()
        lines = [l for l in text.splitlines() if l.strip()]
        assert len(lines) == 2

        # 每行引用的截图文件必须真实存在
        for line in lines:
            parts = line.split("\t")
            shot = parts[-1].strip()
            assert shot, f"步骤行缺少截图引用: {line}"
            assert os.path.exists(os.path.join(out, shot)), f"截图不存在: {shot}"

    def test_空缓冲不落盘也不报错(self, tmp_path):
        r = make_runner()
        assert r._dump_trace(0, out_root=str(tmp_path)) == ""

    def test_未初始化属性时不报错(self, tmp_path):
        """__new__ 构造的 ActionRunner(如 gui/test_gui.py)没有 _trace 字段

        run_steps 是公开入口,GUI 会直接调,不能因为少个初始化字段就崩。
        """
        r = ActionRunner.__new__(ActionRunner)
        r.d = FakeD()
        r.case_name = ""
        r.store = {}

        r._trace_capture("步骤", True)          # 不应抛异常

        assert r._trace[0]["desc"] == "步骤"
        assert r._dump_trace(0, out_root=str(tmp_path)) != ""

    def test_截图缺失时仍写出步骤行(self, tmp_path):
        """截不到图的步骤也要留痕,不能整行丢掉"""
        r = make_runner()
        FakeD.fail_screenshot = True
        try:
            r._trace_capture("截不到图的一步", True)
        finally:
            FakeD.fail_screenshot = False

        out = r._dump_trace(0, out_root=str(tmp_path))
        text = open(os.path.join(out, "steps.txt"), encoding="utf-8").read()
        assert "截不到图的一步" in text
