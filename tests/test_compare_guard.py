# -*- coding: utf-8 -*-
"""_do_compare / diff 断言的阈值合法性校验

背景: 用例 YAML 里写 `diff: xxx.png` + `threshold: 1` 时,
similarity 值域是 [0,1], `similarity > threshold` 即 `1.0 > 1` 恒为 False,
断言永不失败——检查项形同虚设,而且报告里还是绿的。

这类配置错误必须显式报错,不能静默通过。
"""
import os
import sys

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.action_runner import ActionRunner
from common.driver import BASE_DIR, YamlFileError, load_yaml_file

CASES_DIR = os.path.join(BASE_DIR, "Test_cases")


def make_runner(screen):
    """构造一个不连设备的 ActionRunner,screenshot 返回给定图像"""
    class FakeD:
        serial = "fake"

        def screenshot(self, path=None, format=None):
            return screen

    r = ActionRunner.__new__(ActionRunner)
    r.d = FakeD()
    r.case_name = "threshold_guard"
    r._last_compare_msg = None
    return r


SCREEN = np.zeros((100, 100, 3), dtype=np.uint8)


@pytest.fixture
def identical_baseline(tmp_path):
    """与 SCREEN 完全相同的基准图 —— 这种情况下 diff 必须判定"无变化" """
    base = tmp_path / "base.png"
    Image.fromarray(SCREEN).save(base)
    return str(base)


class TestInverseThresholdGuard:
    """diff 模式(inverse): 阈值 >= 1 会让断言恒真,必须拒绝

    用完全相同的图做基准: 修复前 similarity=1.0, `1.0 > 1` 为 False,
    断言静默通过(测试报 DID NOT RAISE); 修复后应抛 ValueError。
    """

    def test_阈值等于1_报ValueError(self, identical_baseline):
        r = make_runner(SCREEN)
        with pytest.raises(ValueError, match="threshold"):
            r._do_compare(identical_baseline, 1, inverse=True)

    def test_阈值等于1点0_报ValueError(self, identical_baseline):
        r = make_runner(SCREEN)
        with pytest.raises(ValueError, match="threshold"):
            r._do_compare(identical_baseline, 1.0, inverse=True)

    def test_阈值大于1_报ValueError(self, identical_baseline):
        r = make_runner(SCREEN)
        with pytest.raises(ValueError, match="threshold"):
            r._do_compare(identical_baseline, 1.5, inverse=True)

    def test_报错信息说明原因(self, identical_baseline):
        """错误信息要能直接定位问题,而不是只说参数非法"""
        r = make_runner(SCREEN)
        with pytest.raises(ValueError) as ei:
            r._do_compare(identical_baseline, 1, inverse=True)
        msg = str(ei.value)
        assert "1" in msg
        assert "永不" in msg or "恒" in msg or "永远" in msg


class TestValidThresholdStillWorks:
    """合法阈值不受影响"""

    def test_阈值0点99_全同图_无变化_应失败(self, tmp_path):
        """基准图与当前屏完全相同 → diff 判定"几乎无变化" → AssertionError"""
        base = tmp_path / "base.png"
        Image.fromarray(SCREEN).save(base)

        r = make_runner(SCREEN)
        with pytest.raises(AssertionError, match="几乎无变化"):
            r._do_compare(str(base), 0.99, inverse=True)

    def test_阈值0点99_有变化_应通过(self, tmp_path):
        base = tmp_path / "base.png"
        Image.fromarray(SCREEN).save(base)

        changed = SCREEN.copy()
        changed[30:70, 30:70] = 255  # 中间 40x40 区域变白

        r = make_runner(changed)
        r._do_compare(str(base), 0.99, inverse=True)  # 不应抛异常

    def test_compare模式_阈值1仍合法(self, tmp_path):
        """非 inverse 的 compare: similarity < threshold 才失败,阈值 1 是合法的严格断言"""
        base = tmp_path / "base.png"
        Image.fromarray(SCREEN).save(base)

        r = make_runner(SCREEN)
        r._do_compare(str(base), 1, inverse=False)  # 完全相同 → 不应抛异常

    def test_compare模式_阈值大于1_报ValueError(self, identical_baseline):
        """镜像问题: similarity < threshold 恒真 → 断言永不通过,同为配置错误"""
        r = make_runner(SCREEN)
        with pytest.raises(ValueError, match="threshold"):
            r._do_compare(identical_baseline, 1.5, inverse=False)


def _iter_steps(steps):
    """递归遍历步骤(含 if/else 分支),产出每个 step dict"""
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        yield step
        for key in ("else",):
            branch = step.get(key)
            if isinstance(branch, list):
                yield from _iter_steps(branch)


class TestCaseYamlThresholdLint:
    """守住用例库: 恒真/恒假的阈值一旦写回 YAML,报告里是绿的但什么都没检查"""

    def test_所有用例的阈值都有效(self):
        bad, unreadable = [], []
        for fname in sorted(os.listdir(CASES_DIR)):
            if not fname.endswith(".yaml"):
                continue
            try:
                data = load_yaml_file(os.path.join(CASES_DIR, fname))
            except YamlFileError as e:
                unreadable.append(str(e))
                continue
            for case in data.get("cases", []):
                for step in _iter_steps(case.get("steps")):
                    th = step.get("threshold")
                    if th is None:
                        continue
                    if "diff" in step and th >= 1:
                        bad.append(f"{fname}::{case['name']} diff threshold={th} (恒为通过)")
                    if "compare" in step and th > 1:
                        bad.append(f"{fname}::{case['name']} compare threshold={th} (恒为失败)")

        problems = []
        if bad:
            problems.append("无效阈值:\n    " + "\n    ".join(bad))
        if unreadable:
            problems.append("用例文件不可读:\n    " + "\n    ".join(unreadable))
        assert not problems, "\n  ".join(problems)
