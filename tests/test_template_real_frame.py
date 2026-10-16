# -*- coding: utf-8 -*-
"""模板匹配: 用**真机截图当固定素材**验, 不连真机(用户 2026-10-16 要求)。

用户原话: "这里关于模板匹配的测试用例你要用截图去测而不是调真机, 确保不会因为真机状态
变化而失败" —— 所以这里的一切都跑在 `fixtures/三星_设备页_顶部.png` 上:

  素材来源: 2026-10-16 三星(SmartThings)插件设备页, 1080x1920, 设备处于「正在等待」——
            顶部大卡片右侧**两个圆盘同时在**: ▶(启动) 与 ⌂(返回回充)。
            裁的是 y 250..960 / x 250..1050(800x710), 两个圆盘都完整在内。
  真值坐标: 从**像素**量出来的圆盘中心(白色圆盘连通域质心), 见 file 注释;
            换算到 fixture 坐标系就是下面的 PLAY_XY / HOME_XY。

为什么要这么测(而不是"连着模拟器现截一张再比"): 真机/模拟器的状态随时会变(清扫中 /
暂停 / 正在等待 / 充电中), 同一个模板在不同状态下本来就该给出不同结果 —— 那种测试
会莫名其妙地红/绿。素材固定之后, 断言的是**引擎的性质**:
  · 原生尺度的模板必须 1.000 命中它自己那个按钮(±几像素), 且尺度就是 1.00;
  · 两个按钮不会互相顶替(区分度够大 —— "点错按钮"就是这么来的);
  · 尺度不对的模板(用户在别的尺度上截的)必须被校验拦下来。
"""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

cv2 = pytest.importorskip("cv2")

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "三星_设备页_顶部.png"
#: 圆盘中心(fixture 坐标系。原始截图里是 (827,335)/(965,334), fixture 原点在 (250,250))
PLAY_XY = (577, 85)
HOME_XY = (715, 84)
DISC = 92                      # 圆盘直径(实测量出来的 92x92)


def _frame():
    return cv2.imdecode(np.fromfile(str(FIXTURE), dtype=np.uint8), cv2.IMREAD_COLOR)


def _disc(frame, xy):
    """按真值中心从素材里抠出整个圆盘 —— 这就是"用「裁剪模板」在该设备上裁出来"的样子"""
    cx, cy = xy
    h = DISC // 2
    return frame[cy - h:cy + h, cx - h:cx + h].copy()


def _gray(img):
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img


def test_fixture_is_the_two_button_frame():
    """素材自检: 两个圆盘真的在里面(免得素材被换成别的画面后, 下面几条变成"测空气")"""
    frame = _frame()
    assert frame.shape[:2] == (710, 800), frame.shape
    for xy, name in ((PLAY_XY, "▶"), (HOME_XY, "⌂")):
        disc = _disc(frame, xy)
        # 圆盘直径 92, 内接正方形占 π/4≈0.785; 再扣掉图标本身的黑色像素(⌂ 图标较大,
        # 实测白色占比 0.59) —— 门槛取 0.5 就足够区分"圆盘在这儿"和"裁到别处了"
        white = float((_gray(disc) > 235).mean())
        assert white > 0.5, f"{name} 圆盘应该是大片白({name} 处白色占比 {white:.2f})"


def test_native_scale_template_hits_its_own_button(tmp_path):
    """★ 原生尺度(整盘 92px)的模板: 1.000 命中自己那个按钮, 尺度就是 1.00

    这正是「裁剪模板」的用法 —— 在该设备的当前画面上、把整个按钮盘裁进去。
    """
    from core import imgio, vision
    frame = _frame()
    gray = _gray(frame)
    for xy, name in ((PLAY_XY, "▶启动"), (HOME_XY, "⌂回充")):
        p = tmp_path / f"{name}.png"
        # ★ 必须走 imgio.imwrite: 文件名是中文/符号, cv2.imwrite 在 Windows 上按 ANSI
        #   路径开文件, 非 UTF-8 控制台(CI runner 就是 cp1252)下**静默写不进去**,
        #   随后 find_in_gray 报 FileNotFoundError(2026-10-16 CI 上实测)
        assert imgio.imwrite(str(p), _disc(frame, xy)), p
        info = {}
        pos = vision.find_in_gray(gray, str(p), detail=info)
        assert pos is not None, f"{name}: 原生尺度模板必须能命中"
        assert abs(pos[0] - xy[0]) <= 6 and abs(pos[1] - xy[1]) <= 6, \
            f"{name}: 应命中 ({xy[0]},{xy[1]}), 实际 {pos}"
        assert info["score"] >= 0.95, (name, info)
        assert info["scale"] == pytest.approx(1.0, abs=0.01), \
            f"{name}: 原生尺度模板不该靠缩放命中: {info}"


def test_two_buttons_do_not_steal_each_other(tmp_path):
    """★ 两个按钮**互相不顶替** —— "点错按钮"的根源就是这里区分度不够。

    判据用「裁剪模板」那套(validate_template): 最佳与次高之差(区分度)要够大,
    且次高必须低于引擎阈值(0.62), 否则真机上随时可能点到另一个按钮。
    """
    from gui.dialogs import validate_template
    frame = _frame()
    for xy, name in ((PLAY_XY, "▶启动"), (HOME_XY, "⌂回充")):
        txt, ok, detail = validate_template(frame, _disc(frame, xy))
        assert ok, f"{name}: 原生尺度整盘模板应当校验合格 → {txt}"
        assert detail["最佳尺度"] == pytest.approx(1.0, abs=0.01), detail
        assert detail["区分度"] >= 0.25, f"{name}: 区分度太小, 容易点错按钮: {detail}"
        assert detail["次高峰"] < vision_threshold(), \
            f"{name}: 另一个按钮的相似度不该过阈值: {detail}"


def vision_threshold():
    from core.vision import NCC_MIN_SCORE
    return NCC_MIN_SCORE


def test_off_scale_crop_is_rejected_by_validation(tmp_path):
    """★ 尺度不对的模板必须被拦下(用户真机上就是这么踩的):

    用户在**别的尺度**上截的模板(实测: 图标只有屏上的 0.57 倍), 靠引擎放大 1.5~2 倍
    才勉强命中 —— 一放大, 图标细节被插值糊掉, 只剩圆盘轮廓, 于是同形按钮互相误匹配
    (日志现场: 回充模板 0.62 命中在"启动"按钮上, 机器人没去回充反而继续清扫)。
    "裁剪模板"的当场校验必须把这种模板判为不合格, 并说出"要缩放多少倍"。
    """
    from gui.dialogs import validate_template
    frame = _frame()
    disc = _disc(frame, PLAY_XY)
    small = cv2.resize(disc, (int(DISC * 0.57), int(DISC * 0.57)),
                       interpolation=cv2.INTER_AREA)     # 0.57 倍 —— 用户那种截法
    txt, ok, detail = validate_template(frame, small)
    assert not ok, f"尺度不对的模板不该判合格: {txt}"
    assert detail["最佳尺度"] > 1.3, f"应报出「要靠放大」: {detail}"
    assert "缩放" in txt, txt


def test_scale_warning_fires_for_off_scale_match(tmp_path, caplog):
    """★ 引擎侧兜底: 一旦真要靠缩放才能命中, 日志里必须出现「模板尺度」预警 ——
    否则只能靠人盯着图才发现(用户 2026-10-16 的原话: "要能自己喊出来")。"""
    from core import vision
    frame = _frame()
    disc = _disc(frame, HOME_XY)
    from core import imgio
    p = tmp_path / "half.png"
    assert imgio.imwrite(str(p), cv2.resize(disc, (DISC // 2, DISC // 2),
                                            interpolation=cv2.INTER_AREA))
    recs = []

    class _H(__import__("logging").Handler):
        def emit(self, record):
            recs.append(record.getMessage())

    h = _H(level=__import__("logging").WARNING)
    vision.log.addHandler(h)
    old = vision.log.level
    vision.log.setLevel(__import__("logging").WARNING)
    try:
        info = {}
        vision.find_in_gray(_gray(frame), str(p), detail=info)
    finally:
        vision.log.removeHandler(h)
        vision.log.setLevel(old)
    assert info.get("scale", 1.0) >= 1.2, info
    assert any("模板尺度" in m for m in recs), recs


def test_low_scale_template_can_hit_the_wrong_button(tmp_path):
    """★ 把"点错按钮"这条**真实现象**钉下来(用户 2026-10-16 的日志现场):

    尺度不对的模板, 最佳命中不一定是目标按钮 —— 全屏扫描时它可能落在别处(用户那次是
    落在"启动"圆盘上, 分数刚好压线 0.62)。所以: 这条模板要么命中**真值按钮**,
    要么返回 None; 一旦命中别处, 就是"点错按钮", 必须被日志预警/校验拦住。
    """
    from core import vision
    frame = _frame()
    gray = _gray(frame)
    small = cv2.resize(_disc(frame, HOME_XY), (int(DISC * 0.57), int(DISC * 0.57)),
                       interpolation=cv2.INTER_AREA)
    from core import imgio
    p = tmp_path / "small_home.png"
    assert imgio.imwrite(str(p), small)
    info = {}
    pos = vision.find_in_gray(gray, str(p), detail=info)
    if pos is not None:
        near_own = abs(pos[0] - HOME_XY[0]) <= 8 and abs(pos[1] - HOME_XY[1]) <= 8
        assert near_own or info.get("score", 1.0) < 0.95, \
            (f"尺度不对的模板命中了别处且分数很高({info} @ {pos}) —— 真机上就是点错按钮", pos)
