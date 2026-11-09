# -*- coding: utf-8 -*-
"""实时参数「添加到曲线」：自定义曲线页、命名与持久化。"""
import time

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from core.apos import catalog  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def settings(tmp_path):
    return QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat)


@pytest.fixture
def page(qapp, tmp_path):
    from gui.device_page import DevicePage
    st = QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat)
    p = DevicePage(settings=st, sdk_dir=tmp_path / "apos")
    yield p
    p.shutdown()


def _row_for(page, path):
    for _g, item, row in page._rows:
        if item["path"] == path:
            return row
    raise AssertionError(f"表格里没有 {path}")


def test_custom_page_named_by_chart_title(qapp, settings):
    from gui.curve_view import CurveView
    v = CurveView(settings=settings)
    # 没有自定义图时不出现在页列表里
    assert v.page_list.count() == len(catalog.CURVE_PAGES)
    assert v.add_custom_curve("/BatteryBattery/BatteryLevel", "电量图", "电量", "%")
    # 每张自定义图独占一页，页名就是设置的曲线图名称
    assert v.page_list.count() == len(catalog.CURVE_PAGES) + 1
    assert v.page_list.item(len(catalog.CURVE_PAGES)).text() == "电量图"


def test_add_and_remove_custom_curve(qapp, settings):
    from gui.curve_view import CurveView
    v = CurveView(settings=settings)
    assert v.add_custom_curve("/BatteryBattery/BatteryLevel", "电量图", "电量", "%")
    assert v.custom_curves()[0]["title"] == "电量图"
    # 历史定义里要有这条路径（切页可回填）
    assert "/BatteryBattery/BatteryLevel" in [d[0] for d in v._curve_defs]
    v.set_page(len(catalog.CURVE_PAGES))
    assert v.title.text() == "电量图"
    assert len(v._cards) == 1
    assert v.current_paths() == ["/BatteryBattery/BatteryLevel"]
    v.remove_custom_curve(0)
    assert v.custom_curves() == []
    assert v.page_list.count() == len(catalog.CURVE_PAGES)


def test_custom_curve_persists(settings):
    from gui.curve_view import CurveView
    v = CurveView(settings=settings)
    v.add_custom_curve("/MotorFan/Speed", "风机图", "风机转速", "rpm")
    v2 = CurveView(settings=settings)
    assert v2.custom_curves() == [{"path": "/MotorFan/Speed", "title": "风机图",
                                   "series": "风机转速", "unit": "rpm"}]


def test_custom_curve_keeps_value_metadata(qapp, settings):
    """自定义曲线要连同 scale/decode/bit 一起保存，否则画出来是错值/空白。"""
    import time
    from gui.curve_view import CurveView
    v = CurveView(settings=settings)
    v.add_custom_curve("/Mcu/LeftOdometer", "里程图", "左里程", "m",
                       meta={"scale": 0.001})
    chart = v.custom_charts()[0]
    assert chart["curves"][0]["scale"] == 0.001
    v.set_page(len(catalog.CURVE_PAGES))
    v.update_snapshot({"ok": {"/Mcu/LeftOdometer": 1147149},
                       "failed": {}, "ts": time.time()})
    key = ("/Mcu/LeftOdometer", None)
    card = next(c for c in v._cards if key in c.series_data)
    assert abs(list(card.series_data[key])[0][1] - 1147.149) < 1e-6
    # 重新加载后仍然带着解释方式
    v2 = CurveView(settings=settings)
    assert v2.custom_charts()[0]["curves"][0]["scale"] == 0.001


def test_add_odometer_row_keeps_scale(page, monkeypatch):
    page._rebuild_table()
    row = _row_for(page, "/Mcu/LeftOdometer")
    monkeypatch.setattr(page, "_ask_curve_target",
                        lambda default, targets: ("里程图", "左里程", None))
    page._add_rows_to_curves([row])
    curve = page.curve_view.custom_charts()[0]["curves"][0]
    assert curve["scale"] == 0.001 and curve["unit"] == "m"


def test_custom_curve_accumulates_history(qapp, settings):
    from gui.curve_view import CurveView
    v = CurveView(settings=settings)
    v.add_custom_curve("/MotorFan/Speed", "风机图", "风机转速", "rpm")
    v.set_page(len(catalog.CURVE_PAGES))
    v.update_snapshot({"ok": {"/MotorFan/Speed": 195}, "failed": {},
                       "ts": time.time()})
    data = v._history[("/MotorFan/Speed", None)]
    assert data and data[-1][1] == 195


def test_add_row_to_curve_asks_names(page, monkeypatch):
    page._rebuild_table()
    row = _row_for(page, "/BatteryBattery/BatteryLevel")
    asked = {}

    def fake_ask(default, charts):
        asked["default"] = default
        return "我的电量图", "我的电量", None

    monkeypatch.setattr(page, "_ask_curve_target", fake_ask)
    page._add_rows_to_curves([row])
    curves = page.curve_view.custom_curves()
    assert len(curves) == 1
    assert curves[0]["title"] == "我的电量图"
    assert curves[0]["series"] == "我的电量"
    assert asked["default"]              # 默认名取自组件参数名
    assert "1 张" in page.status_label.text()
    # 曲线页的页名跟着设置的曲线图名称走
    assert page.curve_view.page_list.item(
        len(catalog.CURVE_PAGES)).text() == "我的电量图"


def test_multi_select_adds_with_default_names(page, monkeypatch):
    page._rebuild_table()
    rows = [_row_for(page, p) for p in
            ("/BatteryBattery/BatteryLevel", "/MotorFan/Speed")]
    monkeypatch.setattr(page, "_ask_curve_target",
                        lambda default, charts: ("不应弹窗", "不应弹窗", None))
    page._add_rows_to_curves(rows)
    curves = page.curve_view.custom_curves()
    assert len(curves) == 2
    assert all(c["title"] != "不应弹窗" for c in curves)


def test_cancel_dialog_adds_nothing(page, monkeypatch):
    page._rebuild_table()
    row = _row_for(page, "/BatteryBattery/BatteryLevel")
    monkeypatch.setattr(page, "_ask_curve_target",
                        lambda default, charts: (None, None, None))
    page._add_rows_to_curves([row])
    assert page.curve_view.custom_curves() == []


def test_add_series_to_existing_chart(qapp, settings):
    from gui.curve_view import CurveView
    v = CurveView(settings=settings)
    v.add_custom_curve("/BatteryBattery/BatteryLevel", "电池图", "电量", "%")
    assert v.add_series_to_chart(0, "/BatteryBattery/BatteryVol",
                                 "电压", "mV")
    charts = v.custom_charts()
    assert len(charts) == 1                       # 页数不变
    assert [c["series"] for c in charts[0]["curves"]] == ["电量", "电压"]
    # 切到这张图时两条曲线都在，采集路径也包含两条
    v.set_page(len(catalog.CURVE_PAGES))
    assert v.title.text() == "电池图"
    assert len(v._cards) == 1
    assert "2 条曲线" in v.meta.text()
    assert set(v.current_paths()) == {"/BatteryBattery/BatteryLevel",
                                      "/BatteryBattery/BatteryVol"}
    # 持久化后仍然是同一张图两条线
    v2 = CurveView(settings=settings)
    charts2 = v2.custom_charts()
    assert len(charts2) == 1 and len(charts2[0]["curves"]) == 2


def test_mcu_paths_not_sent_to_apos(page):
    assert page._apos_paths(["/Mcu/ChargeStatus", "/BatteryBattery/BatteryLevel"]) \
        == ["/BatteryBattery/BatteryLevel"]


def test_drive_wheel_page_has_slip_curve(qapp, settings):
    from gui.curve_view import CurveView
    v = CurveView(settings=settings)
    idx = next(i for i, p in enumerate(catalog.CURVE_PAGES)
               if p["name"] == "主动轮")
    paths = [c["path"] for g in catalog.CURVE_PAGES[idx]["groups"]
             for c in g["curves"]]
    assert "/CarrierPosCarrierPos/SkidError" in paths
    assert "/CarrierPosCarrierPos/LineVelocity" in paths
    assert "/Mcu/LeftOdometer" in paths
    assert "/Mcu/YawAngle" in paths
    assert "/MotorWheelL/Speed" in paths


def test_clear_history_on_device_switch(qapp, settings):
    import time
    from gui.curve_view import CurveView
    v = CurveView(settings=settings)
    v.set_page(next(i for i, p in enumerate(catalog.CURVE_PAGES)
                    if p["name"] == "风机"))
    v.update_snapshot({"ok": {"/MotorFan/Speed": 100},
                       "failed": {}, "ts": time.time()})
    assert any(v._history.values())
    v.clear_history()
    assert all(not d for d in v._history.values())
    assert v._t0 is None


def test_odometer_curve_scaled_to_meters(qapp, settings):
    import time
    from gui.curve_view import CurveView
    v = CurveView(settings=settings)
    idx = next(i for i, p in enumerate(catalog.CURVE_PAGES)
               if p["name"] == "主动轮")
    v.set_page(idx)
    v.update_snapshot({"ok": {"/Mcu/LeftOdometer": 1147149},
                       "failed": {}, "ts": time.time()})
    key = ("/Mcu/LeftOdometer", None)
    card = next(c for c in v._cards if key in c.series_data)
    assert abs(list(card.series_data[key])[0][1] - 1147.149) < 1e-6


def test_builtin_curve_hide_and_restore(qapp, settings):
    """内置曲线右键删除=隐藏（持久化），可右键恢复。"""
    from gui.curve_view import CurveView
    v = CurveView(settings=settings)
    idx = next(i for i, p in enumerate(catalog.CURVE_PAGES)
               if p["name"] == "主动轮")
    v.set_page(idx)
    card = next(c for c in v._cards if c.group["name"] == "打滑系数")
    curve = card.group["curves"][0]
    v._hide_curve("主动轮", "打滑系数", curve)
    v.set_page(idx)
    card = next(c for c in v._cards if c.group["name"] == "打滑系数")
    assert card.group["curves"] == []
    # 持久化：新实例仍然隐藏
    v2 = CurveView(settings=settings)
    v2.set_page(idx)
    card2 = next(c for c in v2._cards if c.group["name"] == "打滑系数")
    assert card2.group["curves"] == []
    v2._show_curve(v2._hidden[0])
    v2.set_page(idx)
    card2 = next(c for c in v2._cards if c.group["name"] == "打滑系数")
    assert card2.group["curves"]


def test_remove_series_and_whole_chart(qapp, settings):
    from gui.curve_view import CurveView
    v = CurveView(settings=settings)
    v.add_custom_curve("/BatteryBattery/BatteryLevel", "电池图", "电量", "%")
    v.add_series_to_chart(0, "/BatteryBattery/BatteryVol", "电压", "mV")
    v.remove_series(0, 1)                     # 删第二条
    charts = v.custom_charts()
    assert len(charts) == 1
    assert [c["series"] for c in charts[0]["curves"]] == ["电量"]
    v.remove_series(0, 0)                     # 删空后整张图消失
    assert v.custom_charts() == []
    assert v.page_list.count() == len(catalog.CURVE_PAGES)


def test_add_row_to_existing_chart_via_dialog(page, monkeypatch):
    page._rebuild_table()
    page.curve_view.add_custom_curve(
        "/BatteryBattery/BatteryLevel", "我的电池", "电量", "%")
    row = _row_for(page, "/BatteryBattery/BatteryVol")
    target = {"kind": "custom", "index": 0, "label": "自定义「我的电池」"}
    monkeypatch.setattr(page, "_ask_curve_target",
                        lambda default, targets: ("我的电池", "电压", target))
    page._add_rows_to_curves([row])
    charts = page.curve_view.custom_charts()
    assert len(charts) == 1
    assert [c["series"] for c in charts[0]["curves"]] == ["电量", "电压"]
    assert "我的电池" in page.status_label.text()


def test_curve_targets_include_builtin_charts(qapp, settings):
    from gui.curve_view import CurveView
    v = CurveView(settings=settings)
    labels = [t["label"] for t in v.curve_targets()]
    assert any("电池 · 电量" in x for x in labels)
    assert any("风机 · 风机" in x for x in labels)


def test_add_series_to_builtin_chart(qapp, settings):
    from gui.curve_view import CurveView
    v = CurveView(settings=settings)
    # 图里已有的参数不允许重复加（否则两条同 key 的线会重叠）
    assert not v.add_series_to_group("电池", "电量",
                                     "/BatteryBattery/BatteryLevel", "电量", "%")
    assert v.add_series_to_group("电池", "电量",
                                 "/BatteryBattery/BatteryVol", "电压", "mV")
    v.set_page(next(i for i, p in enumerate(catalog.CURVE_PAGES)
                    if p["name"] == "电池"))
    # 电量那张图现在有两条曲线
    card = next(c for c in v._cards if c.group.get("name") == "电量")
    assert [s["title"] for s in card.group["curves"]] == ["电池-电量", "电压"]
    assert "/BatteryBattery/BatteryVol" in v.current_paths()
    # 持久化
    v2 = CurveView(settings=settings)
    assert v2.extra_curves()[0]["path"] == "/BatteryBattery/BatteryVol"
    # 删除追加曲线后恢复原样
    v2.remove_extra("电池", "电量", "/BatteryBattery/BatteryVol")
    assert v2.extra_curves() == []


def test_add_row_to_builtin_chart_via_dialog(page, monkeypatch):
    page._rebuild_table()
    row = _row_for(page, "/BatteryBattery/BatteryVol")
    target = {"kind": "builtin", "page": "电池", "group": "电量",
              "label": "电池 · 电量"}
    monkeypatch.setattr(page, "_ask_curve_target",
                        lambda default, targets: ("电池 · 电量", "电池电压", target))
    page._add_rows_to_curves([row])
    assert page.curve_view.extra_curves()[0]["path"] == \
        "/BatteryBattery/BatteryVol"
    assert "电池 · 电量" in page.status_label.text()
