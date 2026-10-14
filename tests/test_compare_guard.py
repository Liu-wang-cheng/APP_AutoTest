# -*- coding: utf-8 -*-
"""compare/diff threshold 越界必须显式报错(防 threshold:1 断言恒真而报告仍绿)。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.actions.asserts import check_threshold


def test_diff_threshold_1_rejected():
    with pytest.raises(ValueError, match="恒"):
        check_threshold(1.0, inverse=True)


def test_diff_threshold_above_1_rejected():
    with pytest.raises(ValueError, match="恒"):
        check_threshold(1.5, inverse=True)


def test_compare_threshold_above_1_rejected():
    with pytest.raises(ValueError, match="恒"):
        check_threshold(1.2, inverse=False)


def test_valid_thresholds_pass():
    check_threshold(0.99, inverse=True)
    check_threshold(0.6, inverse=False)
    check_threshold(0.0, inverse=False)
    check_threshold(1.0, inverse=False)   # compare 允许 =1(严格小于才失败)


# ── 对实际用例文件的静态检查 ──

def _iter_compare_steps():
    """扫 Test_cases/ 下所有用例(**含 APP 组子目录**), 产出 (用例名, 键, 阈值)

    ★ 原来用 `os.listdir(cases_dir)` 只扫**顶层** —— 而真实用例都在
      `Test_cases/<APP组>/` 子目录里(顶层 0 个 yaml), 于是循环体从不执行、
      checked 恒为 0, 这个守护一直空转(实测输出"共检查 0 处 compare/diff 阈值")。
      这里复用 collect_cases —— 它已正确处理组目录与 case_order;
      else 子步骤同样会执行, 一并展开(iter_all_steps)。
    """
    from tests.test_yaml_runner import collect_cases, iter_all_steps
    for module, name, steps, *_ in collect_cases():
        label = f"{module}/{name}"
        for step, _depth in iter_all_steps(steps):
            for key in ("compare", "diff"):
                if key in step:
                    default = 0.99 if key == "diff" else 0.6
                    yield label, key, float(step.get("threshold", default))


def test_所有用例的阈值都合法():
    """用例文件里的 compare/diff 阈值不能越界

    阈值越界会让断言恒真/恒假,而报告里完全看不出来 —— 这是最危险的静默失败,
    所以要在静态检查阶段就拦下来,而不是等真机跑完看到一片绿。
    """
    checked = 0
    for label, key, threshold in _iter_compare_steps():
        try:
            check_threshold(threshold, inverse=(key == "diff"))
        except ValueError as e:
            pytest.fail(f"用例「{label}」的 {key} 阈值非法: {e}")
        checked += 1
    print(f"共检查 {checked} 处 compare/diff 阈值")
    # ★ 防回归: 扫描逻辑一旦又只能看到 0 个用例, 这个守护就等于没有 —— 必须报红,
    #   而不是安静地"通过"(它此前空转了多久没人知道)
    assert checked > 0, (
        "没有扫描到任何 compare/diff 步骤 —— 用例布局变了? "
        "扫描逻辑必须跟着改, 否则这个守护会静默失效")


# ── 对比范围(百分比) + 证据标注(用户 2026-10-14) ──

def _imread_any(path):
    """读图(中文路径安全): 项目惯例 imdecode, cv2.imread 在 Windows 中文路径返回 None
    (CI 真机踩到: runneradmin 的 tmp 路径含「对比」二字 → imread None → 假失败)"""
    import cv2
    import numpy as np
    buf = np.fromfile(path, dtype=np.uint8)
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


class _FrameDev:
    """返回指定帧的设备桩: 帧 = 上半灰(120) 下半白(250), 便于按区域算相似度"""
    serial = "fake"

    def __init__(self, frame):
        self._frame = frame
        self.shot_paths = []

    def screenshot(self, path=None, format=None):
        if path:
            import cv2
            import numpy as np
            buf = np.fromfile(path, dtype=np.uint8) if False else None
            self.shot_paths.append(path)
            # 写真帧(供标注流程读回)
            ok, buf = cv2.imencode(".png", self._frame)
            with open(path, "wb") as f:
                f.write(buf.tobytes())
            return path
        return self._frame.copy()


class _Runner:
    """最小 runner 桩: _execute 后置块会用到的属性"""
    def __init__(self, frame, tmp_path):
        self.d = _FrameDev(frame)
        self._cur_step = {}
        self._cur_step_desc = "对比测试"
        self._pre_click_shot = None
        self.results = []
        self.case_name = "t"
        from core import vision
        self._shot_root = str(tmp_path)

    def _step_shot_path(self, step_desc=""):
        import os
        return os.path.join(self._shot_root, "shots", "step01_对比.png")

    def _annotate_shot(self, path, draw):
        import cv2
        import numpy as np
        buf = np.fromfile(path, dtype=np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        draw(img)
        ok, out = cv2.imencode(".png", img)
        with open(path, "wb") as f:
            f.write(out.tobytes())


def _write_baseline(tmp_path, frame):
    import cv2
    p = tmp_path / "base.png"
    ok, buf = cv2.imencode(".png", frame)
    p.write_bytes(buf.tobytes())
    return str(p)


def test_compare_area_percent_semantics(tmp_path):
    """★ 对比范围按**百分比**(用户 2026-10-14): "0,50 100,100" = 只比下半屏。
    帧: 上半灰下半白, 基准同构但下半被改 → 全屏相似度低, 下半区域相似度高。"""
    import cv2
    import numpy as np
    from core.actions.asserts import compare_screen

    frame = np.full((200, 100, 3), 120, dtype=np.uint8)
    frame[100:, :] = (250, 250, 250)           # 下半白
    base = frame.copy()
    base[150:, :] = (0, 0, 0)                  # 基准: 下 1/4 是黑(差异在 75%~100% 高度)

    runner = _Runner(frame, tmp_path)
    base_path = _write_baseline(tmp_path, base)

    # 全屏(默认中部 10,20-90,80 会含部分黑) → 用明确的小范围避开差异区:
    # "0,0 100,40" = 只比上 40%(全灰区) → 相似度 100%
    compare_screen(runner, base_path, threshold=0.99, region="0%,0% 100%,40%")
    assert runner._pre_click_shot, "证据槽应记录标注帧"


def test_compare_area_default_unchanged(tmp_path):
    """不传 region → 行为同旧(中部区域)"""
    import cv2
    import numpy as np
    from core.actions.asserts import compare_screen

    frame = np.full((200, 100, 3), 120, dtype=np.uint8)
    frame[100:, :] = (250, 250, 250)
    runner = _Runner(frame, tmp_path)
    base_path = _write_baseline(tmp_path, frame.copy())   # 完全一致 → 1.0
    compare_screen(runner, base_path, threshold=1.0)       # 默认区域也应 1.0 → 通过
    assert runner._pre_click_shot


def test_compare_area_invalid_raises(tmp_path):
    """左>=右 / 上>=下 / 非数字 → 明确报错"""
    import numpy as np
    from core.actions.asserts import compare_screen
    frame = np.full((200, 100, 3), 120, dtype=np.uint8)
    runner = _Runner(frame, tmp_path)
    base_path = _write_baseline(tmp_path, frame)
    with pytest.raises(ValueError, match="对比范围"):
        compare_screen(runner, base_path, 0.6, region="90%,10% 10%,80%")
    with pytest.raises(ValueError, match="对比范围"):
        compare_screen(runner, base_path, 0.6, region="abc")


def test_compare_area_pixel_form(tmp_path):
    """★ 双单位: 不带 % = **像素**(108,576 这种落点), 带 % = 百分比(2026-10-14)"""
    import cv2
    import numpy as np
    from core.actions.asserts import compare_screen

    frame = np.full((200, 100, 3), 120, dtype=np.uint8)
    runner = _Runner(frame, tmp_path)
    base_path = _write_baseline(tmp_path, frame)
    compare_screen(runner, base_path, threshold=0.99, region="10,20 90,180")   # 像素
    assert runner._pre_click_shot
    img = _imread_any(runner._pre_click_shot)
    assert tuple(img[20, 50]) == (0, 200, 255), tuple(img[20, 50])   # 边框在 y=20(像素)


def test_compare_area_mixed_units_rejected(tmp_path):
    """% 与不带 % 混用 → 明确报错(避免"一半像素一半百分比"的歧义)"""
    import numpy as np
    from core.actions.asserts import compare_screen
    frame = np.full((200, 100, 3), 120, dtype=np.uint8)
    runner = _Runner(frame, tmp_path)
    base_path = _write_baseline(tmp_path, frame)
    with pytest.raises(ValueError, match="单位"):
        compare_screen(runner, base_path, 0.6, region="10%,20 90,80")


def test_compare_area_annotates_evidence(tmp_path):
    """标注: 证据图上要画出范围矩形(边框色被写入) + 文字(sim/thr)"""
    import cv2
    import numpy as np
    from core.actions.asserts import compare_screen

    frame = np.full((200, 100, 3), 120, dtype=np.uint8)
    runner = _Runner(frame, tmp_path)
    base_path = _write_baseline(tmp_path, frame)
    compare_screen(runner, base_path, threshold=0.99, region="10%,10% 90%,90%")
    shot = runner._pre_click_shot
    assert shot and _imread_any(shot) is not None
    img = _imread_any(shot)
    # 范围矩形边框画在 10%~90% 位置: 100x200 帧 → x=10..90, y=20..180, 边框色 (0,200,255) BGR
    assert tuple(img[20, 50]) == (0, 200, 255), tuple(img[20, 50])
    assert tuple(img[180, 50]) == (0, 200, 255), tuple(img[180, 50])
