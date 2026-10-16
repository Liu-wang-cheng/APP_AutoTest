# -*- coding: utf-8 -*-
"""文本判断必须排除**手机系统 UI**的文本(用户 2026-10-15 实测报的 bug)。

现场: 被测 APP 是扫地机器人, 手机(模拟器)插着 USB 在充电 —— 层级 dump 里除了 APP 页面,
还带着**手机自己的**状态栏/通知栏(`com.android.systemui`), 里面有「正在充电」。
而"确保设备在充电"这条前置找的就是「充电」⇒ **机器人没在充电也判通过**(拿手机的电量
状态顶了机器人的)。同类风险: 系统通知里的「检查更新」命中故障词「检查」、锁屏「等待中」
命中空闲词「等待」。

判据统一在 `core/sentinel.py`: 文本匹配(断言/if 条件/wait_for/前置检查/哨兵)一律跳过
手机系统 UI 包的节点; u2 的全屏查询(textContains)那条路也在 runner._assert_locator 里补了同样的过滤。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import sentinel, session  # noqa: E402


def _xml(*nodes):
    return "".join(nodes)


APP_CHARGING = ('<node text="充电中" package="com.tuya.smartiot" '
                'bounds="[40,300][300,360]"/>')
PHONE_CHARGING = ('<node text="正在充电" package="com.android.systemui" '
                  'bounds="[10,10][300,80]"/>')
PHONE_NOTIFY = ('<node text="正在检查更新" package="com.android.systemui" '
                'bounds="[10,10][300,80]"/>')
APP_OTHER = ('<node text="正在清洁" package="com.tuya.smartiot" '
             'bounds="[40,300][300,360]"/>')


class _Dev:
    """最小设备桩: is_charging 会 dump 一次层级"""

    serial = "fake"

    def __init__(self, xml):
        self._xml = xml

    def dump_hierarchy(self):
        return self._xml

    def __call__(self, **kw):
        class _El:
            def exists(self, timeout=1):
                return False
        return _El()


@pytest.fixture(autouse=True)
def _reset_ocr_frame_cache():
    """OCR 的帧缓存(1.5s TTL)是模块级全局 —— 本文件的用例会连调两次, 必须前后清掉"""
    from core import ocr
    ocr._cache.update(t=0.0, key=None, boxes=[], texts=[])
    yield
    ocr._cache.update(t=0.0, key=None, boxes=[], texts=[])


@pytest.fixture(autouse=True)
def _restore_ignored():
    """本文件会改"忽略哪些包" —— 前后都还原, 别留给别的测试"""
    sentinel.set_ignore_packages(None)
    yield
    sentinel.set_ignore_packages(None)


# ── ① 树匹配层: 系统 UI 的节点不算 ──

def test_visible_nodes_skips_system_ui_package():
    """★ 手机状态栏/通知栏的文本不进"看到的文本" —— 这就是那条充电假通过的根因"""
    nodes = sentinel.visible_nodes(_xml(PHONE_CHARGING, APP_OTHER))
    texts = [t for t, _d, _p in nodes]
    assert texts == ["正在清洁"], texts


def test_charging_check_ignores_phone_charging_state():
    """★ 真机场景: 手机在充电(系统 UI 有「正在充电」), 机器人**没**在充电
    ⇒ 「确保设备在充电」必须判**未充电**(以前会假通过)"""
    d = _Dev(_xml(PHONE_CHARGING, APP_OTHER))
    assert session.is_charging(d) is False

    # 机器人自己报了充电 → 正常判通过(别修过头把真命中一起排掉)
    d2 = _Dev(_xml(PHONE_CHARGING, APP_CHARGING))
    assert session.is_charging(d2) is True


def test_sentinel_ignores_fault_and_idle_words_from_system_ui():
    """系统通知里的「检查更新」/ 锁屏「等待中」不能触发故障/空闲哨兵"""
    assert sentinel.scan_status_words(xml=_xml(PHONE_NOTIFY, APP_OTHER)) == \
        (None, None, None)
    idle = ('<node text="等待中" package="com.android.systemui" '
            'bounds="[10,10][300,80]"/>')
    assert sentinel.scan_status_words(xml=_xml(idle, APP_OTHER)) == (None, None, None)
    # APP 自己的故障文案当然还要能扫到
    app_fault = ('<node text="滚刷被困, 请清理后重试" package="com.tuya.smartiot" '
                 'bounds="[40,300][900,360]"/>')
    kind, word, full = sentinel.scan_status_words(xml=_xml(app_fault))
    assert kind == "fault" and word == "被困" and "滚刷被困" in full


def test_text_present_ignores_system_ui():
    """断言/if 条件的统一入口(text_present)同样不吃系统 UI 的文本"""
    d = _Dev(_xml(PHONE_CHARGING, APP_OTHER))
    assert session.text_present(d, ["充电"]) is False
    assert session.text_present(d, ["清洁"]) is True


# ── ② 可配置: 别的 ROM / 常驻通知 APP 也能排掉 ──

def test_ignore_packages_is_configurable():
    xml = ('<node text="闹钟等待" package="com.miui.systemui" '
           'bounds="[10,10][300,80]"/>')
    assert sentinel.visible_nodes(xml)              # 默认不排它
    sentinel.set_ignore_packages("com.miui.systemui")
    assert sentinel.visible_nodes(xml) == []
    # 默认的系统 UI 包仍在排除名单里(配置是"追加"而不是"替换")
    assert "com.android.systemui" in sentinel.ignore_packages()
    # 传 None 恢复默认
    sentinel.set_ignore_packages(None)
    assert sentinel.ignore_packages() == set(sentinel.SYSTEM_UI_PACKAGES)


def test_nodes_without_package_are_kept():
    """★ 精简层级不带 package 属性 —— 那种节点**必须保持原样**(宁可不误杀)"""
    xml = '<node text="充电中" bounds="[40,300][300,360]"/>'
    assert [t for t, _d, _p in sentinel.visible_nodes(xml)] == ["充电中"]


# ── ③ OCR 兜底: 按**实测**系统 UI 矩形丢识别行(没有包名可依, 只能按坐标) ──

FULL_SCREEN = '<node bounds="[0,0][1080,1920]"/>'   # 只为推出屏幕尺寸
SYS_XML = ('<node text="20:22" package="com.android.systemui" bounds="[10,9][110,51]"/>'
           '<node text="" package="com.android.systemui" bounds="[989,13][1009,47]"/>')


def test_system_ui_rects_use_real_bounds_not_a_ratio():
    """★ 用层级里系统 UI 节点的**真实范围**, 不是"顶部 5%"这种猜的比例

    实测(1080x1920, 用户 2026-10-15): 系统栏只到 61px, 而 5% = 96px —— 多切 35px,
    而 APP 自己的标题在 y=104, 距 5% 线只剩 8px(换台挖孔屏就会把 APP 文案一起切掉)。
    """
    rects = sentinel.system_ui_rects(SYS_XML)
    assert rects == [(10, 9, 110, 51), (989, 13, 1009, 47)], rects
    assert sentinel.system_ui_rects('<node text="充电中" bounds="[1,2][3,4]"/>') == [], \
        "没有 package 的节点不该被当成系统 UI"


def test_ocr_lines_inside_system_ui_are_dropped_straddling_are_kept():
    """★ 只有**整框**落在系统 UI 矩形里才丢; 压线的一律保留(用户定的严格判据)"""
    rects = sentinel.system_ui_regions(SYS_XML + FULL_SCREEN)
    lines = [
        ("正在充电", (0, 0, 200, 40)),       # 整框在状态栏内 → 丢
        ("充电中", (0, 80, 300, 130)),       # 状态栏之下 → 留
        ("扫地机器人0090", (150, 40, 900, 130)),   # 从状态栏压线往下 → 留(严格判据)
    ]
    kept = [t for t, _b in sentinel.drop_system_ui_lines(lines, rects)]
    assert kept == ["充电中", "扫地机器人0090"], kept
    # 没有系统 UI 信息(精简层级) → 一条都不丢
    assert sentinel.drop_system_ui_lines(lines, []) == lines


class _OcrDev:
    """桩设备: 层级给一句系统 UI 文案, 画面里"看起来"有手机充电文案(OCR 口径)"""

    serial = "fake"

    def __init__(self, xml):
        self._xml = xml

    def dump_hierarchy(self):
        return self._xml

    def screenshot(self, path=None, format=None):
        return None

    def __call__(self, **kw):
        class _El:
            def exists(self, timeout=1):
                return False
        return _El()


def _force_ocr(monkeypatch, ocr_lines):
    """让判断一定走 OCR 兜底, 并把它认到的行固定下来"""
    from core import ocr
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "screen_boxes", lambda d, **k: list(ocr_lines))


def test_charging_precondition_not_fooled_by_phone_status_bar_via_ocr(monkeypatch):
    """★ 真机场景的 OCR 版本: 手机状态栏上"看得见"的「正在充电」不能顶成机器人充电

    树侧靠包名排除, OCR 读的是像素没有包名 —— 只能按实测的系统 UI 矩形丢。
    """
    d = _OcrDev(SYS_XML + FULL_SCREEN)       # 树里只有系统 UI 的时钟 → 稀疏 → 走 OCR
    _force_ocr(monkeypatch, [("正在充电", (0, 0, 200, 40)),      # 系统栏内 → 该丢
                             ("扫地机器人0090", (150, 104, 900, 160))])
    assert session.is_charging(d) is False, "手机状态栏的「正在充电」不算机器人充电"

    _force_ocr(monkeypatch, [("正在充电", (0, 0, 200, 40)),
                             ("充电中", (70, 309, 200, 360))])   # APP 自己的 → 该认
    assert session.is_charging(d) is True


def test_ocr_fault_words_from_system_ui_do_not_trip_sentinel(monkeypatch):
    """系统通知栏里 OCR 出来的「检查更新」不该触发故障哨兵(APP 自己的照常触发)"""
    from core import session as _s
    _s.reset_status_words()
    _force_ocr(monkeypatch, [("正在检查更新", (0, 0, 200, 40))])
    assert _s.scan_status_words  # 哨兵走的是同一批 OCR 行
    hit, _src, _raw, _pos = _s.text_present_detail(_OcrDev(SYS_XML + FULL_SCREEN), ["充电"])
    assert hit is None, "系统栏里的「检查更新」既不该命中目标, 也不该触发故障哨兵"


# ── ④ 识别**之前**就屏蔽系统栏(用户 2026-10-16: "应该把手机系统那一段排除掉识别才对") ──

class _ShotDev:
    """桩设备: 给一张固定画面, 用于检查"送进 OCR 引擎的图"长什么样"""

    serial = "fake"

    def __init__(self, img):
        self._img = img

    def screenshot(self, path=None, format=None):
        return self._img.copy()


def test_ocr_masks_system_ui_before_recognition(monkeypatch):
    """★ 送进 OCR 引擎的那张图里, 系统栏那一段必须已经被抹黑(而不是识别完再丢)

    抹黑而不裁图的原因: 裁掉会让后面所有行的 y 坐标整体偏移, 每个调用方都要做坐标回填。
    """
    import numpy as np
    from core import ocr

    seen = {}

    def _fake_engine(img):
        seen["img"] = img.copy()
        return [], None

    monkeypatch.setattr(ocr, "_get", lambda: _fake_engine)
    img = np.full((300, 200, 3), 200, dtype=np.uint8)
    ocr.screen_boxes(_ShotDev(img), use_cache=False, exclude=[(0, 0, 200, 60)])
    got = seen["img"]
    assert got[:60].max() == 0, "系统栏那一段应被抹黑"
    assert got[60:].min() == 200, "系统栏以下必须原样保留(坐标系不变)"
    assert got.shape == img.shape, "抹黑不能改变画面尺寸(坐标才对得上)"


def test_ocr_cache_key_includes_excluded_regions(monkeypatch):
    """★ 同一帧、不同屏蔽范围不能互相顶替缓存(否则会拿到"没抹黑那次"的结果)"""
    from core import ocr
    calls = []

    def _fake_engine(img):
        calls.append(img.copy())
        return [], None

    monkeypatch.setattr(ocr, "_get", lambda: _fake_engine)
    import numpy as np
    dev = _ShotDev(np.full((300, 200, 3), 200, dtype=np.uint8))
    ocr.screen_boxes(dev, exclude=[(0, 0, 200, 60)])
    ocr.screen_boxes(dev, exclude=[(0, 0, 200, 60)])       # 同范围 → 命中缓存
    assert len(calls) == 1, "同屏蔽范围应复用缓存"
    ocr.screen_boxes(dev, exclude=[])                       # 换范围 → 必须重新识别
    assert len(calls) == 2, "换了屏蔽范围就不能复用缓存"
