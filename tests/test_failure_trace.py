# -*- coding: utf-8 -*-
"""失败时的时序证据(环形缓冲)。

失败只留一张截图是不够的 —— 那是一张"结果照",看不到失败前那几步页面是怎么
变的。而失败诊断真正需要的是时序:上一步在哪个页面、弹窗什么时候出现的、
哪一步开始偏的。这里验证最近 N 步的描述+截图留在内存,只在失败时落盘。
"""
import os
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.trace import TraceRecorder

SCREEN = np.full((120, 80, 3), 200, dtype=np.uint8)


class FakeDevice:
    serial = "fake"
    fail = False

    def screenshot(self, path=None, format=None):
        if FakeDevice.fail:
            raise RuntimeError("device offline")
        return SCREEN.copy()


@pytest.fixture(autouse=True)
def _reset_fail():
    FakeDevice.fail = False
    yield
    FakeDevice.fail = False


def make(limit=3, case_name="用例A"):
    return TraceRecorder(case_name=case_name, limit=limit)


def test_记录描述与通过状态():
    t = make()
    t.capture(FakeDevice(), "点击开始清扫", True)
    t.capture(FakeDevice(), "确认清扫中", False)
    assert [x["desc"] for x in t._trace] == ["点击开始清扫", "确认清扫中"]
    assert [x["passed"] for x in t._trace] == [True, False]


def test_只保留最近N步():
    t = make(limit=3)
    for i in range(6):
        t.capture(FakeDevice(), f"步骤{i}", True)
    assert len(t._trace) == 3
    assert [x["desc"] for x in t._trace] == ["步骤3", "步骤4", "步骤5"]


def test_limit为0时不记录():
    t = make(limit=0)
    t.capture(FakeDevice(), "步骤", True)
    assert t._trace == []


def test_截图失败不中断记录():
    """设备掉线时截不到图,但步骤描述仍要留下 —— 否则失败现场一无所获"""
    t = make()
    FakeDevice.fail = True
    t.capture(FakeDevice(), "点击开始清扫", True)
    assert len(t._trace) == 1
    assert t._trace[0]["desc"] == "点击开始清扫"
    assert t._trace[0]["shot"] is None


def test_落盘生成steps和jpg(tmp_path):
    t = make()
    t.capture(FakeDevice(), "步骤一", True)
    t.capture(FakeDevice(), "步骤二", False)
    out = t.dump(FakeDevice(), 1, out_root=str(tmp_path))
    assert out, "落盘应返回目录"
    files = os.listdir(out)
    assert "steps.txt" in files
    assert any(f.endswith(".jpg") for f in files)


def test_steps文件含描述与通过状态(tmp_path):
    t = make()
    t.capture(FakeDevice(), "点击开始清扫", True)
    t.capture(FakeDevice(), "确认清扫中", False)
    out = t.dump(FakeDevice(), 1, out_root=str(tmp_path))
    text = open(os.path.join(out, "steps.txt"), encoding="utf-8").read()
    assert "点击开始清扫" in text
    assert "确认清扫中" in text
    assert "PASS" in text
    assert "FAIL" in text


def test_每行引用的截图真实存在(tmp_path):
    t = make()
    t.capture(FakeDevice(), "第一步", True)
    t.capture(FakeDevice(), "第二步", True)
    out = t.dump(FakeDevice(), 1, out_root=str(tmp_path))
    text = open(os.path.join(out, "steps.txt"), encoding="utf-8").read()
    lines = [l for l in text.splitlines() if l.strip()]
    assert len(lines) == 2
    for line in lines:
        shot = line.split("\t")[-1].strip()
        assert shot, f"步骤行缺少截图引用: {line}"
        assert os.path.exists(os.path.join(out, shot)), f"截图不存在: {shot}"


def test_空缓冲不落盘也不报错(tmp_path):
    t = make()
    assert t.dump(FakeDevice(), 0, out_root=str(tmp_path)) == ""


def test_截图缺失时仍写出步骤行(tmp_path):
    """截不到图的步骤也要留痕,不能整行丢掉"""
    t = make()
    FakeDevice.fail = True
    t.capture(FakeDevice(), "截不到图的一步", True)
    out = t.dump(FakeDevice(), 0, out_root=str(tmp_path))
    text = open(os.path.join(out, "steps.txt"), encoding="utf-8").read()
    assert "截不到图的一步" in text


def test_用例名进文件名避免互相覆盖(tmp_path):
    """不同用例的失败证据不能互相覆盖"""
    t1 = TraceRecorder(case_name="用例A", limit=3)
    t1.capture(FakeDevice(), "步骤", True)
    out1 = t1.dump(FakeDevice(), 0, out_root=str(tmp_path))

    t2 = TraceRecorder(case_name="用例B", limit=3)
    t2.capture(FakeDevice(), "步骤", True)
    out2 = t2.dump(FakeDevice(), 0, out_root=str(tmp_path))

    assert out1 != out2
    assert "用例A" in out1 and "用例B" in out2
