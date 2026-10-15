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
