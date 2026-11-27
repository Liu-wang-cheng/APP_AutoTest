# -*- coding: utf-8 -*-
"""设备参数页 / 顶部双页面 / 曲线（离屏）。"""
import json
import os
import time

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def page(qapp, tmp_path):
    from gui.device_page import DevicePage
    st = QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat)
    p = DevicePage(settings=st, sdk_dir=tmp_path / "apos")
    yield p
    p.shutdown()


def _pump_send(page, timeout=3.0):
    """等后台发事件线程完成并处理 done 回调（发送已移到 QThread）。"""
    app = QApplication.instance()
    end = time.time() + timeout
    while getattr(page, "_send_worker", None) is not None and time.time() < end:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()


def _row_for(page, path):
    for _g, item, row in page._rows:
        if item["path"] == path:
            return row
    raise AssertionError(f"表格里没有 {path}")


def test_device_table_shows_snapshot(page):
    page._apply_snapshot({
        "ok": {"/BatteryBattery/BatteryLevel": 80,
               "/System/CustomSN": "SN123"},
        "failed": {}, "ts": time.time()})
    row = _row_for(page, "/BatteryBattery/BatteryLevel")
    assert page.table.item(row, 1).text() == "80"
    row2 = _row_for(page, "/System/CustomSN")
    assert page.table.item(row2, 1).text() == "SN123"


def test_device_table_marks_failed(page):
    page._apply_snapshot({
        "ok": {}, "failed": {"/BatteryBattery/BatteryLevel": "timeout"},
        "ts": time.time()})
    row = _row_for(page, "/BatteryBattery/BatteryLevel")
    assert page.table.item(row, 1).text() == "读取失败"


def test_device_table_columns_fixed(page):
    """列宽固定：参数名/单位/时间固定，当前值占满剩余（用户 2026-11-06）。"""
    from PySide6.QtWidgets import QHeaderView
    page._rebuild_table()
    assert not page.table.horizontalHeader().stretchLastSection()
    assert page.table.columnWidth(0) == 220
    assert page.table.columnWidth(2) == 70
    assert page.table.columnWidth(3) == 100
    assert (page.table.horizontalHeader().sectionResizeMode(1)
            == QHeaderView.Stretch), "当前值列应占满剩余宽度"


def test_hidden_groups_and_unavailable_params_removed():
    """三合一拖布/万向轮/振动电机分组、以及取不到值的参数都不展示."""
    from core.apos import catalog
    names = [g["name"] for g in catalog.PARAM_GROUPS]
    assert "三合一拖布电机" not in names
    assert "万向轮" not in names
    assert "振动电机" not in names
    paths = catalog.all_param_paths()
    for p in ("/PointLaser/PointLaserData", "/MotorWheelLift/Curr",
              "/DustdetDustdet/DustLevel", "/MotorMidLift/Curr"):
        assert p not in paths


def test_pitch_roll_params_and_curves_use_mcu_frame():
    """俯仰/横滚取自 7809 调试帧(与 AutopackDebugger 同源), 参数页+曲线页都要有."""
    from core.apos import catalog
    items = {it.get("name"): it for g in catalog.PARAM_GROUPS
             for it in g["items"]}
    assert items["俯仰角"]["path"] == "/Mcu/PitchAngle"
    assert items["横滚角"]["path"] == "/Mcu/RollAngle"
    titles = [c["title"] for page in catalog.CURVE_PAGES
              for g in page["groups"] for c in g["curves"]]
    assert "IMU-俯仰角(°)" in titles and "IMU-横滚角(°)" in titles
    # 曲线值来自 7809 MCU 快照, 不参与 APOS 订阅
    assert "/Mcu/PitchAngle" not in catalog.all_curve_paths()


def test_work_status_and_carrier_mode_are_chinese():
    """工作状态/底盘模式的所有厂商枚举都要翻译成中文(用户 2026-11-18)."""
    from core.apos import catalog
    item = {"path": "/CleanTask/CleanAppStatus"}
    for raw, zh in (
            ("work_status_back_charge", "回充中"),
            ("work_status_base_station", "基站任务中"),
            ("work_status_area_clean", "划区清扫中"),
            ("work_status_curpoint_clean", "定点清扫中"),
            ("work_status_error", "故障"),
            ("work_status_idle", "空闲")):
        snap = {"ok": {item["path"]: {"CleanAppStatus": raw}}}
        assert catalog.display_value(item, snap) == zh
    carrier = {"path": "/Carrier/CarrierWorkMode"}
    for raw, zh in (("CARRIER_WORK_NORMAL", "正常工作"),
                    ("CARRIER_WORK_IDLE", "空闲"),
                    ("CARRIER_WORK_SHUTDOWN", "底盘关机")):
        assert catalog.display_value(
            carrier, {"ok": {carrier["path"]: raw}}) == zh


def test_drum_position_three_separate_params():
    """清洁滚筒三个到位状态分别显示(用户 2026-11-18)."""
    from core.apos import catalog
    paths = {
        "清洁滚筒下降到位": "/KeyStretchingInPlaceDown/WorkState",
        "清洁滚筒外扩到位": "/KeyStretchingInPlaceEx/WorkState",
        "清洁滚筒抬升到位": "/KeyStretchingInPlaceUp/WorkState",
    }
    names = {it.get("name"): it for g in catalog.PARAM_GROUPS
             for it in g["items"]}
    for name, path in paths.items():
        assert name in names and names[name]["path"] == path
        item = names[name]
        assert catalog.display_value(item, {"ok": {path: 1}}) == "到位"
        assert catalog.display_value(item, {"ok": {path: 0}}) == "未到位"


def test_object_param_shows_full_value(page):
    """地检/点激光这类整块数据要完整显示(单元格内换行)，不能只给摘要。"""
    from core.apos import catalog
    item = next(it for g in catalog.PARAM_GROUPS for it in g["items"]
                if it["path"] == "/Cliff/CliffData")
    assert "field" not in item
    snap = {"ok": {"/Cliff/CliffData": {f"Cliff{i}": 4095 for i in range(20)}}}
    text = catalog.display_value(item, snap)
    assert "Cliff0" in text and "Cliff19" in text   # 完整内容
    assert "共 2" not in text


def test_long_object_value_wraps_in_cell(page):
    snap = {"ok": {"/Cliff/CliffData": {f"Cliff{i}": 4095 for i in range(20)}},
            "failed": {}, "ts": time.time()}
    page.search.setText("地检数据")
    page._apply_snapshot(snap)
    page._rebuild_table()
    row = page.table.rowCount() - 1
    assert "Cliff19" in page.table.item(row, 1).text()
    assert page.table.rowHeight(row) > 50, "长内容应在单元格内换行显示"


def test_connection_refresh_does_not_auto_reconnect(qapp, monkeypatch):
    """连接失败/断开后的刷新不能自动重连（否则会无限重试，日志里来回跳）。"""
    import types
    from PySide6.QtWidgets import QComboBox
    import gui.app_pages as ap
    calls = []
    monkeypatch.setattr(ap.QTimer, "singleShot",
                        staticmethod(lambda ms, fn: calls.append(ms)))
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    class Dev:
        connected = False
        connecting = False
        connected_ip = ""

        class settings:
            @staticmethod
            def value(key, default=None):
                return ""

        @staticmethod
        def _known_devices():
            return [{"ip": "1.2.3.4", "model": "M", "sn": "S"}]

    win = types.SimpleNamespace(robot_ip_combo=QComboBox(),
                                _robot_auto_connecting=False)
    ap.refresh_robot_ips(win, Dev, auto=False)
    assert not calls, "连接状态变化触发的刷新不能自动重连"
    ap.refresh_robot_ips(win, Dev, auto=True)
    assert calls, "启动/设备列表刷新仍应自动连接一次"


def test_robot_ip_combo_shows_model_sn_ip_from_device_info(qapp):
    """自动化测试页 IP 下拉显示 型号-SN-IP；型号/SN 晚到时用设备信息补全。"""
    import types
    from PySide6.QtWidgets import QComboBox
    import gui.app_pages as ap

    class Dev:
        connected = True
        connecting = False
        connected_ip = "192.168.10.21"
        _device_info = {"model": "CLA_ZY2L", "sn": "LDTest0012"}

        class settings:
            @staticmethod
            def value(key, default=None):
                return ""

        @staticmethod
        def _known_devices():
            return []

    win = types.SimpleNamespace(robot_ip_combo=QComboBox(),
                                _robot_auto_connecting=True)
    win.robot_ip_combo.setEditable(True)
    ap.refresh_robot_ips(win, Dev(), auto=False)
    # 选中后输入框只显示 IP; 下拉项仍是 型号-SN-IP(用户 2026-11-27)
    assert win.robot_ip_combo.currentText() == "192.168.10.21"
    assert win.robot_ip_combo.itemText(0) == "CLA_ZY2L-LDTest0012-192.168.10.21"


def test_fit_robot_combo_follows_long_label(qapp):
    """自动化测试页的扫地机IP下拉也要装得下「型号-SN-IP」。"""
    from PySide6.QtWidgets import QComboBox
    from gui.app_pages import _fit_robot_combo
    combo = QComboBox()
    combo.setEditable(True)
    text = "CLL_PE3C-uuid79c29b5e6258dd70-192.168.10.71"
    combo.addItem(text, "192.168.10.71")
    _fit_robot_combo(combo)
    need = combo.fontMetrics().horizontalAdvance(text) + 40
    assert combo.minimumWidth() == combo.maximumWidth()
    assert combo.maximumWidth() >= need
    assert combo.maximumWidth() <= 560


def test_ip_combo_widens_for_long_labels(page):
    """扫地机IP输入框要能容纳「型号-SN-IP」长文本，不截断（用户 2026-11-06）。"""
    page._add_device_item({"model": "CLA_HS4", "sn": "B43A89CL800003A",
                           "ip": "192.168.10.63"})
    first = page.device_combo.maximumWidth()
    page._add_device_item({"model": "CLL_PE3C", "sn": "uuid79c29b5e6258dd70",
                           "ip": "192.168.10.71"})
    widest = "CLL_PE3C-uuid79c29b5e6258dd70-192.168.10.71"
    need = page.device_combo.fontMetrics().horizontalAdvance(widest) + 40
    assert page.device_combo.minimumWidth() == page.device_combo.maximumWidth()
    assert page.device_combo.maximumWidth() >= need
    assert page.device_combo.maximumWidth() >= first
    assert page.device_combo.maximumWidth() <= 680



def test_selected_ip_prefers_typed_text(page):
    page.device_combo.setEditable(True)
    page.device_combo.setEditText("10.1.2.3")
    assert page._selected_ip() == "10.1.2.3"
    page._add_device_item({"model": "M", "sn": "S", "ip": "192.168.1.5"})
    assert page._selected_ip() == "192.168.1.5"


def test_scan_replaces_stale_devices(page):
    """重新扫描后只保留本次扫到的可连接设备，离线的历史 IP 要清掉（用户 2026-11-06）。"""
    page._scan_done([{"model": "M", "sn": "S", "ip": "10.0.0.1"}])
    page._scan_done([{"model": "M2", "sn": "S2", "ip": "10.0.0.2"}])
    ips = [page.device_combo.itemData(i) for i in range(page.device_combo.count())]
    assert ips == ["10.0.0.2"]
    assert [d["ip"] for d in page._known_devices()] == ["10.0.0.2"]


def test_scan_done_saves_devices(page, tmp_path):
    page._scan_done([{"model": "CLA_HS4", "sn": "SN1", "ip": "192.168.1.9"}])
    assert page.device_combo.count() >= 1
    assert [d["ip"] for d in page._known_devices()] == ["192.168.1.9"]


def test_battery_reader_converts_mv_to_v(page):
    class FakeService:
        def read_once(self, paths):
            return {"/BatteryBattery/BatteryLevel": 80,
                    "/BatteryBattery/BatteryVol": 15800,
                    "/BatteryBattery/BatteryTemp": 31.5}

    page._service = FakeService()
    page._connected = True
    data = page.battery_reader()
    assert data["battery"] == 80
    assert data["voltage_v"] == 15.8
    assert data["temp_c"] == 31.5
    assert data["source"] == "param"


def test_robot_connect_dialog_lists_saved_devices(qapp, tmp_path):
    from gui.device_page import DevicePage
    from gui.robot_connect_dialog import RobotConnectDialog
    st = QSettings(str(tmp_path / "dlg.ini"), QSettings.IniFormat)
    st.setValue("apos/last_ip", "10.0.0.2")
    p = DevicePage(settings=st, sdk_dir=tmp_path / "apos")
    # 列表来自"本次扫描"(实时), 不再读历史设置
    p._scan_done([
        {"model": "M1", "sn": "S1", "ip": "10.0.0.1"},
        {"model": "M2", "sn": "S2", "ip": "10.0.0.2"},
    ])
    try:
        dlg = RobotConnectDialog(p)
        assert dlg._selected_ip() == "10.0.0.2"
        assert dlg.combo.count() == 2
        dlg._use_app()
        assert dlg.use_param is False
        dlg.close()
    finally:
        p.shutdown()


def test_remembers_last_connected_ip(qapp, tmp_path):
    from gui.device_page import DevicePage
    st = QSettings(str(tmp_path / "last.ini"), QSettings.IniFormat)
    st.setValue("apos/devices", json.dumps([
        {"model": "M1", "sn": "S1", "ip": "10.0.0.1"},
        {"model": "M2", "sn": "S2", "ip": "10.0.0.2"},
    ]))
    st.setValue("apos/last_ip", "10.0.0.2")
    p = DevicePage(settings=st, sdk_dir=tmp_path / "apos")
    try:
        assert p._selected_ip() == "10.0.0.2"
        p._remember_ip("10.0.0.1")
        assert st.value("apos/last_ip") == "10.0.0.1"
    finally:
        p.shutdown()


def test_fault_simulation_panel_reads_voice_and_led_from_device_log(page, tmp_path):
    """故障模拟: 触发后从设备日志解析语音序号/灯显/故障码(用户 2026-11-11)。"""
    class FakeService:
        def simulate_fault(self, key):
            return True

        def post_event(self, topic, event):
            self.called = (topic, event)
            return True

        def fault_log_since(self, t0):
            return [
                (1.0, "11-11 7:14:37.944/SW D/application_frame.cpp:241 "
                      "event:error_fan_stuck, beep:066, ismust_play:1"),
                (1.0, "11-11 7:14:37.950/SW D/led_show_base.cpp:261 "
                      "event_id: error_fan_stuck, must_show: 1 data: {"),
                (1.0, '"rgb_led__home" : "red_slow_flick",'),
                (1.0, '"rgb_led__start" : "red_flick_3"'),
                (1.0, "apos_adapter.cpp:3481 "
                      "msg=error_fan_stuck code=926"),
            ]

        def clear_faults(self):
            return True

    page._service = FakeService()
    page._connected = True
    page._connected_ip = "10.63"
    page._simulate_fault("fan_stuck")
    _pump_send(page)
    assert "已触发" in page.fault_status.text()
    page._show_fault_result("fan_stuck", 0)
    text = page.fault_result.text()
    assert "066" in text and "red_slow_flick" in text and "926" in text
    # 灯显 JSON 续行也要显示在日志区
    assert "rgb_led__home" in page.fault_log.toPlainText()
    # 日志区可滚动(用户 2026-11-11)
    assert page.fault_log.verticalScrollBar() is not None
    page._clear_faults()
    assert "已发送清除状态" in page.fault_status.text()
    assert page.fault_tab is not None
    # 常用故障改为下拉选择, 不再平铺按钮(用户 2026-11-11)
    idx = page.fault_pick_combo.findData("error_fan_stuck")
    assert idx >= 0
    page.fault_pick_combo.setCurrentIndex(idx)
    page._simulate_picked_fault()
    _pump_send(page)
    assert "已触发" in page.fault_status.text()
    # 常用故障也支持手动输入关键字, 下方弹出候选
    assert page.fault_pick_combo.isEditable()
    pc = page.fault_pick_combo.completer()
    assert pc.filterMode() == Qt.MatchContains
    pc.setCompletionPrefix("风机")
    assert pc.completionCount() >= 1
    page.fault_pick_combo.setEditText("风机卡住")
    page._simulate_picked_fault()
    _pump_send(page)
    assert page._service.called == ("/hardware/error", "error_fan_stuck")

    # 加载事件配置后自动比对: 一致 → 绿勾
    import json
    cfg = tmp_path / "events.json"
    cfg.write_text(json.dumps([{
        "identifier": "error_fan_stuck", "name": "风机卡住",
        "rgb_hardware_function_set": [
            {"identifier": "rgb_led__home",
             "rgb_light_effect": {"identifier": "red_slow_flick"}}],
        "sound": {"identifier": "066"}}], ensure_ascii=False), encoding="utf-8")
    page._load_fault_config(str(cfg))
    # AP 方案输入框只显示文件名, 完整路径在 tooltip(用户 2026-11-13)
    assert page.fault_cfg_label.text() == cfg.name
    assert page.fault_cfg_label.toolTip() == str(cfg)
    assert page._fault_cfg_path == str(cfg)
    # 下拉显示中文名, 触发仍用 identifier
    assert "风机卡住" in page.fault_event_combo.itemText(0)
    page.fault_event_combo.setCurrentIndex(0)
    assert page._current_fault_event() == "error_fan_stuck"
    # 手动输入关键字, 下方弹出候选(中文名/英文标识都能包含匹配)
    comp = page.fault_event_combo.completer()
    assert comp.filterMode() == Qt.MatchContains
    comp.setCompletionPrefix("fan")
    assert comp.completionCount() >= 1
    comp.setCompletionPrefix("风机")
    assert comp.completionCount() >= 1
    # 选中候选(只改输入框文字、没有 currentIndex)也能还原 identifier
    page.fault_event_combo.currentIndex()
    page.fault_event_combo.setEditText("风机卡住（error_fan_stuck）")
    assert page._current_fault_event() == "error_fan_stuck"
    # 主题不再让用户选(用户 2026-11-11), 工具自动判断
    assert not hasattr(page, "fault_topic_combo")
    page._show_fault_result("fan_stuck", 0)
    assert "✓ 与配置一致" in page.fault_verdict.text()

    class BadService(FakeService):
        def fault_log_since(self, t0):
            return [(1.0, "event:error_fan_stuck, beep:999"),
                    (1.0, '"rgb_led__home" : "all_off"')]

    page._service = BadService()
    page._show_fault_result("fan_stuck", 0)
    assert "✗ 与配置不一致" in page.fault_verdict.text()
    assert "999" in page.fault_verdict.text()
    # 触发即实测, 不再有"以实测为准"按钮(用户 2026-11-11)
    assert not hasattr(page, "fault_accept_btn")
    # 手动选/输入配置事件名触发; 配置里没有的事件 → 未配置
    page._service = FakeService()
    page.fault_event_combo.setCurrentText("error_drop")
    page._trigger_custom_event()
    _pump_send(page)
    assert page._service.called == ("/hardware/error", "error_drop")
    page._show_event_result("error_drop", "地检异常", 0)
    assert "未配置" in page.fault_verdict.text()

    # 配置里有事件但没配语音/灯显(频繁集尘): 显示未配置, 不显示未捕获
    cfg2 = tmp_path / "events2.json"
    cfg2.write_text(json.dumps([{
        "identifier": "app_sweeper_keyboard_press_frequently",
        "name": "频繁集尘", "rgb_hardware_function_set": [],
        "sound": {}}], ensure_ascii=False), encoding="utf-8")
    page._load_fault_config(str(cfg2))
    page._show_event_result("app_sweeper_keyboard_press_frequently",
                            "频繁集尘", 0)
    assert "未配置" in page.fault_result.text()
    assert "未捕获" not in page.fault_result.text()
    assert "未配置语音/灯显" in page.fault_verdict.text()


def test_curve_view_updates(qapp):
    from core.apos import catalog
    from gui.curve_view import CurveView
    battery_idx = next(i for i, p in enumerate(catalog.CURVE_PAGES)
                       if p["name"] == "电池")
    v = CurveView()
    v.set_page(battery_idx)            # 电池页
    v.update_snapshot({"ok": {"/BatteryBattery/BatteryLevel": 80},
                       "failed": {}, "ts": time.time()})
    key = ("/BatteryBattery/BatteryLevel", None)
    cards = [c for c in v._cards if key in c.series_data]
    assert cards and len(cards[0].series_data[key]) == 1
    assert v.follow is True
    # Y 轴由数据驱动手动设范围(顶部刻度=数据最大值), 不再用 pyqtgraph 自动范围
    vr = cards[0].plot.getViewBox().viewRange()[1]
    assert vr[1] >= 80
    levels = cards[0].plot.getAxis("left")._tickLevels
    assert levels and levels[0][-1] == (80.0, "80")


def test_main_window_top_nav(qapp, monkeypatch, tmp_path):
    from gui import main_window as mw
    monkeypatch.setattr(mw, "CASES_DIR", str(tmp_path / "Test_cases"), raising=False)
    w = mw.MainWindow()
    try:
        assert w.pages.count() == 6
        # 默认打开自动化测试页
        assert w.pages.currentWidget() is w.test_page
        assert hasattr(w, "robot_ip_combo")
        w._show_page("device")
        assert w.pages.currentWidget() is w.device_page
        w._show_page("endurance")
        assert w.pages.currentWidget() is w.endurance_page
        w._show_page("loop")
        assert w.pages.currentWidget() is w.loop_page
        w._show_page("fault")
        assert w.pages.currentWidget() is w.fault_page
        w._show_page("ai")
        assert w.pages.currentWidget() is w.ai_page
        w._show_page("test")
        assert w.pages.currentWidget() is w.test_page
    finally:
        w.close()


def test_catalog_translates_status_and_has_vendor_params():
    from core.apos import catalog
    assert len(catalog.all_param_paths()) >= 70
    assert catalog.translate_value("", "work_status_total_clean") == "清扫中"
    assert catalog.translate_value("", "CARRIER_WORK_IDLE") == "空闲"
    snap = {"ok": {"/CleanTask/CleanAppStatus":
                   {"CleanAppStatus": "work_status_dry_mop"}}}
    assert catalog.display_value({"path": "/CleanTask/CleanAppStatus"}, snap) == "拖布烘干中"


def test_wall_distance_param_and_curve():
    """沿墙距离按厂商 IrAlongWall ADC 差值数组换算（未沿墙时为 0）。"""
    from core.apos import catalog
    path = "/IrAlongWall/AdcDifValue[MaxDetectionAdcChannelNum]"
    assert path in catalog.all_param_paths()
    item = next(it for g in catalog.PARAM_GROUPS if g["name"] == "沿墙红外"
                for it in g["items"] if it["name"] == "沿墙距离")
    assert item["path"] == path and item["decode"] == "ir_wall_distance"
    assert catalog.display_value(item, {"ok": {path: "0000"}}) == "0"
    assert catalog.display_value(item, {"ok": {path: "0500"}}) == "5"
    page = next(p for p in catalog.CURVE_PAGES if p["name"] == "沿墙")
    curves = [c for g in page["groups"] for c in g["curves"]]
    assert curves[0]["path"] == path
    assert curves[0].get("decode") == "ir_wall_distance"
    # 曲线也能把十六进制数组解成数值（全 0 → 0）
    from gui.curve_view import CurveView
    v = CurveView()
    v.set_page(next(i for i, p in enumerate(catalog.CURVE_PAGES)
                    if p["name"] == "沿墙"))
    v.update_snapshot({"ok": {path: "0000"}, "failed": {}, "ts": time.time()})
    key = (path, None)
    card = next(c for c in v._cards if key in c.series_data)
    assert list(card.series_data[key])[0][1] == 0.0


def test_charge_status_curve_uses_mcu_flag(qapp):
    """充电状态曲线用 7809 的 MCU 标志（在桩 1 / 离桩 0，与厂商面板一致）。"""
    from core.apos import catalog
    from gui.curve_view import CurveView
    idx = next(i for i, p in enumerate(catalog.CURVE_PAGES)
               if p["name"] == "电池")
    v = CurveView()
    v.set_page(idx)
    v.update_snapshot({"ok": {"/Mcu/ChargeStatus": 1},
                       "failed": {}, "ts": time.time()})
    key = ("/Mcu/ChargeStatus", None)
    cards = [c for c in v._cards if key in c.series_data]
    assert cards and list(cards[0].series_data[key])[0][1] == 1.0


def test_curve_value_format_avoids_scientific_notation():
    from gui.curve_view import _fmt
    assert _fmt(1147149.0, {"unit": "mm"}) == "1147149 mm"
    assert _fmt(553571.0, {}) == "553571"
    assert _fmt(0.5, {"unit": "mm"}) == "0.5 mm"
    assert _fmt(0.0003, {}) == "0.0003"


def test_cliff_trigger_curves_colors_and_hover(qapp):
    """地检触发四条线颜色/线型不同，悬停显示参数名和数值。"""
    from PySide6.QtCore import QPointF
    from core.apos import catalog
    from gui.curve_view import CurveView
    v = CurveView()
    v.set_page(next(i for i, p in enumerate(catalog.CURVE_PAGES)
                    if p["name"] == "地检"))
    for i, val in enumerate((9, 8, 9)):
        v.update_snapshot({"ok": {"/Mcu/CliffTrigger": val},
                           "failed": {}, "ts": 1000.0 + i * 60})
    card = next(c for c in v._cards if c.group["name"] == "地检触发")
    assert len(card._lines) == 4
    colors = {line.opts["pen"].color().name() for line in card._lines.values()}
    assert len(colors) == 4
    pos = card.plot.getViewBox().mapViewToScene(QPointF(1.0, 0.5))
    card._on_mouse_move(pos)
    text = card._hover.text()
    assert "左边跌落" in text and "右边跌落" in text
    assert "时间" in text


def test_curve_auto_fit_after_30s_idle(qapp):
    """手动缩放/拖动后暂停跟随; 30 秒无操作自动恢复自适应(用户 2026-11-11)。"""
    from gui.curve_view import AUTO_FIT_IDLE_MS, CurveView
    v = CurveView()
    assert AUTO_FIT_IDLE_MS == 30_000
    assert v._idle_timer.interval() == AUTO_FIT_IDLE_MS
    assert v._idle_timer.isSingleShot()
    assert "30 秒" in v.follow_hint.text()

    v._manual_range()                    # 模拟用户拖动/缩放
    assert v.follow is False
    assert not v.follow_box.isChecked()
    assert v._idle_timer.isActive()
    assert not v.follow_hint.isHidden()

    v._auto_fit_after_idle()             # 计时到点
    assert v.follow is True
    assert v.follow_box.isChecked()
    assert v.follow_hint.isHidden()
    assert not v._idle_timer.isActive()

    v._manual_range()                    # 再次操作 → 重新计时
    assert v._idle_timer.isActive()


def test_y_axis_top_tick_is_data_max_including_spike(qapp):
    """Y 轴顶部刻度=当前数据最大值；数据突然变大时轴要跟上(用户 2026-11-11)。"""
    from core.apos import catalog
    from gui.curve_view import CurveView
    v = CurveView()
    idx = next(i for i, p in enumerate(catalog.CURVE_PAGES)
               if p["name"] == "风机")
    v.set_page(idx)
    card = next(c for c in v._cards if c.plot is not None)
    base = time.time()
    for i, val in enumerate((100, 200, 500)):
        v.update_snapshot({"ok": {"/MotorFan/Speed": val},
                           "failed": {}, "ts": base + i * 60})
    v.update_snapshot({"ok": {"/MotorFan/Speed": 5000},
                       "failed": {}, "ts": base + 240})
    vr = card.plot.getViewBox().viewRange()[1]
    assert vr[1] > 5000, "顶部留余量, 最大值标签才不会被裁掉"
    assert vr[0] < 100, "底部也留余量, 最小值标签才不会被裁掉"
    ticks = card.plot.getAxis("left")._tickLevels[0]
    assert ticks[0] == (100.0, "100"), ticks      # 底部刻度 = 数据最小值
    assert ticks[-1] == (5000.0, "5000"), ticks   # 顶部刻度 = 数据最大值


def test_y_axis_min_max_follow_live_data(qapp):
    """Y 轴上下限随实时数据变化, 并保证两端刻度都存在(用户 2026-11-11)。"""
    from core.apos import catalog
    from gui.curve_view import CurveView
    v = CurveView()
    idx = next(i for i, p in enumerate(catalog.CURVE_PAGES)
               if p["name"] == "电池")
    v.set_page(idx)
    card = [c for c in v._cards
            if "/BatteryBattery/BatteryVol" in [x["path"] for x in c.series]][0]
    base = time.time()
    for i, val in enumerate((9900, 10000, 10050)):
        v.update_snapshot({"ok": {"/BatteryBattery/BatteryVol": val},
                           "failed": {}, "ts": base + i * 60})
    ticks = card.plot.getAxis("left")._tickLevels[0]
    assert ticks[0] == (9900.0, "9900")
    assert ticks[-1] == (10050.0, "10050")
    # 数据抬升后上限跟着变(下限仍取窗口内的数据)
    v.update_snapshot({"ok": {"/BatteryBattery/BatteryVol": 12000},
                       "failed": {}, "ts": base + 240})
    ticks = card.plot.getAxis("left")._tickLevels[0]
    assert ticks[-1] == (12000.0, "12000")


def test_curve_follow_state_is_per_page(qapp):
    """跟随/自适应按曲线页(组)独立: 风机暂停不影响电池(用户 2026-11-11)。"""
    from core.apos import catalog
    from gui.curve_view import CurveView
    v = CurveView()
    fan = next(i for i, p in enumerate(catalog.CURVE_PAGES) if p["name"] == "风机")
    batt = next(i for i, p in enumerate(catalog.CURVE_PAGES) if p["name"] == "电池")

    v.set_page(fan)
    v._manual_range()                       # 风机页缩放/拖动
    assert v.follow is False and not v.follow_box.isChecked()
    assert not v.follow_hint.isHidden()

    v.set_page(batt)                        # 切到电池: 独立状态, 仍在跟随
    assert v.follow is True and v.follow_box.isChecked()
    assert v.follow_hint.isHidden()

    v.set_page(fan)                         # 切回风机: 暂停状态被记住
    assert v.follow is False
    v._reset_view()                         # 顶部自适应只恢复风机
    assert v.follow is True and v.follow_box.isChecked()

    v.set_page(batt)                        # 电池从未被影响
    assert v.follow is True
    v._manual_range()                       # 电池单独暂停
    v.set_page(fan)
    assert v.follow is True                 # 风机不受电池影响

    # 30 秒到点恢复的是"被暂停的那一页", 即电池; 切回去已恢复
    v._auto_fit_after_idle()
    v.set_page(batt)
    assert v.follow is True and v.follow_box.isChecked()


def test_multi_curve_values_above_plot_not_occluded(qapp):
    """多曲线实时值放在图上方彩色标签里, 不再用透明图例被曲线压住。"""
    from PySide6.QtCore import QPointF
    from core.apos import catalog
    from gui.curve_view import CurveView
    v = CurveView()
    v.set_page(next(i for i, p in enumerate(catalog.CURVE_PAGES)
                    if p["name"] == "地检"))
    for i, val in enumerate((9, 8)):
        v.update_snapshot({"ok": {"/Mcu/CliffTrigger": val},
                           "failed": {}, "ts": 1000.0 + i * 60})
    card = next(c for c in v._cards if c.group["name"] == "地检触发")
    assert card._legend is None, "图内图例应移除, 避免曲线压住文字"
    # Y 轴高度封顶, 不再被拉满整屏(用户 2026-11-11)
    assert card.plot.minimumHeight() == card.plot.maximumHeight() == 220
    assert len(card._value_labels) == 4
    texts = [lab.text() for lab in card._value_labels.values()]
    assert any("左边跌落" in t and "1" in t for t in texts), texts
    assert any("#e11d48" in t or "#2563eb" in t for t in texts), "应有彩色圆点"
    # 数值随数据更新
    v.update_snapshot({"ok": {"/Mcu/CliffTrigger": 0},
                       "failed": {}, "ts": 1120.0})
    texts = [lab.text() for lab in card._value_labels.values()]
    assert all(": <b>0</b>" in t or ": <b>0 " in t for t in texts), texts
    # 悬停提示仍然正常
    pos = card.plot.getViewBox().mapViewToScene(QPointF(1.0, 0.5))
    card._on_mouse_move(pos)
    assert "左边跌落" in card._hover.text()


def test_curve_dict_key_value(qapp):
    """地检是整块字典，曲线要能按 key 取单个探头值（用户 2026-11-06）。"""
    from core.apos import catalog
    from gui.curve_view import CurveView
    idx = next(i for i, p in enumerate(catalog.CURVE_PAGES)
               if p["name"] == "地检")
    v = CurveView()
    v.set_page(idx)
    v.update_snapshot({"ok": {"/Cliff/CliffData": {"Cliff0": 12, "Cliff1": 34}},
                       "failed": {}, "ts": time.time()})
    key = ("/Cliff/CliffData", "Cliff0")
    c0 = [c for c in v._cards if key in c.series_data]
    assert c0 and list(c0[0].series_data[key])[0][1] == 12.0


def test_curve_history_backfills_after_page_switch(qapp):
    """在别的页签/别的曲线页期间采集到的数据，切回时应能回填。"""
    from core.apos import catalog
    from gui.curve_view import CurveView
    battery_idx = next(i for i, p in enumerate(catalog.CURVE_PAGES)
                       if p["name"] == "电池")
    v = CurveView()
    v.set_page(0)  # 风机页
    v.update_snapshot({"ok": {"/BatteryBattery/BatteryLevel": 66,
                              "/MotorFan/Speed": 2200},
                       "failed": {}, "ts": time.time()})
    v.set_page(battery_idx)  # 电池页：切换到这一刻才创建卡片
    key = ("/BatteryBattery/BatteryLevel", None)
    cards = [c for c in v._cards if key in c.series_data]
    assert cards and list(cards[0].series_data[key])[0][1] == 66


def test_scan_falls_back_to_tcp_when_udp_port_busy(qapp, monkeypatch):
    """5550 被 AutopackDebugger 占用(UDP bind 抛 OSError)时仍要走 TCP 兜底。"""
    import gui.device_page as dp

    def boom(**kwargs):
        raise OSError("UDP 5550 被占用")

    monkeypatch.setattr(dp.scanner, "scan", boom)
    th = dp.ScanThread(timeout=0.1)
    monkeypatch.setattr(th, "_tcp_fallback", lambda items: [
        {"ip": "10.0.0.5", "sn": "S", "model": "M"}])
    got = {}
    th.finished.connect(lambda items: got.setdefault("items", items))
    th.failed.connect(lambda msg: got.setdefault("failed", msg))
    th.run()
    assert "failed" not in got
    assert got["items"][0]["ip"] == "10.0.0.5"


def test_failed_switch_clears_old_values(page):
    """切换设备失败后不能把上一台机器的旧值留在表里（用户 2026-11-10）。"""
    page._apply_snapshot({"ok": {"/BatteryBattery/BatteryLevel": 80},
                          "failed": {}, "ts": time.time()})
    row = _row_for(page, "/BatteryBattery/BatteryLevel")
    assert page.table.item(row, 1).text() == "80"
    page._connect_done(False, "192.168.10.21", {"error": "机器人未响应"})
    assert page._last_snapshot["ok"] == {}
    row = _row_for(page, "/BatteryBattery/BatteryLevel")
    assert page.table.item(row, 1).text() == "—"
    assert "连接失败" in page.status_label.text()


def test_connect_done_keeps_thread_until_it_finishes(page):
    """done 之后线程还要补读基站版本: 引用不能提前清空（关窗会崩）。"""
    sentinel = object()
    page._connect_thread = sentinel
    try:
        page._connect_done(True, "10.0.0.9", {"sn": "S", "model": "M"})
        assert page._connect_thread is sentinel
    finally:
        page._connect_thread = None


def test_connect_to_queues_new_ip_while_connecting(page):
    """初始连接还没结束时切换 IP 不能丢: 先排队（用户 2026-11-16）。"""
    page._connecting = True
    page._connect_ip = "192.168.10.63"
    try:
        assert page.connect_to("192.168.10.70") is True
        assert page._pending_connect_ip == "192.168.10.70"
        # 正在连的正好是同一台: 不重复排队
        assert page.connect_to("192.168.10.63") is False
        assert page._pending_connect_ip == "192.168.10.70"
    finally:
        page._connecting = False
        page._pending_connect_ip = ""


def test_queued_ip_connects_after_previous_attempt(page, monkeypatch):
    calls = []
    monkeypatch.setattr(page, "connect_device", lambda: calls.append(1))
    page._connecting = True
    page._connect_ip = "192.168.10.63"
    assert page.connect_to("192.168.10.70") is True
    page._connect_done(False, "192.168.10.63", {"error": "机器人未响应"})
    assert calls == [1]
    assert page._pending_connect_ip == ""
    assert page.device_combo.currentText() == "192.168.10.70"



def test_connected_clears_stale_fault_status(page):
    """连上后「无法连接…正在切换或未就绪」必须清掉（用户 2026-11-16）。"""
    page.fault_status.setText(
        "无法连接 192.168.10.63（正在切换或未就绪）")
    page._connect_done(True, "192.168.10.63", {"sn": "S", "model": "M"})
    assert page.fault_status.text() == "已连接 192.168.10.63"


def test_connected_keeps_unrelated_fault_status(page):
    page.fault_status.setText("PRD 解析中…")
    page._connect_done(True, "10.0.0.9", {"sn": "S", "model": "M"})
    assert page.fault_status.text() == "PRD 解析中…"


def test_shutdown_cancels_connect_thread(page):
    calls = {}

    class FakeThread:
        def isRunning(self):
            return True

        def cancel(self):
            calls["cancel"] = True

        def wait(self, timeout):
            calls["wait"] = timeout
            return True

    page._connect_thread = FakeThread()
    page.shutdown()
    assert calls.get("cancel") is True and calls.get("wait") == 6000


def test_shutdown_cancels_running_scan(page):
    calls = {}

    class FakeThread:
        def __init__(self):
            self.running = True

        def isRunning(self):
            return self.running

        def cancel(self):
            calls["cancel"] = True

        def wait(self, timeout=None):
            calls["wait"] = timeout
            self.running = False
            return True

    page._scan_thread = FakeThread()
    page.shutdown()
    assert calls.get("cancel") is True and calls.get("wait") == 5000


def test_catalog_param_names_are_chinese():
    from core.apos import catalog
    labels = [catalog.item_label(it) for g in catalog.PARAM_GROUPS
              for it in g["items"]]
    assert "电量" in labels and "左主动轮电流" in labels
    # 厂商 DisplayName 不直接出现在表格里（避免中英混排）
    assert "Level" not in labels and "Current" not in labels
    assert all(lbl.strip() for lbl in labels)


def test_side_brush_extend_group_maps_to_arm_paths():
    """厂商把边刷外扩放在 KeyArmMotorLocationIn 组里，别当成拖布臂。"""
    from core.apos import catalog
    group = next(g for g in catalog.PARAM_GROUPS if g["name"] == "边刷外扩")
    items = {it["path"]: it["name"] for it in group["items"]}
    assert items["/KeyArmMotorLocationIn/WorkState"] == "内位置在位"
    assert items["/KeyArmMotorLocationOut/WorkState"] == "外位置在位"
    assert items["/MotorArm/Curr"] == "电机电流"
    assert items["/MotorArm/PwmFb"] == "电机PWM"


def test_side_brush_and_mid_brush_groups_exist():
    from core.apos import catalog
    names = {g["name"]: {it["path"] for it in g["items"]}
             for g in catalog.PARAM_GROUPS}
    assert "/MotorSideBrushL/Curr" in names["边刷"]
    assert "/MotorSideBrushR/Curr" in names["边刷"]
    assert "/MotorMidBrush/Curr" in names["中刷"]


def test_table_tab_collects_curve_paths_too(page):
    """连上后即使停在表格页，曲线路径也要采集，切过去不能从零开始。"""
    from core.apos import catalog
    seen = {}

    class FakeService:
        def set_paths(self, paths, interval=None):
            seen["paths"] = paths
            seen["interval"] = interval

    page._service = FakeService()
    page._connected = True
    page._current_page = 0
    page._set_paths_for_tab()
    assert "/MotorSideBrushL/Speed" in seen["paths"]
    assert "/MotorFan/Speed" in seen["paths"]
    assert set(seen["paths"]) >= {
        p for p in catalog.all_param_paths()
        if not catalog.is_mcu_path(p)}          # MCU 反馈走 7809, 不订阅 APOS


def test_system_group_has_image_and_mcu_version():
    """实时参数要能看到镜像版本和 MCU 版本（来自 FirmwareInfo）。"""
    from core.apos import catalog
    group = next(g for g in catalog.PARAM_GROUPS if g["name"] == "系统")
    labels = {it["name"]: it for it in group["items"]}
    assert labels["镜像版本"]["field"] == "SocImageVersion"
    assert labels["MCU版本"]["field"] == "McuVersion"
    snap = {"ok": {"/System/FirmwareInfo":
                   '{"Model": "CLA_HS4", "Version": "2.3.97",'
                   ' "SocImageVersion": "V1.6.1783423166",'
                   ' "McuVersion": "0.3676.2"}'}}
    assert catalog.display_value(labels["镜像版本"], snap) == "V1.6.1783423166"
    assert catalog.display_value(labels["MCU版本"], snap) == "0.3676.2"


def test_robot_info_reads_sn_firmware_and_base_station(page):
    """报告用的设备信息：SN / 固件版本 / 基站版本。"""
    class FakeClient:
        def read_base_station_version(self, retries=1, delay=0.4):
            return "8.2.1.1"

    class FakeService:
        client = FakeClient()

        def read_once(self, paths):
            return {"/System/CustomSN": "SN9",
                    "/System/FirmwareInfo":
                        '{"Model": "CLA_HS4", "Version": "2.1.26"}'}

    page._service = FakeService()
    page._connected = True
    info = page.robot_info()
    assert info["sn"] == "SN9"
    assert info["firmware_version"] == "2.1.26"
    assert info["model"] == "CLA_HS4"
    assert info["base_station_version"] == "8.2.1.1"

    page._device_info = info
    page._update_info_label()
    assert "SN9" in page.info_label.text()
    assert "8.2.1.1" in page.info_label.text()


def test_robot_info_empty_when_disconnected(page):
    info = page.robot_info()
    assert info["sn"] == "" and info["firmware_version"] == ""
    assert info["base_station_version"] == ""


def test_run_worker_fills_report_device_info(qapp):
    """报告头：机器人信息优先，APP 版本兜底。"""
    from gui.runner_thread import RunWorker
    captured = {}

    class FakeReport:
        def set_device_info(self, **kw):
            captured.update(kw)

    class FakeDevice:
        serial = "ADB123"

        def app_version(self, package):
            return "1.8.51.10"

    w = RunWorker("dev", [], [], robot_info_reader=lambda: {
        "sn": "SN9", "firmware_version": "2.3.97",
        "base_station_version": "8.2.1.1"})
    try:
        w._fill_device_info(FakeReport(), FakeDevice(), {"app": {"package": "pkg"}})
    finally:
        w.deleteLater()
    assert captured == {"sn": "SN9", "app_version": "1.8.51.10",
                        "firmware_version": "2.3.97",
                        "base_station_version": "8.2.1.1"}


def test_connect_is_async_and_reads_base_station(qapp, tmp_path, monkeypatch):
    """切换/连接不能在 UI 线程做（用户 2026-11-06 反馈切换 IP 卡顿）。"""
    import time
    from PySide6.QtCore import QEventLoop, QTimer
    import gui.device_page as dp
    monkeypatch.setattr(dp, "ensure_sdk", lambda *a, **k: True)

    class FakeClient:
        def read_base_station_version(self, retries=1, delay=0.4):
            return "8.2.1.1"

    class FakeService:
        client = FakeClient()

        def connect(self, ip):
            time.sleep(0.4)        # 模拟网络连接耗时
            return True

        def read_once(self, paths):
            return {"/System/CustomSN": "SN9",
                    "/System/FirmwareInfo":
                        '{"Model": "CLA_HS4", "Version": "2.3.97"}'}

        def set_paths(self, paths, interval=None):
            self.paths = paths

        def disconnect(self):
            pass

        def latest(self):
            return {"ok": {}, "failed": {}, "ts": 0}

    st = QSettings(str(tmp_path / "async.ini"), QSettings.IniFormat)
    page = dp.DevicePage(settings=st, sdk_dir=tmp_path / "apos",
                         service_factory=lambda: FakeService())
    page.device_combo.setEditText("10.0.0.5")
    states = []
    page.connectingChanged.connect(lambda on, ip: states.append((on, ip)))
    loop = QEventLoop()
    page.connect_finished.connect(lambda *a: loop.quit())
    try:
        t0 = time.time()
        page.connect_device()
        elapsed = time.time() - t0
        assert elapsed < 0.2, f"connect_device 阻塞了 UI {elapsed:.2f}s"
        assert page.connecting
        assert (True, "10.0.0.5") in states, "连接中状态要通知出去(右上角显示)"
        QTimer.singleShot(5000, loop.quit)
        loop.exec()
        assert page.connected
        assert states[-1] == (False, "")
        assert "SN9" in page.info_label.text()
        # 基站版本是连接完成后后台补的（先让流跑 3 秒），稍等它到达
        for _ in range(350):
            if "8.2.1.1" in page.info_label.text():
                break
            qapp.processEvents()
            time.sleep(0.02)
        assert "8.2.1.1" in page.info_label.text()
    finally:
        page.shutdown()


def test_endurance_page_combos_are_dropdowns(qapp):
    """续航组合：默认 8 条；吸力/水量只能下拉选择，吸力 0~5、水量 0~4。"""
    from gui.endurance_page import EndurancePage

    page = EndurancePage()
    try:
        assert len(page._collect_combos()) == 8
        fan = page.table.cellWidget(0, 2)
        assert not fan.isEditable()
        fan.setCurrentIndex(fan.findData(0))
        assert page._collect_combos()[0][1] == 0
        fan.setCurrentIndex(fan.findData(5))
        assert page._collect_combos()[0][1] == 5
        water = page.table.cellWidget(4, 3)
        assert not water.isEditable()
        water.setCurrentIndex(water.findData(4))
        assert page._collect_combos()[4][2] == 4
        # 一个按钮：主按钮一键选中，菜单反选
        page._invert_checks()
        assert not page._row_checked(0)
        page._check_all()
        assert page._row_checked(0)
        # 同一个按钮：第一次全选，第二次反选
        page.all_btn.click()
        assert page._row_checked(0)
        assert page.all_btn.text() == "反选"
        page.all_btn.click()
        assert not page._row_checked(0)
        assert page.all_btn.text() == "全选"
    finally:
        page.shutdown()


def test_page_nav_shows_connecting_state(qapp):
    """右上角设备状态要显示「连接中 · IP」，不能一直停留在旧的「已连接」。"""
    from gui.app_pages import PageNav
    nav = PageNav()
    nav.set_device(True, "CLA_HS4 · 10.0.0.1")
    assert "已连接" in nav.dev_status.text()
    nav.set_connecting(True, "10.0.0.9")
    assert "连接中" in nav.dev_status.text()
    assert "10.0.0.9" in nav.dev_status.text()
    nav.set_device(True, "CLA_HS4 · 10.0.0.9")
    # 导航栏只显示型号(避免挤掉页签)，完整 IP 在 tooltip
    assert "已连接" in nav.dev_status.text()
    assert "10.0.0.9" in nav.dev_status.toolTip()
    nav.set_device(False, "")
    assert "未连接" in nav.dev_status.text()


def test_disconnect_is_async(qapp, tmp_path):
    """断开也要放后台（ParamService.stop 最多 join 5 秒）。"""
    import time
    import gui.device_page as dp

    class SlowService:
        def disconnect(self):
            time.sleep(0.4)

    st = QSettings(str(tmp_path / "disc.ini"), QSettings.IniFormat)
    page = dp.DevicePage(settings=st, sdk_dir=tmp_path / "apos")
    page._service = SlowService()
    page._connected = True
    page._connected_ip = "10.0.0.5"
    try:
        t0 = time.time()
        page.disconnect_device()
        elapsed = time.time() - t0
        assert elapsed < 0.2, f"disconnect_device 阻塞了 UI {elapsed:.2f}s"
        assert page._disconnecting
        page._disconnect_thread.wait(3000)
        for _ in range(10):
            qapp.processEvents()
        assert not page._disconnecting
    finally:
        page.shutdown()


def test_toolbar_left_and_right_groups(qapp, monkeypatch, tmp_path):
    """新建/打开/保存靠左，故障检测及其后按钮靠右（用户 2026-11-06）。"""
    from gui import main_window as mw
    monkeypatch.setattr(mw, "CASES_DIR", str(tmp_path / "Test_cases"), raising=False)
    w = mw.MainWindow()
    w.resize(1024, 640)
    w.show()
    try:
        qapp.processEvents()
        bar = w.test_page.layout().itemAt(0).widget()
        assert w.new_btn.x() < 40, "新建应在最左"
        assert w.save_btn.x() + w.save_btn.width() < bar.width() // 3
        right = w.update_btn.x() + w.update_btn.width()
        assert right >= bar.width() - 24, "检查更新应贴右"
        assert w.fault_btn.x() > w.save_btn.x() + w.save_btn.width() + 60
    finally:
        w.close()


def test_main_window_toolbar_wraps_and_nav_status_fits(qapp, monkeypatch, tmp_path):
    """1280 宽屏下工具栏要能换行，顶部右侧状态不能被顶出窗口。"""
    from gui import main_window as mw
    monkeypatch.setattr(mw, "CASES_DIR", str(tmp_path / "Test_cases"), raising=False)
    w = mw.MainWindow()
    try:
        w.resize(1080, 720)
        w.show()
        qapp.processEvents()
        assert w.minimumSizeHint().width() <= 1200
        nav = w.page_nav
        assert nav.dev_status.geometry().right() <= nav.width()
    finally:
        w.close()


def test_fault_project_checklist_and_report(page, tmp_path, monkeypatch):
    """对照 HS4 项目清单(灯显/语音/故障码)并导出 Excel 报告(用户 2026-11-12)。"""
    import openpyxl
    from gui import device_page as dp
    xls = tmp_path / "HS4_LED_Voice_Config.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "主机语音定义"
    ws.append(["语音编号", "触发条件", "语音内容参考示范", "中文",
               "英文", "场景类型", "机器人停机状态"])
    ws.append(["Q066", "主机风机异常", "主机风机异常，请检查",
               "主机风机异常，请检查", "", "", "停机"])
    ws2 = wb.create_sheet("主机灯显定义")
    ws2.append(["分类", "过程", "灯显"])
    ws2.append(["故障", "风机异常", "红色慢闪"])
    ws3 = wb.create_sheet("SS故障消息上报260904")
    ws3.append(["Push ID", "QDMS ID\n错误码", "", "", "prd中文文案", "",
                "Normal/Error"])
    ws3.append(["DA_RC_E_156", "ErrorCode_156", "", "",
                "刷子上卡有异物", "", "Error，停机"])
    wb.save(xls)

    page._load_hs4_project(str(xls))
    cfg = tmp_path / "events.json"
    cfg.write_text(json.dumps([{
        "identifier": "error_fan_stuck", "name": "风机卡住",
        "rgb_hardware_function_set": [{
            "identifier": "rgb_led__home",
            "name": "RGB回充灯设备",
            "rgb_light_effect": {"identifier": "red_slow_flick",
                                 "name": "红色慢闪"}}],
        "sound": {"identifier": "066"}}], ensure_ascii=False),
        encoding="utf-8")
    page._load_fault_config(str(cfg))

    class Svc:
        def post_event(self, topic, event):
            return True

        def fault_log_since(self, t0):
            return [
                (1.0, "application_frame.cpp:241 "
                      "event:error_fan_stuck, beep:066"),
                (1.0, "led_show_base.cpp:261 event_id: error_fan_stuck "
                      "data: {"),
                (1.0, '"rgb_led__home" : "red_slow_flick"'),
                (1.0, "apos_adapter.cpp msg=error_fan_stuck code=156"),
            ]

        def clear_faults(self):
            return True

    page._service = Svc()
    page._connected = True
    page._show_event_result("error_fan_stuck", "风机卡住", 0)
    note = page.fault_hs4_note.text()
    assert "语音 通过" in note and "Q066" in note
    assert "灯显 通过" in note and "清单词条 ✓" in note
    assert "故障码 通过" in note and "DA_RC_E_156" in note
    row = page._fault_rows[-1]
    assert row["overall"] == "通过"
    assert row["voice_actual"] == "066"
    assert row["code_status"] == "通过"
    assert page.fault_report_btn.isEnabled()

    out = tmp_path / "report.xlsx"
    monkeypatch.setattr(dp.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(out), "")))
    page._export_fault_report()
    assert out.exists()
    wb2 = openpyxl.load_workbook(out, data_only=True)
    assert wb2["明细"]["E2"].value == "error_fan_stuck\n风机卡住"

    # 批量测试按关键字过滤
    page.fault_pick_combo.setEditText("风机")
    items = page._batch_items()
    assert items and all(("风机" in label)
                         for _ev, label, _row in items)


def test_prd_parsed_config_overrides_event_config(page, tmp_path):
    """PRD AI 解析结果展示在表格, 并按它验证(优先于事件配置)。"""
    cfg = tmp_path / "events.json"
    cfg.write_text(json.dumps([{
        "identifier": "error_fan_stuck", "name": "风机卡住",
        "rgb_hardware_function_set": [{
            "identifier": "rgb_led__home", "name": "RGB回充灯设备",
            "rgb_light_effect": {"identifier": "red_slow_flick",
                                 "name": "红灯慢闪"}},
            {"identifier": "rgb_led__start", "name": "RGB电源灯设备",
             "rgb_light_effect": {"identifier": "all_off",
                                  "name": "关闭"}}],
        "sound": {"identifier": "066"}}], ensure_ascii=False),
        encoding="utf-8")
    page._load_fault_config(str(cfg))
    page._on_prd_parsed([{
        "event": "error_fan_stuck", "name": "风机卡住", "voice": "099",
        "voice_text": "风机故障", "led_home": "red_slow_flick",
        "led_home_text": "红灯慢闪", "led_start": "all_off",
        "led_start_text": "关闭", "fault_code": "156",
        "fault_push": "DA_RC_E_156", "note": "第一行\n第二行"}])
    assert not page.fault_prd_table.isHidden()
    # 备注支持换行并保留完整内容
    assert page.fault_prd_table.wordWrap() is True
    assert page.fault_prd_table.item(0, 7).text() == "第一行\n第二行"
    assert page.fault_prd_table.rowCount() == 1
    # 中文名用 AP 事件配置里的名称, 不再用 PRD 语音文案(用户 2026-11-13)
    assert page.fault_prd_table.item(0, 1).text() == "风机卡住"
    # AI 解析提示要进日志区(用户 2026-11-12)
    assert "AI 解析完成" in page.fault_log.toPlainText()
    # AI 不再给灯显, 由表格下拉手动选择
    assert page._prd_rows[0]["led_home"] == ""
    from gui.device_page import NoWheelComboBox
    # 列顺序: 4=电源灯, 5=回充灯
    home = page.fault_prd_table.cellWidget(0, 5)
    assert isinstance(home, NoWheelComboBox)
    before = home.currentIndex()
    class _Wheel:
        ignored = False
        def ignore(self):
            self.ignored = True
    ev = _Wheel()
    home.wheelEvent(ev)
    assert ev.ignored and home.currentIndex() == before   # 滚轮不改选项
    home.setCurrentIndex(home.findData("red_slow_flick"))
    start = page.fault_prd_table.cellWidget(0, 4)
    start.setCurrentIndex(start.findData("all_off"))
    exp = page._expected_for("error_fan_stuck")
    assert exp["sound"] == "099"            # PRD 覆盖事件配置的 066
    assert exp["led"]["rgb_led__home"] == "red_slow_flick"
    assert exp["led"]["rgb_led__start"] == "all_off"
    # AP 配置里该事件没有故障码; ZY2L 这类项目日志也不上报 code,
    # 不能拿 AI 解析的码当期望值(用户 2026-11-17: 0134 期望 1001 假失败)
    assert exp["code"] == ""

    class Svc:
        def fault_log_since(self, t0):
            return [
                (1.0, "application_frame.cpp:241 "
                      "event:error_fan_stuck, beep:099"),
                (1.0, "led_show_base.cpp:261 event_id: error_fan_stuck "
                      "data: {"),
                (1.0, '"rgb_led__home" : "red_slow_flick"'),
                (1.0, '"rgb_led__start" : "all_off"'),
                (1.0, "apos_adapter.cpp msg=error_fan_stuck code=156"),
            ]

    page._service = Svc()
    page._connected = True
    page._show_event_result("error_fan_stuck", "风机卡住", 0)
    assert "✓ 与配置一致" in page.fault_verdict.text()
    row = page._fault_rows[-1]
    # 期望语音列只显示 PRD 自己的语音号(AP 的 066 只参与判定, 用户 2026-11-17)
    assert row["voice_expected"] == "099"
    assert row["code_actual"] == "156"
    assert row["overall"] == "通过"
    # 表格编辑后保存为新的验证配置
    page.fault_prd_table.item(0, 3).setText("077")
    assert page._expected_for("error_fan_stuck")["sound"] == "077"
    assert json.loads(page.settings.value("apos/prd_parsed"))[0]["voice"] == "077"
    # 有 PRD 解析结果时, 批量测试只跑 PRD 里的事件
    assert [e for e, _n, _r in page._batch_items()] == ["error_fan_stuck"]


def test_prd_parse_buttons_learning_first_and_force_ai(page):
    """PRD 有「解析」(优先学习库) 和「直接用 AI 解析 PRD」(用户 2026-11-13)."""
    assert page.fault_prd_parse_btn.text() == "解析"
    assert "学习库" in page.fault_prd_parse_btn.toolTip()
    assert page.fault_prd_ai_btn.text() == "直接用 AI 解析 PRD"
    assert "忽略学习库" in page.fault_prd_ai_btn.toolTip()

    from gui import device_page as dp

    class _FakePage:
        _prd_catalog = None

        def _memory_prefill(self):
            raise AssertionError("直接用 AI 时不应读取学习库")

        def _catalog_entries(self, _catalog):
            return {5: [("Q005", {})], 6: [("Q006", {})]}

        def _ai_event_list(self):
            return [{"event": "charging", "name": "开始充电"}]

        def _candidate_events_for(self, _missing):
            return [{"event": "charging", "name": "开始充电"}]

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(dp.ai_prd, "extract_voice_catalog",
                        lambda _p: {"active": {"5": {"final": 5},
                                               "6": {"final": 6}},
                                    "map": {}})
    try:
        worker = dp._PrdPrepWorker(_FakePage(), "prd.xlsx",
                                   prefer_memory=False)
        got = []
        worker.done.connect(got.append)
        worker.run()
    finally:
        monkeypatch.undo()
    assert got and got[0]["force_ai"] is True
    assert got[0]["covered"] == set() and got[0]["mem_items"] == []
    assert got[0]["missing"] == [5, 6]


def test_ai_config_page_default_address_and_save(qapp, tmp_path):
    """AI 配置独立成页, 默认地址 192.168.160.145:8081。"""
    from gui.ai_config_page import AiConfigPage
    st = QSettings(str(tmp_path / "ai.ini"), QSettings.IniFormat)
    page = AiConfigPage(settings=st)
    # 学习库管理放在同一页的第二个页签(用户 2026-11-13)
    assert page.tabs.count() == 2 and page.tabs.tabText(1) == "学习库"
    assert page.library_table is not None
    assert page.library_audit_btn.text() == "AI 审核"
    # 学习库按钮统一放在页签最上方(用户 2026-11-13)
    lib_layout = page.tabs.widget(1).layout()
    assert lib_layout.itemAt(0).layout() is not None
    assert page.url_edit.text() == "http://192.168.160.145:8081"
    page.key_edit.setText("sk-abc")
    page.model_combo.setEditText("gpt-4o")
    page.max_tokens_spin.setValue(65536)
    page.save()
    assert st.value("ai/prd_base_url") == "http://192.168.160.145:8081"
    assert st.value("ai/prd_api_key") == "sk-abc"
    assert st.value("ai/prd_model") == "gpt-4o"
    assert st.value("ai/prd_max_tokens", type=int) == 65536
    page.url_edit.setText("http://10.0.0.9:9000/v1")
    page.save()
    assert st.value("ai/prd_base_url") == "http://10.0.0.9:9000/v1"


def test_prd_parse_integrates_and_starts_auto_audit(page, monkeypatch):
    """AI 解析结果整合学习库后, 自动审核本次新增映射(用户 2026-11-13)."""
    captured = []
    monkeypatch.setattr(page, "_start_prd_learning_audit",
                        lambda entries: captured.extend(entries))
    page._prd_parse_used_ai = True
    page._on_prd_parsed([{
        "event": "error_fan_stuck", "name": "风机卡住", "voice": "066",
        "voice_text": "风机异常", "voice_key": "风机异常"}])
    assert captured and captured[0]["event"] == "error_fan_stuck"
    assert captured[0]["key"] == "风机异常"
    hit = page._prd_memory.exact("风机异常")
    assert hit and hit["event"] == "error_fan_stuck"
    assert page._prd_parse_used_ai is False


def test_prd_auto_audit_correction_updates_row_and_memory(page):
    """自动审核修正映射后, 表格与学习库一起更新(用户 2026-11-13)."""
    page._prd_rows = [{
        "event": "error_fan_stuck", "name": "风机卡住", "voice": "066",
        "voice_text": "风机异常", "voice_key": "风机异常",
        "source": "ai", "event_options": [],
        "led_home": "", "led_home_text": "", "led_start": "",
        "led_start_text": "", "fault_code": "999", "note": ""}]
    page._prd_memory.put("风机异常", {
        "event": "error_fan_stuck", "event_name": "风机卡住"},
        source="ai")
    page._prd_audit_run_id = 7
    page._on_prd_learning_audited(7, {
        "corrections": [{"key": "风机异常",
                         "event": "error_kit_fan_err",
                         "current_event": "error_fan_stuck",
                         "reason": "更准确"}],
        "checked": 1, "errors": []})
    row = page._prd_rows[0]
    assert row["event"] == "error_kit_fan_err"
    assert row["source"] == "ai_review" and row["fault_code"] == ""
    hit = page._prd_memory.exact("风机异常")
    assert hit["event"] == "error_kit_fan_err"
    assert hit["source"] == "ai_review"


def test_library_ai_audit_applies_valid_corrections(qapp, tmp_path,
                                                     monkeypatch):
    """学习库「AI 审核」把修正写回本机记忆(含人工记录, 用户 2026-11-16)."""
    from core.apos import prd_memory
    from gui.ai_config_page import AiConfigPage
    st = QSettings(str(tmp_path / "audit.ini"), QSettings.IniFormat)
    page = AiConfigPage(settings=st)
    mem = prd_memory.PrdMemory(str(tmp_path / "lib.json"))
    mem.put("风机异常", {"event": "error_fan_stuck",
                        "event_name": "风机卡住"}, source="user")
    page._audit_mem = mem
    page._audit_allowed = {"error_kit_fan_err": "风机异常"}
    page._audit_skipped = 0
    monkeypatch.setattr(page, "_reload_library", lambda: None)
    page._on_library_audit_done({
        "corrections": [{"key": "风机异常",
                         "event": "error_kit_fan_err",
                         "current_event": "error_fan_stuck",
                         "reason": "更准确"}],
        "checked": 1, "errors": []})
    saved = prd_memory.PrdMemory(str(tmp_path / "lib.json"))
    hit = saved.items["风机异常"]
    assert hit["event"] == "error_kit_fan_err"
    assert hit["event_name"] == "风机异常" and hit["source"] == "ai_review"
    assert "修正 1 条" in page.library_status.text()


def test_ai_config_fetch_models_marks_1m(qapp, tmp_path, monkeypatch):
    """从 /models 获取模型, 带 1m 的模型标 1M 并保存标记(用户 2026-11-12)。"""
    from core.apos import ai_prd
    from gui.ai_config_page import AiConfigPage
    monkeypatch.setattr(ai_prd, "fetch_models", lambda *a: [
        "gpt-4o", "gemini-2.5-pro-1m", "claude-sonnet-5-5"])
    st = QSettings(str(tmp_path / "ai2.ini"), QSettings.IniFormat)
    page = AiConfigPage(settings=st)
    page.fetch_models()
    import time as _t
    end = _t.time() + 5
    while page._fetch_worker is not None and _t.time() < end:
        qapp.processEvents()
    labels = [page.model_combo.itemText(i)
              for i in range(page.model_combo.count())]
    assert "gemini-2.5-pro-1m（1M）" in labels
    idx = page.model_combo.findData("gemini-2.5-pro-1m")
    page.model_combo.setCurrentIndex(idx)
    page._model_selected()
    assert page.one_m_check.isChecked()
    assert "1M" in page.model_hint.text()
    page.save()
    assert st.value("ai/prd_model") == "gemini-2.5-pro-1m"
    assert st.value("ai/prd_model_1m", type=bool) is True
    # 非 1M 模型不自动勾选
    page.model_combo.setCurrentIndex(page.model_combo.findData("gpt-4o"))
    page._model_selected()
    page.one_m_check.setChecked(False)
    page.save()
    assert st.value("ai/prd_model_1m", type=bool) is False


def test_device_ai_config_uses_default_url(page):
    cfg = page._ai_config()
    assert cfg["base_url"] == "http://192.168.160.145:8081"
    # Claude Code 解析方式已取消(用户 2026-11-17), 配置里不再有该开关
    assert "use_claude_code" not in cfg
    # 事件页的「AI 设置…」跳到 AI 配置页(通过主窗口 _show_page)
    calls = []
    page.window()._show_page = lambda name: calls.append(name)
    assert page._open_ai_settings() is True
    assert calls == ["ai"]


def test_prd_expected_ignores_unbacked_fault_code(page):
    """AP 配置没有故障码时, AI 臆造的码不参与判定(ZY2L 日志无 code)."""
    page._fault_expect = {"ev": {"name": "x", "sound": "0001",
                                 "led": {}, "led_names": {}}}
    row = {"qid": "0134", "voice": "0134", "event": "ev",
           "fault_code": "1001", "code_source": ""}
    page._prd_rows = [row]
    assert page._expected_for("ev", prd_row=row).get("code") == ""
    # 真机实测回填的码也不能当期望(自己验证自己); 人工确认过才保留
    row["code_source"] = "actual"
    assert page._expected_for("ev", prd_row=row).get("code") == ""
    row["code_source"] = "user"
    assert page._expected_for("ev", prd_row=row).get("code") == "1001"


def test_note_translation_uses_chinese(page):
    """解析页备注里的 PRD 含义用中文(用户 2026-11-17)."""
    page._prd_rows = [{
        "qid": "0930", "voice": "0930",
        "note": "PRD：충전대 급수통에 스케일 방지 모듈이 없습니다.\n（0930）",
        "prd_content_zh": "充电座供水箱没有防垢模块，请安装。",
    }, {
        "qid": "0260", "voice": "0260",
        "note": "PRD：充电状态下，再触发回充\n（0260）；复用语音0020",
        "prd_content_zh": "",
    }]
    page._apply_note_translations()
    assert page._prd_rows[0]["note"] == (
        "PRD：充电座供水箱没有防垢模块，请安装。\n（0930）")
    # 没有翻译时保持原文, 复用说明保留
    assert page._prd_rows[1]["note"] == (
        "PRD：充电状态下，再触发回充\n（0260）；复用语音0020")


def test_prd_content_translation_prefers_chinese(page, monkeypatch):
    """非中文 PRD 内容翻译成中文; 已有中文语音文案直接用(用户 2026-11-17)."""
    import gui.device_page as dp
    page._prd_rows = [
        {"qid": "0819", "voice": "0819",
         "note": "PRD：Need to add voice\n（0819）", "voice_text": "仅旋律"},
        {"qid": "0830", "voice": "0830",
         "note": "PRD：Please check temperature\n（0830）",
         "voice_text": "请检查基站温度。"},
    ]
    page._ai_config = lambda: {"base_url": "u", "api_key": "",
                               "model": "m"}
    calls = []

    def fake_translate(texts, *a, **k):
        calls.append(list(texts))
        return {texts[0]: "需要添加语音"}

    monkeypatch.setattr(dp.ai_prd, "translate_texts", fake_translate)
    page._ensure_prd_translations()
    assert page._prd_rows[1]["prd_content_zh"] == "请检查基站温度。"
    assert page._prd_rows[0]["prd_content_zh"] == "需要添加语音"
    assert calls == [["Need to add voice"]]


def test_prd_expected_rejects_ap_sound_when_prd_voice_missing(page):
    """PRD 语音文件缺失时, AP 自己的声音不能顶替(用户 2026-11-17: 0921)."""
    page._voice_check_result = {
        "checked": True,
        "missing": [{"file": "0921.mp3", "qids": ["0921"]}]}
    page._fault_expect = {"empty_robot_water_tank_done": {
        "name": "结束水箱排水", "sound": "0824", "led": {},
        "led_names": {}}}
    row = {"qid": "0921", "voice": "0921",
           "event": "empty_robot_water_tank_done",
           "note": "PRD：残留水清除已完成\n（0921）"}
    page._prd_rows = [row]
    exp = page._prd_expected("empty_robot_water_tank_done", prd_row=row)
    assert exp["sound"] == "0921"
    assert "acceptable_sounds" not in exp


def test_ap_missing_voice_marks_unconfigured(page):
    """PRD 要语音但 AP 没配声音: 整条判 AP平台未配置, 不因灯显对上算通过."""
    page._fault_expect = {"system_start": {
        "name": "系统启动完成事件", "sound": "",
        "led": {"rgb_led__start": "white_on"}, "led_names": {}}}
    prd = {"qid": "0001", "voice": "0001", "event": "system_start",
           "note": "", "name": "系统启动完成事件"}
    ver = {"overall": "通过",
           "voice": {"status": "AP平台未配置", "expected": "", "actual": ""},
           "led": {"status": "通过", "expected": "", "actual": ""},
           "code": {"status": "无", "expected": "", "actual": ""}}
    page._record_fault_row("system_start", "系统启动完成事件", ver, {},
                           prd_row=prd)
    row = page._fault_rows[-1]
    assert row["overall"] == "AP平台未配置"


def test_table_edit_preserves_fields_and_unmatched_rows(page):
    """编辑一个单元格不能清掉复用/不适用/中文翻译字段, 也不能删未匹配行."""
    page._prd_rows = [
        {"qid": "0066", "event": "a", "name": "A", "voice": "0066",
         "voice_text": "文案", "reuse_voice": "0020", "skip_voice": True,
         "comment": "Not apply this voice", "prd_content_zh": "中文",
         "note": "PRD：x\n（0066）", "fault_code": "1", "source": "ai",
         "event_options": []},
        {"qid": "0826", "event": "", "name": "未找到对应事件",
         "voice": "0826", "note": "PRD：提醒\n（0826）", "source": "ai"},
    ]
    page._fill_prd_table()
    rows = page._rows_from_table()
    assert len(rows) == 2
    keep = rows[0]
    assert keep["reuse_voice"] == "0020"
    assert keep["voice_text"] == "文案"
    assert keep["skip_voice"] is True
    assert keep["comment"] == "Not apply this voice"
    assert keep["prd_content_zh"] == "中文"
    assert rows[1]["qid"] == "0826" and rows[1]["event"] == ""


def test_batch_items_keep_all_rows_without_qid(page):
    """PDF/TXT 解析行没有 qid 时, 批量不能只保留第一条(用户 2026-11-17)."""
    page._prd_rows = [{"event": "a"}, {"event": "b"}, {"event": "c"}]
    items = page._batch_items()
    assert [x[0] for x in items] == ["a", "b", "c"]


def test_enrich_handles_rows_without_voice(page):
    """旧目录 + 无 voice 的行不能让 _enrich 抛 TypeError(用户 2026-11-17)."""
    page._prd_catalog = {"active": {66: {"qid": "Q066", "final": 66}},
                         "map": {66: 66}}
    page._fault_expect = {"a": {"name": "A", "sound": "", "led": {},
                                "led_names": {}}}
    page._prd_rows = [{"qid": "", "voice": "", "event": "a",
                       "name": "A", "note": ""}]
    page._enrich_prd_event_options()      # 不抛异常即可
    assert page._prd_rows[0]["event"] == "a"


def test_user_locked_event_not_overridden_by_forced(page):
    """人工确认的行不能被 Q 号级覆盖改回去(用户 2026-11-17)."""
    page._prd_catalog = {"active": {100: {
        "qid": "Q100", "condition": "休眠唤醒", "text": "",
        "chinese": "", "final": 100}}, "excluded": {}, "map": {100: 100}}
    page._fault_expect = {
        "exit_dormant": {"name": "退出休眠", "sound": "", "led": {},
                         "led_names": {}},
        "change_fan_level": {"name": "吸力切换", "sound": "", "led": {},
                             "led_names": {}}}
    page._prd_rows = [{
        "qid": "Q100", "voice": "100", "event": "change_fan_level",
        "name": "吸力切换", "source": "user",
        "note": "PRD：休眠唤醒\n（Q100）"}]
    page._enrich_prd_event_options()
    assert page._prd_rows[0]["event"] == "change_fan_level"


def test_start_batch_clears_previous_rows(page, monkeypatch):
    """新批量测试要清空上一批结果(用户 2026-11-17)."""
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.Yes))
    page._fault_rows = [{"qid": "old"}]
    page._fault_report_started = "old"
    page._start_batch([("a", "A", {"qid": "1", "event": "a"})])
    assert page._fault_rows == []
    assert page._fault_report_started == ""
    page._batch = None


def test_prd_expected_ignores_actual_led(page):
    """实测回填的灯显不能当期望值(否则永远通过)."""
    page._fault_expect = {"ev": {"name": "E", "sound": "", "led": {},
                                 "led_names": {}}}
    row = {"qid": "1", "voice": "0001", "event": "ev",
           "led_home": "red_on", "led_home_text": "红灯常亮",
           "led_source": "actual", "note": "PRD：x\n（1）"}
    page._prd_rows = [row]
    assert page._prd_expected("ev", prd_row=row)["led"] == {}


def test_prd_expected_skipped_voice_is_cancelled(page):
    """PRD 标注不使用的语音, 判定口径与语音包检查一致(不要求播报)."""
    page._fault_expect = {"ev": {"name": "E", "sound": "0001", "led": {},
                                 "led_names": {}}}
    row = {"qid": "1", "voice": "0001", "event": "ev",
           "skip_voice": True, "note": "PRD：x\n（1）"}
    page._prd_rows = [row]
    exp = page._prd_expected("ev", prd_row=row)
    assert exp["voice_cancelled"] is True
    assert exp["sound"] == ""


def test_unmatched_prd_rows_are_reported(page):
    """解析后没匹配到 AP 事件的行要进报告(用户 2026-11-17)."""
    page._prd_rows = [
        {"qid": "0826", "voice": "0826", "event": "",
         "note": "PRD：基站清洗盘清洗提醒\n（0826）"},
        {"qid": "0001", "voice": "0001", "event": "system_start",
         "note": "PRD：开机\n（0001）"},
    ]
    out = page._unmatched_prd_rows()
    assert len(out) == 1
    assert out[0]["qid"] == "0826"
    assert out[0]["overall"] == "无对应事件"
    assert "清洗盘清洗提醒" in out[0]["prd_content"]
    assert "AP平台" in out[0]["note"]


def test_ai_config_use_claude_code_toggle_removed(page):
    """Claude Code 解析方式已整体取消(用户 2026-11-17)."""
    assert "use_claude_code" not in page._ai_config()


def test_prd_shared_voice_keeps_left_right_names(page):
    """共用同一句语音时(如 Q036 轮组卡住), 中文名用 AP 事件名区分左右轮。"""
    shared = "轮组卡住，请检查"
    page._on_prd_parsed([
        {"event": "error_left_wheel_stuck", "voice": "036",
         "voice_text": shared, "fault_code": "157", "note": ""},
        {"event": "error_right_wheel_stuck", "voice": "036",
         "voice_text": shared, "fault_code": "158", "note": ""},
    ])
    names = [r["name"] for r in page._prd_rows]
    assert any("左轮" in n for n in names)
    assert any("右轮" in n for n in names)
    texts = [page.fault_prd_table.item(i, 1).text()
             for i in range(page.fault_prd_table.rowCount())]
    assert set(texts) == set(names)


def test_fault_log_single_latest_batch_appends(page):
    """单次触发只显示最新日志; 批量测试时多条才追加(用户 2026-11-12)。"""
    class Svc:
        def __init__(self, beep):
            self.beep = beep

        def simulate_fault(self, key):
            return True

        def fault_log_since(self, t0):
            return [(1.0, f"event:error_fan_stuck, beep:{self.beep}")]

    page._batch = None
    page._connected = True
    page._service = Svc("066")
    page._simulate_fault("fan_stuck")
    _pump_send(page)
    page._show_fault_result("fan_stuck", 0)
    assert "beep:066" in page.fault_log.toPlainText()
    # 第二次单次触发: 旧日志被替换
    page._service = Svc("999")
    page._simulate_fault("fan_stuck")
    _pump_send(page)
    page._show_fault_result("fan_stuck", 0)
    text = page.fault_log.toPlainText()
    assert "beep:999" in text and "beep:066" not in text

    # 批量模式: 多条追加上去
    page._batch = {"items": [], "idx": 0, "stop": False}
    page._service = Svc("111")
    page._simulate_fault("fan_stuck")
    _pump_send(page)
    page._show_fault_result("fan_stuck", 0)
    page._service = Svc("222")
    page._simulate_fault("fan_stuck")
    _pump_send(page)
    page._show_fault_result("fan_stuck", 0)
    text = page.fault_log.toPlainText()
    # 批量模式只写一行摘要(不再灌 30 行原始日志, 避免界面卡顿)
    assert "语音 111" in text and "语音 222" in text


def test_all_gui_combos_ignore_wheel(qapp):
    """所有下拉框都不响应滚轮, 只能点选(用户 2026-11-12)。"""
    from pathlib import Path
    from gui.widgets import NoWheelComboBox, _FitCombo
    assert issubclass(_FitCombo, NoWheelComboBox)
    class _Wheel:
        ignored = False
        def ignore(self):
            self.ignored = True
    ev = _Wheel()
    NoWheelComboBox().wheelEvent(ev)
    assert ev.ignored
    # 源码里不允许再直接实例化 QComboBox(会绕过滚轮保护)
    gui = Path(__file__).resolve().parent.parent / "gui"
    bad = [p.name for p in gui.glob("*.py")
           if "QComboBox()" in p.read_text(encoding="utf-8")]
    assert not bad, f"这些文件还在直接 new QComboBox: {bad}"


def test_prd_table_width_type_edit_context_and_led_autofill(page):
    """事件列加宽/类型可改/右键触发/实测灯显回填(用户 2026-11-12)。"""
    from PySide6.QtCore import Qt as _Qt
    page._on_prd_parsed([{
        "event": "error_fan_stuck", "name": "风机卡住", "kind": "错误",
        "voice": "066", "voice_text": "风机异常",
        "fault_code": "156", "note": ""}])
    table = page.fault_prd_table
    assert table.columnWidth(0) >= 190
    # 类型是四类下拉, 可手动修改
    type_combo = table.cellWidget(0, 2)
    assert type_combo is not None
    assert [type_combo.itemText(i) for i in range(type_combo.count())] ==         ["故障", "提醒", "通用", "基站"]
    type_combo.setCurrentIndex(type_combo.findText("提醒"))
    row0 = next(r for r in page._prd_rows
                if r["event"] == "error_fan_stuck")
    assert row0["kind"] == "提醒"
    # 右键事件名 → 手动触发
    class Svc:
        def __init__(self):
            self.called = None
        def post_event(self, topic, event):
            self.called = (topic, event)
            return True
        def fault_log_since(self, t0):
            return [
                (1.0, "application_frame.cpp:241 "
                      "event:error_fan_stuck, beep:066"),
                (1.0, "led_show_base.cpp:261 event_id: error_fan_stuck "
                      "data: {"),
                (1.0, '"rgb_led__home" : "red_slow_flick"'),
                (1.0, '"rgb_led__start" : "all_off"'),
            ]
    page._service = Svc()
    page._connected = True
    page._trigger_prd_event("error_fan_stuck")
    _pump_send(page)
    assert page._service.called == ("/hardware/error", "error_fan_stuck")
    # 灯显未配置: 触发后回填实际值
    assert page._prd_rows[0]["led_home"] == ""
    page._show_event_result("error_fan_stuck", "风机卡住", 0)
    row = page._prd_rows[0]
    assert row["led_home"] == "red_slow_flick"
    assert row["led_start"] == "all_off"
    assert table.columnWidth(4) >= 160 and table.columnWidth(5) >= 160
    # 灯显下拉显示中文名, 完整内容在 tooltip; 4=电源灯, 5=回充灯
    assert table.cellWidget(0, 5).currentText() == "red_slow_flick"
    assert table.cellWidget(0, 5).currentData() == "red_slow_flick"
    assert table.cellWidget(0, 4).currentData() == "all_off"
    assert json.loads(page.settings.value("apos/prd_parsed"))[0]["led_home"] == \
        "red_slow_flick"


def test_ai_event_list_includes_general_ap_events(page, tmp_path):
    """有 AP 配置时, AI 事件清单只允许配置里的事件(避免别的项目的无效事件)."""
    import json as _json
    cfg = tmp_path / "ap.json"
    cfg.write_text(_json.dumps([{
        "identifier": "clean_paused", "name": "暂停清扫事件",
        "rgb_hardware_function_set": [], "sound": {"identifier": "007"}}],
        ensure_ascii=False), encoding="utf-8")
    page._load_fault_config(str(cfg))
    events = {e["event"]: e["name"] for e in page._ai_event_list()}
    assert events == {"clean_paused": "暂停清扫事件"}
    # 没加载 AP 配置时才退回内置模拟事件
    page._fault_expect = {}
    builtin = {e["event"] for e in page._ai_event_list()}
    assert "error_fan_stuck" in builtin


def test_ai_worker_chunks_events():
    from gui.device_page import _AiPrdWorker
    events = [{"event": f"e{i}", "name": str(i)} for i in range(170)]
    parts = list(_AiPrdWorker.chunks(events))
    assert [len(p) for p in parts] == [80, 80, 10]
    assert [x["event"] for p in parts for x in p] == [e["event"] for e in events]


def test_prd_rows_dedupe_shared_voice_but_keep_left_right(page):
    """同一语音的事件合并为一行(左/右轮保留区分), 避免重复拖布/振动电机。"""
    page._on_prd_parsed([
        {"event": "error_kit_mop_motor_err", "voice": "150",
         "voice_text": "拖布电机异常", "kind": "错误", "note": ""},
        {"event": "error_mop_motor_stuck", "voice": "150",
         "voice_text": "拖布电机异常", "kind": "错误", "note": ""},
        {"event": "error_kit_vibration_motor_err", "voice": "151",
         "voice_text": "振动电机异常", "kind": "错误", "note": ""},
        {"event": "error_vibration_motor_stuck", "voice": "151",
         "voice_text": "振动电机异常", "kind": "错误", "note": ""},
    ])
    voices = [r["voice"] for r in page._prd_rows]
    assert voices.count("150") == 1 and voices.count("151") == 1
    assert len(page._prd_rows) == 2
    assert any("同一语音事件" in (r.get("note") or "")
               for r in page._prd_rows)


def test_prd_rows_match_catalog_count_and_drop_extra_events(page):
    """表格行数按 PRD 最终语音数; PRD 没有的语音不显示(用户 2026-11-12)。"""
    page._prd_catalog = {
        "active": {
            66: {"qid": "Q066", "kind": "错误", "condition": "风机异常",
                 "text": "风机异常", "chinese": "主机风机异常，请检查",
                 "final": 66},
            138: {"qid": "Q138", "kind": "错误", "condition": "风机开路",
                  "text": "风机异常", "chinese": "主机风机异常，请检查",
                  "final": 66},
        },
        "excluded": {},
        "map": {66: 66, 138: 66},
    }
    page._on_prd_parsed([
        {"event": "error_fan_stuck", "voice": "066", "voice_text": "风机异常",
         "kind": "错误", "note": ""},
        {"event": "error_kit_fan_err", "voice": "066", "voice_text": "风机异常",
         "kind": "错误", "note": ""},
        {"event": "not_in_prd", "voice": "999", "voice_text": "别的",
         "kind": "通用", "note": ""},
    ])
    # PRD 里有 Q066 和 Q138 两个事件(后者复用 Q066 的语音), 要各占一行
    assert len(page._prd_rows) == 2
    rows = {r["qid"]: r for r in page._prd_rows}
    assert set(rows) == {"Q066", "Q138"}
    # 语音列显示 PRD 自己的 Q 号; 复用语音单独保存
    assert rows["Q066"]["voice"] == "066"
    assert rows["Q138"]["voice"] == "138"
    assert rows["Q138"]["reuse_voice"] == "066"
    assert rows["Q066"]["note"].startswith("PRD：风机异常\n（Q066）")
    assert rows["Q138"]["note"].startswith("PRD：风机开路\n（Q138）")
    assert "复用语音Q066" in rows["Q138"]["note"]
    for row in rows.values():
        opts = {o["event"] for o in row["event_options"]}
        assert opts == {"error_fan_stuck", "error_kit_fan_err"}
    assert page.fault_prd_table.rowCount() == 2


def test_prd_rows_show_voice_index_when_prd_has_no_q(page):
    """PRD 没有 Q 号时显示实际语音序号, 不能补 Q(用户 2026-11-17)。"""
    page._prd_catalog = {
        "active": {66: {"qid": "Q066", "label": "0066", "kind": "错误",
                        "condition": "风机异常", "text": "风机异常",
                        "chinese": "主机风机异常，请检查", "final": 66}},
        "excluded": {}, "map": {66: 66},
    }
    page._on_prd_parsed([
        {"event": "error_fan_stuck", "voice": "0066",
         "voice_text": "风机异常", "kind": "错误", "note": ""},
    ])
    row = page._prd_rows[0]
    assert row["qid"] == "0066"
    assert row["note"].startswith("PRD：风机异常\n（0066）")


def test_backfill_skip_voice_from_catalog_string_keys(page):
    """旧结果按语音号补 comment/skip_voice(目录键是字符串也要命中)."""
    page._prd_catalog = {"active": {
        "42": {"qid": "Q042", "label": "0042",
               "comment": "[LG]  Not apply this voice"},
        "44": {"qid": "Q044", "label": "0044",
               "comment": "[LG] Need to explain"},
    }}
    page._prd_rows = [
        {"qid": "0042", "voice": "0042", "voice_text": "连接失败"},
        {"qid": "0044", "voice": "0044", "voice_text": "马上离开当前区域"},
    ]
    page._backfill_prd_skip_voice()
    assert page._prd_rows[0]["skip_voice"] is True
    assert "Not apply" in page._prd_rows[0]["comment"]
    assert page._prd_rows[1]["skip_voice"] is False


def test_old_qid_relabeled_from_catalog_label(page):
    """旧解析结果里的 Q819 → PRD 实际编号 0819; 真正带 Q 的项目不动。"""
    page._prd_catalog = {"active": {
        819: {"qid": "Q819", "label": "0819"},
        66: {"qid": "Q066"},
    }, "excluded": {}, "map": {}}
    page._prd_rows = [
        {"qid": "Q819", "event": "a", "note": "PRD：污水箱取出\n（Q819）"},
        {"qid": "Q066", "event": "b", "note": "PRD：风机异常\n（Q066）"},
    ]
    page._relabel_prd_ids()
    assert page._prd_rows[0]["qid"] == "0819"
    assert "（0819）" in page._prd_rows[0]["note"]
    # 没有 label 的条目保持原样(HS4 本来就是 Q 号)
    assert page._prd_rows[1]["qid"] == "Q066"


def test_memory_prefill_covers_voice_and_records_user(page, tmp_path):
    """记忆库命中的语音不再发 AI; 人工修改写回记忆库(用户 2026-11-12)。"""
    from core.apos.prd_memory import PrdMemory, voice_key
    from core.apos import prd_memory
    page._prd_memory = PrdMemory(str(tmp_path / "mem.json"))
    page._prd_catalog = {"active": {
        66: {"qid": "Q066", "kind": "错误", "condition": "风机异常",
             "text": "风机异常", "chinese": "主机风机异常，请检查",
             "final": 66}},
        "excluded": {}, "map": {66: 66}}
    page._prd_memory.put(voice_key("风机异常", "主机风机异常，请检查"),
                         {"event": "error_fan_stuck", "kind": "故障",
                          "fault_code": "999", "fault_push": "OLD",
                          "qid": "Q999", "voice": "999"}, source="user")
    items, covered = page._memory_prefill()
    assert covered == {66}
    assert items[0]["event"] == "error_fan_stuck"
    # 只复用事件映射; 旧项目 Q 号/故障码不能带进当前项目(用户 2026-11-16)
    assert items[0]["qid"] == "Q066"
    assert items[0]["fault_code"] == "" and items[0]["fault_push"] == ""
    assert items[0]["voice"] == "066"
    assert items[0]["source"] == "user"     # 保留记忆来源, 锁定审核结果
    # 人工编辑写回, 且类型归一
    page._prd_rows = [{"event": "error_fan_stuck", "voice": "066",
                       "voice_key": voice_key("风机异常", "主机风机异常，请检查"),
                       "kind": "提醒", "voice_text": "主机风机异常，请检查"}]
    page._remember_rows(page._prd_rows, source="user")
    hit = page._prd_memory.exact(page._prd_rows[0]["voice_key"])
    assert hit["kind"] == "提醒" and hit["source"] == "user"


def test_memory_prefill_remaps_old_project_event(page, tmp_path):
    """旧项目 AP event 不存在时, 按中文语义映射到当前项目事件(用户 2026-11-16)."""
    from core.apos.prd_memory import PrdMemory, voice_key
    page._prd_memory = PrdMemory(str(tmp_path / "mem2.json"))
    page._fault_expect = {
        "error_fan_stuck": {"name": "风机卡住", "sound": {"identifier": "066"}},
        "error_drop": {"name": "地检异常", "sound": {"identifier": "117"}},
    }
    page._prd_catalog = {"active": {
        66: {"qid": "Q066", "kind": "错误", "condition": "风机异常",
             "chinese": "主机风机异常，请检查", "final": 66}},
        "excluded": {}, "map": {66: 66}}
    page._prd_memory.put(
        voice_key("风机异常", "主机风机异常，请检查"),
        {"event": "old_project_fan_event", "event_name": "风机卡住",
         "fault_code": "999", "qid": "Q999"}, source="user")
    items, covered = page._memory_prefill()
    assert covered == {66}
    assert items[0]["event"] == "error_fan_stuck"
    assert items[0]["qid"] == "Q066" and items[0]["fault_code"] == ""


def test_multi_event_voice_has_event_dropdown_and_learns(page, tmp_path):
    """一条语音匹配多个事件 → 事件列下拉选择, 修改写回记忆库(用户 2026-11-12)。"""
    from core.apos.prd_memory import PrdMemory
    page._prd_memory = PrdMemory(str(tmp_path / "m.json"))
    page._prd_catalog = {"active": {
        66: {"qid": "Q066", "kind": "错误", "condition": "风机异常",
             "text": "风机异常", "chinese": "主机风机异常，请检查",
             "final": 66}},
        "excluded": {}, "map": {66: 66}}
    page._on_prd_parsed([
        {"event": "error_fan_stuck", "voice": "066", "kind": "故障",
         "fault_code": "926", "fault_push": "", "source": "ai"},
        {"event": "error_kit_fan_err", "voice": "066", "kind": "故障",
         "fault_code": "138", "fault_push": "DA_RC_E_138", "source": "ai"},
    ])
    combo = page._event_combo_at(0)
    assert combo is not None
    from PySide6.QtCore import Qt as _Qt
    assert combo.contextMenuPolicy() == _Qt.CustomContextMenu
    items = {combo.itemText(i) for i in range(combo.count())}
    assert items == {"error_fan_stuck", "error_kit_fan_err"}
    table = page.fault_prd_table
    table.item(0, 7).setText("备注X")
    combo.setCurrentIndex(combo.findText("error_kit_fan_err"))
    row = page._prd_rows[0]
    assert row["event"] == "error_kit_fan_err"
    # 切换事件只跟随 AP 中文名; 故障码/灯显/类型/备注都保持原值(用户 2026-11-13)
    assert row["fault_code"] == "926"
    assert row["kind"] == "故障"
    assert row["note"] == "备注X"
    assert row["led_home"] == "" and row["led_start"] == ""
    # 改成提醒后没有故障码
    type_combo = table.cellWidget(0, 2)
    type_combo.setCurrentIndex(type_combo.findText("提醒"))
    row = page._prd_rows[0]
    assert row["kind"] == "提醒" and row["fault_code"] == ""
    hit = page._prd_memory.exact(row["voice_key"])
    assert hit["event"] == "error_kit_fan_err" and hit["source"] == "user"


def test_candidate_events_only_for_unmatched_voices(page):
    """只把未命中语音的 Top-N 候选事件发给 AI, 减少请求量(用户 2026-11-12)。"""
    import numpy as np

    class FakeEmb:
        available = True

        def encode(self, texts):
            out = []
            for t in texts:
                v = np.array([1.0 if "风机" in t else 0.0,
                              1.0 if "清扫" in t else 0.0, 0.1])
                out.append(v / np.linalg.norm(v))
            return np.array(out)

    page._embedder_override = FakeEmb()
    page._prd_catalog = {"active": {
        66: {"qid": "Q066", "kind": "错误", "condition": "风机异常",
             "text": "风机异常", "chinese": "主机风机异常，请检查",
             "final": 66}},
        "excluded": {}, "map": {66: 66}}
    all_ev = page._ai_event_list()
    picked = page._candidate_events_for([66])
    # 向量候选 + 中文关键字候选, 上限比纯向量稍宽
    assert 0 < len(picked) <= 20
    assert any("风机" in (e.get("name") or "") for e in picked)
    assert len(picked) < len(all_ev) or len(all_ev) <= 20
    # 没有本地模型时退回全部事件, 不能漏
    page._embedder_override = False
    assert len(page._candidate_events_for([66])) == len(all_ev)


def test_clear_button_removed_and_batch_reset(page):
    """清除故障按钮去掉; 批量结束用 StopClean/StopFindCharger 复位."""
    assert not hasattr(page, "fault_clear_btn")

    class Svc:
        def stop_tasks(self):
            return [("/TaskManager/StopClean", 0),
                    ("/GoBackStationTask/StopFindCharger", 0)]

    page._service = Svc()
    page._reset_robot_state()
    text = page.fault_log.toPlainText()
    assert "StopClean=0" in text and "StopFindCharger=0" in text


def test_wrapped_combo_uses_item_tooltip(page):
    """下拉框选中项悬停要显示 ToolTipRole 里的中文名(用户 2026-11-16)."""
    from PySide6.QtCore import Qt
    from gui.widgets import WrappedCombo
    combo = WrappedCombo()
    combo.addItem("error_fan_stuck", "error_fan_stuck")
    combo.setItemData(0, "风机卡住", Qt.ToolTipRole)
    combo.setCurrentIndex(0)
    combo._sync_wrap()
    assert combo.toolTip() == "风机卡住"
    assert combo._wrap.toolTip() == "风机卡住"


def test_batch_voice_check_missing_is_logged(page):
    """批量前语音包检查缺失要进入状态/日志, 并保留给报告(用户 2026-11-16)."""
    page._batch_pending_items = []
    page._on_voice_checked({
        "checked": True, "files": ["003.mp3"], "error": "",
        "missing": [{"file": "166.mp3", "qids": ["Q166"],
                     "sources": [{"qid": "Q166",
                                  "condition": "集尘过于频繁",
                                  "field": "PRD语音"}]}]})
    assert page._voice_check_result["missing"][0]["file"] == "166.mp3"
    assert "语音包缺失" in page.fault_status.text()
    assert "Q166" in page.fault_log.toPlainText()


def test_space_duplicate_events_moves_duplicate_after_next(qapp):
    """相邻重复事件要错开: 先执行下一条, 再执行重复的那条（用户 2026-11-16)."""
    from gui.device_page import _space_duplicate_events

    items = [("a", "A", None), ("a", "A", None), ("b", "B", None)]
    out = _space_duplicate_events(items)
    assert [x[0] for x in out] == ["a", "b", "a"]
    # 重复项在末尾(没有「下下条」): 和前面元素对调, 同样不能相邻
    tail = _space_duplicate_events(
        [("b", "B", None), ("c", "C", None),
         ("a", "A", None), ("a", "A", None)])
    assert all(tail[i][0] != tail[i + 1][0] for i in range(len(tail) - 1))
    assert sorted(x[0] for x in tail) == ["a", "a", "b", "c"]
    # 三个连续重复 + 后面有其它事件: 全部拆开
    out2 = _space_duplicate_events(
        [("a", "A", None), ("a", "A", None), ("a", "A", None),
         ("b", "B", None), ("c", "C", None)])
    assert all(out2[i][0] != out2[i + 1][0] for i in range(len(out2) - 1))


def test_start_batch_spaces_consecutive_duplicates(page, monkeypatch):
    """_start_batch 真正执行前会把相邻重复事件错开."""
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.Yes)
    monkeypatch.setattr(page, "_batch_step", lambda: None)
    page._start_batch([("a", "A", None), ("a", "A", None),
                       ("b", "B", None)])
    assert [x[0] for x in page._batch["items"]] == ["a", "b", "a"]
    page._batch = None


def test_stream_ready_checks_upload_stream(page):
    class S:
        class stream:
            running = False

    page._service = S()
    assert page._stream_ready() is False
    S.stream.running = True
    assert page._stream_ready() is True

    class Plain:
        pass

    page._service = Plain()
    assert page._stream_ready() is True      # 无 stream 的桩不拦
    page._service = None
    assert page._stream_ready() is False


def test_batch_pauses_until_stream_recovers(page):
    """机器人网络重启导致断流时, 批量不能把后续事件全记成失败."""
    class Svc:
        class stream:
            running = False

        def post_event(self, topic, event):
            raise AssertionError("通道未恢复时不能发送")

        def fault_log_since(self, t0):
            return []

    page._service = Svc()
    page._connected = True
    page._batch = {"items": [("a", "A", None)], "idx": 0, "stop": False}
    page._batch_step()
    assert page._batch["idx"] == 0
    assert "等待恢复" in page.fault_status.text()
    assert page._batch["wait_secs"] == 2
    page._batch = None          # 清掉, 避免测试结束后定时器再触发


def test_batch_send_failure_retries_same_item(page):
    """发送失败(通道断)时保持当前条目, 等恢复重测而不是跳过."""
    class Svc:
        def post_event(self, topic, event):
            return False

        def fault_log_since(self, t0):
            return []

    page._service = Svc()
    page._connected = True
    page._batch = {"items": [("error_fan_stuck", "风机卡住", None)],
                   "idx": 0, "stop": False}
    assert page._trigger_event("error_fan_stuck", "风机卡住",
                               then=page._batch_after) is True
    _pump_send(page)
    assert page._batch["idx"] == 0
    assert "发送失败" in page.fault_status.text()
    assert "重测本条" in page.fault_log.toPlainText()
    page._batch = None


def test_batch_items_and_report_dedupe_by_prd_event(page):
    """同一 AP 事件对应多个 PRD 事件时各测一条, 报告按 PRD 事件去重."""
    page._prd_rows = [
        {"qid": "Q066", "event": "error_fan_stuck", "name": "风机卡住",
         "voice_text": "主机风机异常"},
        {"qid": "Q024", "event": "error_fan_stuck", "name": "风机卡住",
         "voice_text": "无尘盒触发工作"},
        {"qid": "Q014", "event": "charging", "name": "开始充电",
         "voice_text": "开始充电"},
    ]
    items = page._batch_items()
    assert [(e, r.get("qid")) for e, _l, r in items] == [
        ("error_fan_stuck", "Q066"), ("error_fan_stuck", "Q024"),
        ("charging", "Q014")]
    page._fault_rows = [
        {"qid": "Q066", "event": "error_fan_stuck", "name": "风机卡住",
         "prd_content": "主机风机异常", "time": "2026-11-13 10:00:00",
         "overall": "不一致", "voice_actual": "066"},
        {"qid": "Q014", "event": "charging", "name": "开始充电",
         "prd_content": "开始充电", "time": "2026-11-13 10:00:03",
         "overall": "通过", "voice_actual": "014"},
        {"qid": "Q024", "event": "error_fan_stuck", "name": "风机卡住",
         "prd_content": "无尘盒触发工作", "time": "2026-11-13 10:00:06",
         "overall": "通过", "voice_actual": "020"},
        {"qid": "Q066", "event": "error_fan_stuck", "name": "风机卡住",
         "prd_content": "主机风机异常", "time": "2026-11-13 10:00:09",
         "overall": "通过", "voice_actual": "077"},
    ]
    rows = page._report_rows()
    # 同一 AP 事件的两个不同 PRD 事件都保留, 且带各自 PRD 内容
    assert [(r["qid"], r["prd_content"]) for r in rows] == [
        ("Q066", "主机风机异常"), ("Q014", "开始充电"),
        ("Q024", "无尘盒触发工作")]
    assert rows[0]["overall"] == "不一致"   # 同一 PRD 事件保留失败结果


def test_low_battery_candidates_exclude_clean_tasks(page):
    """清扫中低电不能把清扫任务事件当候选(用户 2026-11-16)."""
    page._fault_expect = {
        "low_battery_find_charger": {
            "name": "电量低启动回充事件", "sound": {"identifier": "011"}},
        "clean_finish": {"name": "清扫完成事件",
                         "sound": {"identifier": "010"}},
        "clean_paused": {"name": "暂停清洁事件",
                         "sound": {"identifier": "007"}},
        "low_battery_to_clean": {
            "name": "电量太低无法启动清扫事件",
            "sound": {"identifier": "017"}},
    }
    page._prd_catalog = {
        "active": {11: {"qid": "Q011", "condition": "清洁中电量不足",
                        "chinese": "电量不足，返回基站",
                        "text": "电量不足，返回基站", "final": 11}},
        "excluded": {}, "map": {11: 11},
    }
    page._prd_rows = [{
        "qid": "Q011", "event": "low_battery_find_charger",
        "name": "电量低启动回充事件", "voice": "011", "source": "verified",
        "voice_text": "电量不足，返回基站",
        "event_options": [{"event": "low_battery_find_charger"},
                          {"event": "clean_finish"},
                          {"event": "clean_paused"},
                          {"event": "low_battery_to_clean"}],
    }]
    page._enrich_prd_event_options()
    opts = {o["event"] for o in page._prd_rows[0]["event_options"]}
    assert "low_battery_find_charger" in opts
    assert "low_battery_to_clean" in opts
    assert "clean_finish" not in opts
    assert "clean_paused" not in opts


def test_water_start_does_not_offer_mop_wash_candidate(page):
    """开始加水不能把开始洗拖布当候选; 开始洗拖布可以选加水事件(用户 2026-11-16)."""
    page._fault_expect = {
        "add_water_start": {"name": "加水开始事件",
                            "sound": {"identifier": "290"}},
        "clean_mop_start": {"name": "开始清洗拖布事件",
                            "sound": {"identifier": "290"}},
    }
    page._prd_catalog = {
        "active": {290: {"qid": "Q290", "condition": "开始加水",
                         "chinese": "开始加水", "text": "开始加水",
                         "final": 290}},
        "excluded": {}, "map": {290: 290},
    }
    page._prd_rows = [{
        "qid": "Q290", "event": "add_water_start",
        "name": "加水开始事件", "voice": "290", "source": "user",
        "voice_text": "开始加水",
        "event_options": [{"event": "add_water_start"},
                          {"event": "clean_mop_start"}],
    }]
    page._enrich_prd_event_options()
    opts = {o["event"] for o in page._prd_rows[0]["event_options"]}
    assert "add_water_start" in opts
    assert "clean_mop_start" not in opts


def test_candidate_filter_removes_unrelated_same_sound_events(page):
    """同语音但对象不同的 AP 事件不能作为候选(用户 2026-11-13)."""
    page._fault_expect = {
        "error_kit_side_brush_err": {
            "name": "边刷开路或短路", "sound": {"identifier": "137"}},
        "error_kit_fan_err": {
            "name": "风机开路或短路", "sound": {"identifier": "137"}},
        "error_kit_left_wheel_err": {
            "name": "左轮开路或短路", "sound": {"identifier": "137"}},
    }
    page._prd_catalog = {
        "active": {137: {"qid": "Q137", "condition": "边刷异常，请检查",
                         "chinese": "边刷异常，请检查",
                         "text": "边刷异常，请检查", "final": 137}},
        "excluded": {}, "map": {137: 137},
    }
    page._prd_rows = [{
        "qid": "Q137", "event": "error_kit_side_brush_err",
        "name": "边刷开路或短路", "voice": "137", "source": "user",
        "voice_text": "边刷异常，请检查",
        "event_options": [
            {"event": "error_kit_side_brush_err"},
            {"event": "error_kit_fan_err"},
            {"event": "error_kit_left_wheel_err"},
        ],
    }]
    page._enrich_prd_event_options()
    opts = {o["event"] for o in page._prd_rows[0]["event_options"]}
    assert "error_kit_side_brush_err" in opts
    assert "error_kit_fan_err" not in opts
    assert "error_kit_left_wheel_err" not in opts


def test_enrich_selects_pickup_for_wheel_lifted(page):
    """轮子离地要选 pickup, 不能留在低电量事件(用户 2026-11-13)."""
    page._fault_expect = {
        "pickup": {"name": "离地检测异常",
                   "sound": {"identifier": "125"}},
        "low_battery_to_clean": {
            "name": "电量太低无法启动清扫事件",
            "sound": {"identifier": "124"}},
    }
    page._prd_catalog = {
        "active": {124: {"qid": "Q124",
                         "condition": "启动清扫，轮子离地开关触发",
                         "chinese": "轮子悬空，机器人无法正常启动",
                         "text": "轮子悬空，机器人无法正常启动",
                         "final": 124}},
        "excluded": {}, "map": {124: 124},
    }
    page._prd_rows = [{
        "qid": "Q124", "event": "low_battery_to_clean",
        "name": "电量太低无法启动清扫事件", "voice": "124",
        "source": "ai", "voice_text": "轮子悬空，机器人无法正常启动",
        "event_options": [{"event": "low_battery_to_clean"}],
    }]
    page._enrich_prd_event_options()
    row = page._prd_rows[0]
    assert row["event"] == "pickup"
    assert {o["event"] for o in row["event_options"]} == {"pickup"}


def test_fine_grained_scenario_keywords(page):
    """细分场景关键字: 离地/摆臂/上桩不能被同类事件抢走(用户 2026-11-13)."""
    assert page._event_rank(
        "启动清扫，轮子离地开关触发", "pickup") > page._event_rank(
        "启动清扫，轮子离地开关触发", "low_battery_to_clean")
    assert page._event_rank(
        "边刷摆臂开路或短路", "error_side_brush_telescoping") > \
        page._event_rank("边刷摆臂开路或短路",
                         "error_kit_side_brush_err")
    assert page._event_rank(
        "回充失败：找到基站，但是上桩失败或者超时", "back_charger_fail") > \
        page._event_rank("回充失败：找到基站，但是上桩失败或者超时",
                         "infrared_guidance_back_charger_fail")
    assert page._domain_hits(
        "沿墙传感器开路", "error_psd_dirty", "沿墙传感器异常") >= 1
    assert page._event_rank(
        "基站开始拖布清洗", "add_water_start") > page._event_rank(
        "基站开始拖布清洗", "clean_mop_start")
    assert page._is_water_only("开始加水")
    assert not page._is_water_only("开始拖布清洗")


def test_on_charger_low_battery_start_maps_to_on_charger_event(page):
    """「低电启动任务（在桩）」→ low_battery_to_clean_on_charger(用户 2026-11-17)."""
    text = "充电状态下，仍低电，手动触发清扫任务/低电启动任务（在桩）"
    assert page._scenario_allow(text) == (
        "low_battery_to_clean_on_charger",)
    # 不在桩的低电启动仍走原来的充电低电提醒
    assert page._scenario_allow("充电中，仍低电") == (
        "app_sweeper_enter_low_battery_charging",)
    # 充电状态下再触发回充: AP 没有「已在基座」事件, 不能套继续回充
    assert page._scenario_allow("充电状态下，再触发回充") == ()
    # 防垢模块提示 AP 也没有对应事件
    assert page._scenario_allow(
        "充电座供水箱中没有防垢模块。请安装防垢模块。") == ()
    assert page._scenario_allow(
        "충전대 급수통에 스케일 방지 모듈이 없습니다.") == ()


def test_batch_expected_uses_each_prd_row(page):
    """同一 AP 事件多个 PRD 行时, 批量按各自 PRD 行算期望(用户 2026-11-13)."""
    row_a = {"qid": "Q131", "event": "error_kit_lidar_err",
             "voice": "131", "reuse_voice": "028", "voice_text": "雷达无数据"}
    row_b = {"qid": "Q139", "event": "error_kit_lidar_err",
             "voice": "139", "voice_text": "激光传感器异常"}
    page._prd_rows = [row_a, row_b]
    assert page._expected_for("error_kit_lidar_err",
                              prd_row=row_a)["sound"] == "131"
    assert page._expected_for("error_kit_lidar_err",
                              prd_row=row_b)["sound"] == "139"


def test_batch_report_writes_deduped_rows(page, tmp_path, monkeypatch):
    """自动报告按 PRD 事件去重并带 PRD 内容写入 Excel(用户 2026-11-13)."""
    import gui.device_page as dp
    captured = {}
    monkeypatch.setattr(
        dp.hs4_config, "make_report",
        lambda _path, rows, *a, **k: captured.update(rows=rows))
    monkeypatch.setattr(page, "_batch_report_path",
                        lambda: str(tmp_path / "dup.xlsx"))
    page._fault_rows = [
        {"qid": "Q110", "event": "mop_clean_start", "name": "开始拖地",
         "prd_content": "开始拖地", "time": "t1"},
        {"qid": "Q110", "event": "mop_clean_start", "name": "开始拖地",
         "prd_content": "开始拖地", "time": "t2"},
        {"qid": "Q006", "event": "mop_clean_start", "name": "开始拖地",
         "prd_content": "边刷/拖地启动", "time": "t3"},
        {"qid": "Q014", "event": "charging", "name": "开始充电",
         "prd_content": "开始充电", "time": "t4"},
    ]
    page._save_batch_report()
    assert [(r["qid"], r["prd_content"]) for r in captured["rows"]] == [
        ("Q110", "开始拖地"), ("Q006", "边刷/拖地启动"),
        ("Q014", "开始充电")]


def test_batch_report_auto_named_and_saved(page, tmp_path, monkeypatch):
    """批量报告自动存到 reports: 项目_基站版本_时间; 不写设备 IP。"""
    import core.driver as driver
    monkeypatch.setattr(driver, "BASE_DIR", str(tmp_path))
    page._device_info = {"model": "CLA_HS4", "firmware_version": "8.2.1.1",
                         "base_station_version": "2.0.5"}
    page._connected_ip = "10.0.0.5"
    path = page._batch_report_path()
    name = path.replace("\\", "/").split("/")[-1]
    # 名称 = 项目 + 固件版本 + 时间; 不用基站版本
    assert name.startswith("故障模拟_CLA_HS4_8.2.1.1_")
    assert "2.0.5" not in name and "10.0.0.5" not in name
    assert name.endswith(".xlsx")
    page._fault_rows = [{
        "time": "2026-11-12 10:00:00", "event": "error_fan_stuck",
        "name": "风机卡住", "group": "硬件故障", "overall": "通过",
        "voice_expected": "066", "voice_actual": "066",
        "voice_status": "通过", "voice_note": "", "led_expected": "",
        "led_actual": "", "led_status": "未配置", "led_note": "",
        "code_actual": "", "code_status": "无", "code_note": "", "note": "",
    }]
    page._save_batch_report()
    assert os.path.exists(path)
    assert "已自动保存" in page.fault_status.text()


def test_save_load_prd_config_yaml(page, tmp_path, monkeypatch):
    """解析配置可保存/读取 YAML(事件/语音/灯显/故障码)(用户 2026-11-12)。"""
    import gui.device_page as dp
    page._prd_rows = [{
        "event": "error_fan_stuck", "name": "风机卡住", "kind": "故障",
        "voice": "066", "voice_text": "风机异常", "fault_code": "926",
        "fault_push": "DA_RC_E_926", "note": "说明",
        "led_home": "red_slow_flick", "led_home_text": "红灯慢闪",
        "led_start": "all_flick", "led_start_text": "闪烁3次"}]
    page._fill_prd_table()
    y = tmp_path / "cfg.yaml"
    monkeypatch.setattr(dp.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(y), "")))
    page._save_prd_config_file()
    assert y.exists()
    text = y.read_text(encoding="utf-8")
    assert "events:" in text and "error_fan_stuck" in text
    import yaml
    data = yaml.safe_load(text)
    ev = data["events"][0]
    assert ev["voice"] == "066" and ev["fault_code"] == "926"
    assert ev["led_home"] == "red_slow_flick"

    # 清空后从 YAML 读取回来
    page._prd_rows = []
    page._fill_prd_table()
    monkeypatch.setattr(dp.QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: (str(y), "")))
    page._load_prd_config_file()
    assert len(page._prd_rows) == 1
    row = page._prd_rows[0]
    assert row["event"] == "error_fan_stuck"
    assert row["voice"] == "066" and row["fault_code"] == "926"
    assert row["led_home"] == "red_slow_flick"
    assert page.fault_prd_table.rowCount() == 1


def test_name_match_adds_psd_dirty_candidate(page):
    """沿墙传感器异常要能匹配到 error_psd_dirty, 并进入事件下拉(用户 2026-11-12)。"""
    page._prd_catalog = {"active": {
        30: {"qid": "Q030", "kind": "错误", "condition": "沿墙传感器开路",
             "text": "沿墙传感器异常，请检查",
             "chinese": "沿墙传感器异常，请检查", "final": 30}},
        "excluded": {}, "map": {30: 30}}
    page._on_prd_parsed([{
        "event": "error_self_test_monitor_wall_follow_duration_min",
        "name": "rtt沿墙时间校验异常", "voice": "030",
        "voice_text": "沿墙传感器异常，请检查", "kind": "故障",
        "source": "ai"}])
    row = page._prd_rows[0]
    opts = {o["event"] for o in (row.get("event_options") or [])}
    assert "error_psd_dirty" in opts
    assert row["event"] == "error_psd_dirty"   # 名称匹配优先于 AI 猜测


def test_prd_event_column_auto_fits_long_names(page):
    """事件列按内容自适应(有上限), 余下宽度留给故障码/备注(用户 2026-11-12)。

    ★ 2026-11-12 调整: 8 列必须在一屏内显示, 事件列不再无限加宽到 620;
      页面显示后会按视口压缩, 长事件名看 tooltip/下拉。
    """
    long_ev = "error_self_test_monitor_wall_follow_duration_min"
    page.fault_prd_table.hide()
    page._prd_rows = [{
        "event": long_ev, "name": "沿墙传感器异常", "kind": "故障",
        "voice": "030", "voice_text": "沿墙传感器异常",
        "fault_code": "", "note": ""}]
    page._fill_prd_table()
    width = page.fault_prd_table.columnWidth(0)
    need = page.fault_prd_table.fontMetrics().horizontalAdvance(long_ev)
    assert width >= min(need + 20, 348)
    assert width > 200


def test_fault_log_also_writes_to_tool_log(page, caplog):
    """故障模拟页日志要同时进 test.log, 方便事后排查(用户 2026-11-13)."""
    import logging
    with caplog.at_level(logging.INFO, logger="vacuum_test"):
        page._fault_log_line("排查关键字ABC", "#dc2626")
        page._fault_log_add(
            '<span style="color:#16a34a">设备原始日志XYZ</span>')
    text = caplog.text
    assert "[故障模拟]" in text
    assert "排查关键字ABC" in text
    assert "设备原始日志XYZ" in text
    assert "<span" not in text


def test_fault_log_follows_latest_and_pauses_on_user_scroll(page, qapp):
    """日志默认滚到最新; 用户滚上去后暂停, 20 秒无操作恢复(2026-11-13)."""
    page.fault_page.resize(1040, 640)
    page.fault_page.show()
    for i in range(80):
        page._fault_log_line(f"第 {i} 行")
        qapp.processEvents()
    bar = page.fault_log.verticalScrollBar()
    assert bar.maximum() > 0
    assert bar.value() >= bar.maximum() - 2      # 默认跟随最新
    assert page._log_idle_timer.interval() == 20000

    # 模拟用户滚到顶部: 暂停自动跟随
    page._log_user_scrolled()
    bar.setValue(0)
    qapp.processEvents()
    page._log_check_bottom()
    assert page._log_auto_scroll is False
    before = bar.value()
    page._fault_log_line("新日志")
    qapp.processEvents()
    assert bar.value() == before                  # 不抢用户滚动位置

    # 20 秒无操作后恢复自动跟随
    page._resume_log_autoscroll()
    qapp.processEvents()
    assert page._log_auto_scroll is True
    assert bar.value() >= bar.maximum() - 2


def test_fault_config_saves_under_tool_dir(page, tmp_path, monkeypatch):
    """解析配置默认存到 工具目录/config/fault_events(用户 2026-11-13)."""
    import os
    import core.driver as driver
    import gui.device_page as dp
    monkeypatch.setattr(driver, "BASE_DIR", str(tmp_path))
    captured = {}

    def fake_save(_parent, _title, default, _flt):
        captured["default"] = default
        return "", ""

    monkeypatch.setattr(dp.QFileDialog, "getSaveFileName",
                        staticmethod(fake_save))
    page._prd_rows = [{"event": "error_fan_stuck", "name": "风机卡住",
                       "voice": "066"}]
    page._device_info = {"model": "CLA_HS4"}
    page._save_prd_config_file()
    default = captured["default"]
    expect_dir = os.path.join(str(tmp_path), "config", "fault_events")
    assert os.path.dirname(default) == expect_dir
    # 命名: 项目名称_故障模拟配置_时间(用户 2026-11-17)
    assert os.path.basename(default).startswith("CLA_HS4_故障模拟配置_")
    assert os.path.basename(default).endswith(".yaml")
    assert os.path.isdir(expect_dir)

    # 没连接时用 PRD 文件名兜底项目名
    page._device_info = {}
    page._connected_ip = ""
    page.fault_prd_label.setText(str(tmp_path / "CLA_HS4_PRD.xlsx"))
    page._prd_path = str(tmp_path / "CLA_HS4_PRD.xlsx")
    page._save_prd_config_file()
    assert os.path.basename(captured["default"]).startswith(
        "CLA_HS4_PRD_故障模拟配置_")


def test_batch_skips_shutdown_event(page):
    """关机事件会让设备断电, 批量测试必须跳过(用户 2026-11-13)."""
    class Svc:
        called = None

        def post_event(self, topic, event):
            self.called = (topic, event)
            return True

    page._service = Svc()
    page._connected = True
    page._batch = {"items": [], "idx": 0, "stop": False}
    assert page._trigger_event("shutdown") is False
    assert page._service.called is None
    assert "跳过" in page.fault_status.text()
    # 恢复出厂设置验证不会实际生效, 不跳过
    assert "request_factory_reset" not in page._DESTRUCTIVE_EVENTS


def test_event_matching_uses_chinese_meaning_not_only_voice(page):
    """事件匹配优先看 AP 中文含义, 不能只按语音号(Q107 音量 vs 风机)."""
    page._fault_expect = {
        "change_fan_level": {"name": "风机档位改变事件",
                             "sound": {"identifier": "107"}},
        "change_volum": {"name": "音量改变事件",
                         "sound": {"identifier": "999"}},
    }
    page._prd_catalog = {
        "active": {107: {"qid": "Q107", "kind": "通用",
                         "condition": "设置音量生效",
                         "text": "音量调节成功", "chinese": "音量调节成功",
                         "final": 107}},
        "excluded": {}, "map": {107: 107},
    }
    page._on_prd_parsed([{"event": "change_fan_level", "voice": "107",
                          "kind": "通用", "source": "ai"}])
    row = page._prd_rows[0]
    assert row["event"] == "change_volum"
    assert row["name"] == "音量改变事件"
    assert "change_volum" in {o["event"] for o in row["event_options"]}


def test_switching_event_updates_chinese_name(page):
    """手动切换事件时英文标识和中文名必须对应更新(用户 2026-11-13)."""
    page._fault_expect = {
        "error_fan_stuck": {"name": "风机卡住"},
        "error_kit_fan_err": {"name": "风机开路或短路"},
    }
    page._prd_rows = [{
        "qid": "Q066", "event": "error_fan_stuck", "name": "风机卡住",
        "kind": "故障", "voice": "066", "voice_text": "风机异常",
        "fault_code": "", "note": "",
        "event_options": [{"event": "error_fan_stuck",
                           "name": "风机卡住"},
                          {"event": "error_kit_fan_err",
                           "name": "风机开路或短路"}]}]
    page._fill_prd_table()
    combo = page._event_combo_at(0)
    combo.setCurrentIndex(combo.findData("error_kit_fan_err"))
    assert page._prd_rows[0]["event"] == "error_kit_fan_err"
    assert page._prd_rows[0]["name"] == "风机开路或短路"
    assert page.fault_prd_table.item(0, 1).text() == "风机开路或短路"


def test_general_event_has_no_fault_code(page):
    """通用/提醒事件 PRD 没有故障码, 不能显示 AI/旧记忆臆造的码."""
    page._prd_rows = [{
        "qid": "Q010", "event": "clean_finish", "name": "清扫完成事件",
        "kind": "通用", "voice": "010", "fault_code": "28",
        "fault_push": "DA_RC_E_28"}]
    page._fault_expect = {"clean_finish": {"name": "清扫完成事件"}}
    page._apply_prd_names()
    assert page._prd_rows[0]["kind"] == "通用"
    assert page._prd_rows[0]["fault_code"] == ""
    assert page._prd_rows[0]["fault_push"] == ""


def test_empty_fault_code_is_filled_from_actual(page):
    """故障码为空时像灯显一样用实测值回填; 已有值不被覆盖."""
    page._fault_expect = {"clean_finish": {"name": "清扫完成事件"},
                          "error_fan_stuck": {"name": "风机卡住"}}
    page._prd_rows = [
        {"qid": "Q010", "event": "clean_finish", "name": "清扫完成事件",
         "kind": "通用", "voice": "010", "fault_code": ""},
        {"qid": "Q066", "event": "error_fan_stuck", "name": "风机卡住",
         "kind": "故障", "voice": "066", "fault_code": "156"},
    ]
    page._fill_prd_table()
    page._fill_prd_code_from_actual("clean_finish", "45")
    page._fill_prd_code_from_actual("error_fan_stuck", "999")
    assert page._prd_rows[0]["fault_code"] == "45"
    assert page._prd_rows[0]["code_source"] == "actual"
    assert page.fault_prd_table.item(0, 6).text() == "45"
    # 已有配置值的保留, 用于判定
    assert page._prd_rows[1]["fault_code"] == "156"
    # 通用事件即使重新应用中文名, 实测回填的码不能被清掉
    page._apply_prd_names()
    assert page._prd_rows[0]["fault_code"] == "45"


def test_ap_empty_sound_shows_unconfigured_not_missing(page):
    """AP 配置没配语音时应显示未配置, 不能把 PRD 语音硬判成未捕获."""
    from core.apos import hs4_config
    page._fault_expect = {"app_sweeper_keyboard_press_frequently": {
        "name": "频繁集尘"}}
    page._prd_rows = [{
        "qid": "Q166", "event": "app_sweeper_keyboard_press_frequently",
        "name": "频繁集尘", "voice": "166", "voice_text": "集尘过于频繁",
        "kind": "提醒"}]
    exp = page._prd_expected("app_sweeper_keyboard_press_frequently")
    assert exp["sound"] == ""
    ver = hs4_config.verify({}, {"app_sweeper_keyboard_press_frequently": exp},
                            "app_sweeper_keyboard_press_frequently")
    assert ver["voice"]["status"] == "AP平台未配置"


def test_reuse_voice_accepts_prd_own_reused_and_ap_voice(page):
    """接受 PRD 自身、PRD 复用、以及同一 AP 事件在当前平台的语音."""
    from core.apos import faults, hs4_config
    event = "error_kit_lidar_obscured"
    page._fault_expect = {event: {
        "name": "雷达被遮挡事件", "sound": {"identifier": "999"}}}
    page._prd_rows = [{
        "qid": "Q131", "event": event, "name": "雷达被遮挡事件",
        "voice": "131", "reuse_voice": "028",
        "voice_text": "雷达有效数据少或雷达无数据", "kind": "故障"}]
    exp = page._prd_expected(event)
    assert exp["sound"] == "131"                 # PRD 自身 Q 号
    assert exp["acceptable_sounds"] == ["028", "999"]
    for beep in ("131", "028", "999"):
        ver = hs4_config.verify({"beep": beep}, {event: exp}, event)
        assert ver["voice"]["status"] == "通过", (beep, ver["voice"])
        cmp = faults.compare({"beep": beep}, {event: exp}, event)
        assert cmp["ok"] is True, (beep, cmp)
    # 其它事件的语音仍然不通过
    assert hs4_config.verify(
        {"beep": "111"}, {event: exp}, event)["voice"]["status"] == "不一致"
    assert faults.compare({"beep": "111"}, {event: exp},
                          event)["ok"] is False


def test_old_row_missing_voice_text_backfills_cancel_marker(page):
    """旧解析结果 voice_text 丢了: 按 PRD 清单补回, 取消播报不能误判缺文件."""
    from core.apos import voice_package

    page._prd_catalog = {"active": {"191": {
        "qid": "Q191", "kind": "提醒", "condition": "集尘中断",
        "text": "集尘好像出了点问题", "chinese": "暂时不提供语音",
        "final": 191}}, "excluded": {}, "map": {"191": 191}}
    page._prd_rows = [{
        "qid": "Q191", "event": "not_finish_dust_collect",
        "name": "集尘失败事件", "voice": "191",
        "voice_text": None, "note": "PRD：集尘中断\n（Q191）"}]
    page._fault_expect = {"not_finish_dust_collect": {
        "name": "集尘失败事件", "sound": {}, "led": {}}}
    page._backfill_prd_voice_text()
    row = page._prd_rows[0]
    assert row["voice_text"] == "暂时不提供语音"
    assert voice_package.required_files([row]) == {}
    exp = page._prd_expected("not_finish_dust_collect", row)
    assert exp["voice_cancelled"] is True
    assert exp["sound"] == ""


def test_q100_sleep_wake_uses_exit_dormant(page):
    """Q100 机器休眠唤醒应映射退出休眠事件, 而不是风机档位(用户 2026-11-13)."""
    page._fault_expect = {
        "change_fan_level": {"name": "风机档位改变事件",
                             "sound": {"identifier": "085"}},
        "exit_dormant": {"name": "退出休眠事件",
                         "sound": {"identifier": "100"}},
    }
    page._prd_catalog = {
        "active": {100: {"qid": "Q100", "kind": "通用",
                         "condition": "机器休眠唤醒", "text": "复用[Q085]",
                         "chinese": "复用[Q085]", "final": 85}},
        "excluded": {}, "map": {100: 85},
    }
    page._on_prd_parsed([{"event": "change_fan_level", "voice": "085",
                          "kind": "通用", "source": "memory"}])
    row = next(r for r in page._prd_rows if r.get("qid") == "Q100")
    assert row["event"] == "exit_dormant"
    assert row["name"] == "退出休眠事件"
    assert row["voice"] == "100" and row["reuse_voice"] == "085"
    assert "exit_dormant" in {o["event"] for o in row["event_options"]}
    # AP 事件播报 Q100 与 PRD 自身 Q100 一致, 不需要冲突提示
    assert "【工具校验】" not in row["note"]


def test_wifi_reset_candidates_are_exact(page):
    """重置 WiFi 只能选 wifi_reset, 不能把联网成功/失败/密码错误当候选."""
    page._fault_expect = {
        "wifi_reset": {"name": "wifi已重置事件",
                       "sound": {"identifier": "005"}},
        "config_wifi_begin": {"name": "开始配置网络事件",
                              "sound": {"identifier": "079"}},
        "config_wifi_success": {"name": "连接网络成功事件",
                                "sound": {"identifier": "004"}},
        "incorrect_wifi_passwd": {"name": "配网验证错误事件",
                                  "sound": {"identifier": "103"}},
        "distribution_network_timeout": {"name": "机器联网失败",
                                         "sound": {"identifier": "104"}},
    }
    page._prd_catalog = {
        "active": {5: {"qid": "Q005", "condition": "重置WiFi",
                       "chinese": "机器等待联网", "text": "等待配置网络",
                       "final": 5}},
        "excluded": {}, "map": {5: 5},
    }
    page._prd_rows = [{"qid": "Q005", "event": "wifi_reset",
                       "name": "wifi已重置事件", "voice": "005"}]
    page._enrich_prd_event_options()
    opts = page._prd_rows[0]["event_options"]
    assert [o["event"] for o in opts] == ["wifi_reset"]
    # 候选缺中文名时用 AP 配置名补全, 悬停提示带中文
    assert opts[0]["name"] == "wifi已重置事件"
    assert page._event_tip("wifi_reset", opts[0]["name"]) == \
        "wifi已重置事件（wifi_reset）"
    assert page._event_tip("wifi_reset", "") == "wifi_reset"


def test_prd_event_combo_tooltip_has_chinese_name(page):
    """解析事件下拉: 候选项/已选中事件都要能显示中文名."""
    from PySide6.QtWidgets import QComboBox

    page._fault_expect = {
        "dry_mop_fan_error": {"name": "烘干风机故障事件",
                              "sound": {"identifier": "084"}},
        "dry_mop_fan_short": {"name": "基站烘干风机短路",
                              "sound": {"identifier": "303"}},
    }
    page._prd_rows = [{
        "qid": "Q303", "event": "dry_mop_fan_error",
        "name": "烘干风机故障事件", "voice": "303", "source": "user",
        "event_options": [{"event": "dry_mop_fan_error"},
                          {"event": "dry_mop_fan_short"}],
    }]
    page._prd_catalog = {
        "active": {303: {"qid": "Q303", "condition": "基站烘干风扇过流异常",
                         "text": "", "chinese": "", "final": 303}},
        "excluded": {}, "map": {303: 303},
    }
    page._enrich_prd_event_options()
    page._fill_prd_table()
    host = page.fault_prd_table.cellWidget(0, 0)
    combo = host.findChild(QComboBox)
    assert combo is not None
    tips = [str(combo.itemData(k, Qt.ToolTipRole)) or ""
            for k in range(combo.count())]
    assert all("（" in t for t in tips), tips
    assert "烘干风机故障事件" in combo.toolTip()


def test_q131_radar_semantic_override(page):
    """Q131 雷达无数据不能被映射成线激光遮挡; 语义优先并标注语音冲突."""
    page._fault_expect = {
        "line_laser_avoid_obstacle_dirty": {
            "name": "线激光避障传感器被遮挡", "sound": {"identifier": "028"}},
        "error_kit_lidar_err": {
            "name": "雷达异常事件", "sound": {"identifier": "139"}},
        "error_kit_lidar_obscured": {
            "name": "雷达被遮挡事件", "sound": {"identifier": "131"}},
    }
    page._prd_catalog = {
        "active": {131: {"qid": "Q131", "kind": "故障",
                         "condition": "雷达有效数据少或雷达无数据",
                         "text": "复用[Q028]", "chinese": "复用[Q028]",
                         "final": 28}},
        "excluded": {}, "map": {131: 28},
    }
    page._on_prd_parsed([{
        "event": "line_laser_avoid_obstacle_dirty", "voice": "028",
        "kind": "故障", "source": "memory"}])
    row = next(r for r in page._prd_rows if r.get("qid") == "Q131")
    assert row["event"] == "error_kit_lidar_err"
    assert row["name"] == "雷达异常事件"
    opts = {o["event"] for o in row["event_options"]}
    assert {"error_kit_lidar_err", "error_kit_lidar_obscured",
            "line_laser_avoid_obstacle_dirty"} <= opts
    assert "【工具校验】" not in row["note"]
    assert row["note"].startswith("PRD：雷达有效数据少或雷达无数据")
    assert "复用语音Q028" in row["note"]


def test_missing_prd_led_uses_prd_scene_not_ap_config(page):
    """灯显期望只能来自 PRD 清单, 不能拿 AP 事件配置的灯显当期望."""
    from core.apos import hs4_config

    # AP 配置灯显故意写成白色, PRD 清单配网失败是红色: 期望必须是红色
    page._fault_expect = {"incorrect_wifi_passwd": {
        "name": "配网验证错误事件", "sound": {"identifier": "103"},
        "led": {"rgb_led__start": "white1_on", "rgb_led__home": "all_off"},
        "led_names": {
            "rgb_led__start": {"device": "RGB电源灯设备",
                               "effect": "白灯1长亮",
                               "effect_id": "white1_on"},
            "rgb_led__home": {"device": "RGB回充灯设备",
                              "effect": "关闭", "effect_id": "all_off"},
            "rgb_led__x": {"device": "x", "effect": "红灯常亮",
                           "effect_id": "red_on"}}}}
    page._hs4 = {"led_scenes": [{
        "scenario": "配网失败", "power": "红色常亮",
        "home": "红色常亮"}]}
    page._prd_rows = [{"qid": "Q103", "event": "incorrect_wifi_passwd",
                       "name": "配网验证错误事件", "voice": "103",
                       "voice_text": "密码错误"}]
    exp = page._prd_expected("incorrect_wifi_passwd", page._prd_rows[0])
    assert exp["led"] == {"rgb_led__start": "red_on",
                          "rgb_led__home": "red_on"}
    assert exp["led_names"]["rgb_led__start"]["effect"] == "红色常亮"
    # 实测没有灯显 → 灯显未捕获（不是 AP平台未配置）
    ver = hs4_config.verify({}, {"incorrect_wifi_passwd": exp},
                            "incorrect_wifi_passwd")
    assert ver["led"]["status"] == "未捕获"
    assert "红色常亮" in ver["led"]["expected"]
    # PRD 行里已经配置的灯显优先
    page._prd_rows[0]["led_start"] = "white1_slow"
    exp2 = page._prd_expected("incorrect_wifi_passwd", page._prd_rows[0])
    assert exp2["led"]["rgb_led__start"] == "white1_slow"


def test_prd_clean_start_events_get_all_same_voice_options(page):
    """同一条开始清扫语音要列出 AP 配置里所有同 sound 事件(用户 2026-11-13)."""

    def ev(name, sound):
        return {"name": name, "sound": {"identifier": sound}}

    page._fault_expect = {
        "explore_map_finish_clean": ev("探索建图完成后清扫事件", "006"),
        "total_clean_start": ev("开始全局扫事件", "006"),
        "mop_clean_start": ev("开始拖地事件", "006"),
        "point_clean_start": ev("开始定点扫事件", "009"),
        "time_total_clean_start": ev("累计清扫开始", "065"),
        "area_clean_start": ev("开始划区扫事件", "065"),
        "auto_area_clean_start": ev("开始选区扫事件", "065"),
    }
    page._prd_catalog = {
        "active": {
            6: {"qid": "Q006", "kind": "通用",
                "condition": "机器开始全局清扫", "chinese": "开始清洁",
                "text": "开始清洁", "final": 6},
            9: {"qid": "Q009", "kind": "通用",
                "condition": "开始定点清洁", "chinese": "开始定点清洁",
                "text": "开始定点清扫", "final": 9},
            65: {"qid": "Q065", "kind": "通用",
                 "condition": "开始房间清洁", "chinese": "开始区域清洁",
                 "text": "开始房间清洁", "final": 65},
        },
        "excluded": {}, "map": {6: 6, 9: 9, 65: 65},
    }
    page._on_prd_parsed([
        {"event": "explore_map_finish_clean", "voice": "006",
         "kind": "通用", "source": "memory"},
        {"event": "point_clean_start", "voice": "009",
         "kind": "通用", "source": "memory"},
        {"event": "time_total_clean_start", "voice": "065",
         "kind": "通用", "source": "memory"},
    ])
    rows = {r["voice"]: r for r in page._prd_rows}
    q6 = rows["006"]
    assert q6["event"] == "total_clean_start"
    assert {o["event"] for o in q6["event_options"]} == {
        "explore_map_finish_clean", "total_clean_start", "mop_clean_start"}
    q65 = rows["065"]
    assert q65["event"] == "area_clean_start"
    assert {o["event"] for o in q65["event_options"]} == {
        "time_total_clean_start", "area_clean_start", "auto_area_clean_start"}
    assert rows["009"]["event"] == "point_clean_start"


def test_read_robot_info_retries_empty_firmware(monkeypatch):
    """刚连上 FirmwareInfo 返回空时要补读, 不能因为 SN 读到了就放弃(2026-11-13)."""
    import gui.device_page as dp
    monkeypatch.setattr(dp.time, "sleep", lambda _s: None)

    class Svc:
        calls = 0

        def read_once(self, _paths):
            self.calls += 1
            if self.calls == 1:
                return {"/System/CustomSN": "SN001",
                        "/System/FirmwareInfo": "{}"}
            return {"/System/CustomSN": "SN001",
                    "/System/FirmwareInfo":
                        '{"Model":"CLA_HS4","Version":"1.2.3"}'}

    svc = Svc()
    info = dp.read_robot_info(svc, version_retries=3,
                              include_base_station=False)
    assert svc.calls >= 2
    assert info["sn"] == "SN001"
    assert info["model"] == "CLA_HS4"
    assert info["firmware_version"] == "1.2.3"


def test_connect_thread_backfills_sn_after_stream_start(qapp, monkeypatch):
    """开流后再补读 SN：ZY2L 的 CustomSN 只有开流后从文件读得到（2026-11-26）。"""
    import gui.device_page as dp
    monkeypatch.setattr(dp.ConnectThread, "_sleep", lambda self, s: None)
    got = []

    class Svc:
        client = None

        def __init__(self):
            self.stream = False

        def connect(self, _ip):
            return True

        def start_stream(self):
            self.stream = True

        def read_once(self, _paths):
            return {
                "/System/CustomSN": "SN001" if self.stream else "",
                "/System/FirmwareInfo":
                    '{"Model":"CLA_ZY2L","Version":"1.0"}',
            }

    t = dp.ConnectThread(Svc(), "1.2.3.4")
    t.done.connect(lambda ok, ip, info: got.append(("done", dict(info))))
    t.info_ready.connect(lambda ip, extra: got.append(("info", dict(extra))))
    t.run()
    assert any(kind == "info" and data.get("sn") == "SN001"
               for kind, data in got), got


def test_prd_kind_filter_hides_rows(page):
    """类型列头下拉只隐藏不匹配的行, 不改变解析结果(用户 2026-11-13)。"""
    page._prd_rows = [
        {"event": "error_fan_stuck", "name": "风机卡住", "kind": "故障",
         "voice": "066", "voice_text": "风机异常", "fault_code": "",
         "note": ""},
        {"event": "app_sweeper_test", "name": "测试事件", "kind": "通用",
         "voice": "191", "voice_text": "测试", "fault_code": "",
         "note": ""},
    ]
    page._fill_prd_table()
    table = page.fault_prd_table
    assert "类型" in table.horizontalHeaderItem(2).text()
    page._set_prd_kind_filter("故障")
    assert not table.isRowHidden(0)
    assert table.isRowHidden(1)
    assert table.horizontalHeader()._kind_filter == "故障"
    page._set_prd_kind_filter("")
    assert not table.isRowHidden(0)
    assert not table.isRowHidden(1)
    assert len(page._prd_rows) == 2      # 数据没有被筛掉


def test_prd_header_click_clears_column_selection(page):
    """点表头不能留下整列/全选的蓝色选中状态(用户 2026-11-13)."""
    page._prd_rows = [{
        "event": "error_fan_stuck", "name": "风机卡住", "kind": "故障",
        "voice": "066", "voice_text": "风机异常", "fault_code": "",
        "note": ""}]
    page._fill_prd_table()
    table = page.fault_prd_table
    from PySide6.QtGui import QPalette
    assert table.palette().color(QPalette.Highlight).alpha() == 0
    table.selectAll()
    assert table.selectionModel().hasSelection()
    page._prd_header_clicked(0)
    assert not table.selectionModel().hasSelection()


def test_batch_pass_learns_verified_mapping(page):
    """语音+灯显都配置并通过时, 写入 verified 学习项(用户 2026-11-13)."""
    page._fault_expect = {"error_fan_stuck": {
        "name": "风机卡住", "sound": {"identifier": "066"},
        "led": {"rgb_led__home": "red_slow_flick"}}}
    page._device_info = {"model": "CLA_HS4", "firmware_version": "2.4.7"}
    page._prd_rows = [{
        "qid": "Q066", "event": "error_fan_stuck", "name": "风机卡住",
        "voice": "066", "voice_key": "风机异常", "voice_text": "风机异常",
        "kind": "故障", "fault_code": "156", "fault_push": ""}]
    ver = {"overall": "通过",
           "voice": {"status": "通过", "expected": "066", "actual": "066"},
           "led": {"status": "通过", "expected": "回充灯红灯慢闪",
                   "actual": "回充灯红灯慢闪"},
           "code": {"status": "通过", "expected": "156", "actual": "156"}}
    row = {"voice_actual": "066", "voice_expected": "066",
           "led_expected": "回充灯红灯慢闪", "code_actual": "156"}
    page._learn_verified("error_fan_stuck", ver, row)
    page._flush_verified_learning()
    hit = page._prd_memory.exact("风机异常")
    assert hit and hit["source"] == "verified"
    assert hit["event"] == "error_fan_stuck"
    assert hit["event_name"] == "风机卡住"
    assert hit["kind"] == "故障"
    assert hit["voice_text"] == "风机异常"
    # 学习库只存 PRD 语义 → AP 事件: Q号/语音号/故障码/灯显/机型都不存
    for field in ("qid", "voice", "fault_code", "fault_push", "ap_sound",
                  "led_expected", "model", "firmware", "ap_fingerprint"):
        assert field not in hit

    # 没配置灯显的事件不进共享学习
    page._verified_pending.clear()
    page._fault_expect["error_fan_stuck"]["led"] = {}
    page._learn_verified("error_fan_stuck", ver, row)
    assert not page._verified_pending


def test_manual_event_cell_editor_wraps(page):
    """双击事件单元格手动输入时用多行编辑框, 内容在框内换行."""
    from PySide6.QtWidgets import QStyleOptionViewItem, QTextEdit
    page._prd_rows = [{
        "qid": "Q066", "event": "error_fan_stuck", "name": "风机卡住",
        "kind": "故障", "voice": "066", "voice_text": "风机异常",
        "fault_code": "", "note": ""}]
    page._fill_prd_table()
    table = page.fault_prd_table
    idx = table.model().index(0, 0)
    opt = QStyleOptionViewItem()
    opt.rect = table.visualRect(idx)
    delegate = table.itemDelegateForColumn(0)
    editor = delegate.createEditor(table, opt, idx)
    assert isinstance(editor, QTextEdit)
    editor.setPlainText("error_self_test_monitor_wall_follow_duration_min")
    delegate.setModelData(editor, table.model(), idx)
    assert table.item(0, 0).text().startswith("error_self_test")


def test_manual_event_combo_wraps_after_enter(page):
    """事件输入框手动输入后回车, 多行显示层要出现."""
    from PySide6.QtTest import QTest
    page.fault_page.show()
    combo = page.fault_event_combo
    long = "error_self_test_monitor_wall_follow_duration_min"
    combo.setEditText(long)
    combo._finish_edit()
    QTest.qWait(50)
    assert combo._wrap.isVisible()
    assert long in combo._wrap.text().replace("​", "")


def test_prd_event_column_is_centered(page):
    """解析事件名称列居中显示(用户 2026-11-13)."""
    from PySide6.QtCore import Qt as _Qt
    page._prd_rows = [
        {"qid": "Q066", "event": "error_fan_stuck", "name": "风机卡住",
         "kind": "故障", "voice": "066", "voice_text": "风机异常",
         "fault_code": "", "note": "", "event_options": []},
        {"qid": "Q020", "event": "dust_box_water_box_out", "name": "尘盒取出",
         "kind": "故障", "voice": "020", "voice_text": "尘盒",
         "fault_code": "", "note": "",
         "event_options": [{"event": "dust_box_water_box_out"},
                           {"event": "dust_box_water_box_in"}]},
    ]
    page._fill_prd_table()
    table = page.fault_prd_table
    assert table.item(0, 0).textAlignment() == _Qt.AlignCenter
    combo = page._event_combo_at(1)
    assert combo is not None
    assert combo.lineEdit().alignment() == _Qt.AlignCenter


def test_prd_search_filters_and_jumps_to_matching_event(page):
    """右侧搜索框按事件/中文名/Q号筛选并跳到匹配行(用户 2026-11-13)."""
    page._prd_rows = [
        {"qid": "Q066", "event": "error_fan_stuck", "name": "风机卡住",
         "kind": "故障", "voice": "066", "voice_text": "风机异常",
         "fault_code": "", "note": "PRD：风机异常（Q066）"},
        {"qid": "Q030", "event": "error_psd_dirty", "name": "沿墙传感器异常",
         "kind": "故障", "voice": "030", "voice_text": "沿墙传感器异常",
         "fault_code": "", "note": "PRD：沿墙传感器开路（Q030）"},
    ]
    page._fill_prd_table()
    table = page.fault_prd_table
    page.fault_search.setText("风机")
    assert not table.isRowHidden(0)
    assert table.isRowHidden(1)
    page.fault_search.setText("Q030")
    assert table.isRowHidden(0)
    assert not table.isRowHidden(1)
    page.fault_search.clear()
    assert not table.isRowHidden(0)
    assert not table.isRowHidden(1)


def test_type_header_click_opens_filter_menu(page):
    """点「类型」列头弹出的筛选菜单包含四类, 选择后立即筛选(用户 2026-11-13)."""
    page._prd_rows = [
        {"event": "error_fan_stuck", "name": "风机卡住", "kind": "故障",
         "voice": "066", "voice_text": "风机异常", "fault_code": "",
         "note": ""},
        {"event": "app_sweeper_test", "name": "测试事件", "kind": "通用",
         "voice": "191", "voice_text": "测试", "fault_code": "",
         "note": ""},
    ]
    page._fill_prd_table()
    assert page.fault_prd_table.horizontalHeader().sectionsClickable()
    menu = page._prd_kind_menu()
    assert [a.text() for a in menu.actions()] == [
        "全部", "故障", "提醒", "通用", "基站"]
    next(a for a in menu.actions() if a.text() == "通用").trigger()
    assert page._prd_kind_filter == "通用"
    assert page.fault_prd_table.isRowHidden(0)
    assert not page.fault_prd_table.isRowHidden(1)


def test_prd_table_columns_fit_viewport(page, qapp):
    """显示状态下 8 列总宽不超过视口, 故障码/备注无需横向拖动。"""
    page._prd_rows = [{
        "event": "error_self_test_monitor_wall_follow_duration_min",
        "name": "沿墙传感器异常", "kind": "故障", "voice": "030",
        "voice_text": "沿墙传感器异常", "fault_code": "232",
        "note": "PRD 语音 Q030"}]
    page.fault_page.resize(1040, 700)
    page.fault_page.show()
    page._fill_prd_table()
    qapp.processEvents()
    table = page.fault_prd_table
    widths = [table.columnWidth(i) for i in range(7)]
    assert sum(widths) + 120 <= table.viewport().width(), (
        widths, table.viewport().width())
    assert table.horizontalScrollBar().maximum() == 0


def test_combo_width_adapts_to_long_option(page):
    """下拉宽度按最长选项名自适应, 长事件名不被截断(用户 2026-11-12)。"""
    combo = page.fault_event_combo
    combo.clear()
    long_ev = "error_self_test_monitor_wall_follow_duration_min"
    combo.addItem(long_ev)
    combo.addItem("充电")
    page._fit_combo_width(combo, 180, 480)
    need = combo.fontMetrics().horizontalAdvance(long_ev) + 36
    assert combo.maximumWidth() >= min(need, 480)
    assert combo.maximumWidth() > 320


def test_robot_ip_combo_defaults_to_device_page_ip(qapp):
    """未连接时自动化测试页也跟随设备页当前/上次 IP(用户 2026-11-27)。"""
    import types
    from PySide6.QtWidgets import QComboBox
    import gui.app_pages as ap

    class Dev:
        connected = False
        connecting = False
        connected_ip = ""

        class settings:
            @staticmethod
            def value(key, default=None):
                return ""

        @staticmethod
        def _known_devices():
            return []

        @staticmethod
        def _selected_ip():
            return "10.7.7.7"

    win = types.SimpleNamespace(robot_ip_combo=QComboBox(),
                                _robot_auto_connecting=True)
    win.robot_ip_combo.setEditable(True)
    ap.refresh_robot_ips(win, Dev(), auto=False)
    assert win.robot_ip_combo.currentText() == "10.7.7.7"



def test_poll_switches_to_disconnected_when_service_lost(page, monkeypatch):
    """设备侧断链(长时间无数据): 轮询要立刻把界面切到未连接(用户 2026-11-27)。"""
    class Svc:
        @staticmethod
        def is_connected():
            return False

    page._connected = True
    page._service = Svc()
    called = []
    monkeypatch.setattr(page, "disconnect_device",
                        lambda: called.append(True))
    page._poll()
    assert called, "链路断开会话没有触发界面断开"


def test_poll_keeps_connected_while_service_alive(page):
    class Svc:
        @staticmethod
        def is_connected():
            return True

        @staticmethod
        def latest():
            return {"ok": {}, "failed": {}, "ts": 0}

    page._connected = True
    page._service = Svc()
    page._poll()          # 不得抛异常/不得改连接状态
    assert page._connected



def test_main_connect_button_text_follows_state(page):
    """设备参数页连接按钮文案随状态变化(用户 2026-11-26)。"""
    page._service = object()
    page._connected = False
    page._connecting = False
    page.device_combo.setEditText("10.0.0.9")
    page._sync_connect_btn()
    assert page.connect_btn.text() == "连接"
    page._connected = True
    page._connected_ip = "10.0.0.9"
    page._sync_connect_btn()
    assert page.connect_btn.text() == "已连接"
    # 输入了别的 IP → 这是一次切换，按钮回到「连接」
    page.device_combo.setEditText("10.0.0.10")
    page._sync_connect_btn()
    assert page.connect_btn.text() == "连接"
    page.device_combo.setEditText("10.0.0.9")
    page._connecting = True
    page._sync_connect_btn()
    assert page.connect_btn.text() == "连接中…"


def test_connect_same_ip_does_not_reconnect(page):
    """已连接同一 IP 时点「已连接」不再触发重连(用户 2026-11-26)。"""
    page._service = object()
    page._connected = True
    page._connected_ip = "10.0.0.9"
    page._connecting = False
    page.device_combo.setEditText("10.0.0.9")
    page.connect_device()
    assert page._connecting is False
    assert "无需重复连接" in page.status_label.text()




def test_nav_running_state_tracks_sources(qapp):
    """续航测试运行时顶部也要显示「运行中」，多个来源互不覆盖（2026-11-26）。"""
    from gui.app_pages import PageNav
    nav = PageNav()
    assert nav.run_status.text() == "● 空闲"
    nav.set_running(True, "endurance")
    assert nav.run_status.text() == "● 运行中"
    nav.set_running(True, "test")
    nav.set_running(False, "endurance")
    assert nav.run_status.text() == "● 运行中"
    nav.set_running(False, "test")
    assert nav.run_status.text() == "● 空闲"


def test_stale_parsed_rows_are_not_shown(page):
    """旧版本解析结果不再直接展示, 提示重新 AI 解析(用户 2026-11-12)。"""
    import json as _json
    page.settings.setValue("apos/prd_parsed", _json.dumps([{
        "event": "old_event", "voice": "001", "name": "旧结果"}]))
    page.settings.setValue("apos/prd_parsed_ver", 0)
    page._load_prd_rows()
    assert page._prd_rows == []
    assert "版本较旧" in page.fault_status.text()


def test_failed_batch_items_only_mismatch_for_retry(page):
    """批量结束后只对不一致的事件单独重测(通过/未配置不重测)."""
    from core.apos import hs4_config
    page._fault_rows = [
        {"qid": "Q1", "event": "a", "overall": hs4_config.STATUS_MISMATCH},
        {"qid": "Q2", "event": "b", "overall": hs4_config.STATUS_OK},
        {"qid": "Q3", "event": "c", "overall": hs4_config.STATUS_UNCONFIGURED},
    ]
    batch = {"items": [("a", "A", {"qid": "Q1"}),
                       ("b", "B", {"qid": "Q2"}),
                       ("c", "C", {"qid": "Q3"})]}
    got = page._failed_batch_items(batch)
    assert [x[2]["qid"] for x in got] == ["Q1"]


def test_loop_clean_page_combos_and_thresholds(qapp):
    """循环清扫页：组合下拉(吸力0~5/水量0~4)、次数可为0、全选切换。"""
    from gui.loop_clean_page import LoopCleanPage
    p = LoopCleanPage()
    assert p.table.rowCount() == 2        # 默认只放仅扫1档 + 扫拖1档
    item0 = p.table.item(0, 0)   # 原生 item 勾选(同自动化用例列表)
    assert item0 is not None
    assert item0.flags() & Qt.ItemIsUserCheckable
    assert not p._row_checked(0)   # 默认不选中
    fan = p.table.cellWidget(0, 2)
    water = p.table.cellWidget(0, 3)
    assert fan.count() == 6 and fan.itemData(5) == 5
    assert water.count() == 5 and water.itemData(4) == 4
    assert p.rounds_spin.minimum() == 0
    assert p.low_spin.value() == 30       # 清扫电量默认 30
    assert p.target_spin.value() == 20    # 回充电量默认 20
    p.all_btn.click()          # 第一次全选
    assert p._row_checked(0)
    p.all_btn.click()          # 第二次反选
    assert not p._row_checked(0)



def test_endurance_ip_uses_device_page_ip_before_connect(qapp):
    """未连接时也用设备页当前 IP, 不再固定 10.21。"""
    from gui.endurance_page import EndurancePage

    class Dev:
        connected_ip = ""

        def _known_devices(self):
            return []

        def _selected_ip(self):
            return "192.168.10.88"

    ep = EndurancePage(device=Dev())
    assert ep._ip() == "192.168.10.88"


def test_endurance_page_restores_combos_and_thresholds(qapp, tmp_path):
    """续航组合勾选/挡位与阈值写入 QSettings，下次打开界面保持上次设置。"""
    from gui.endurance_page import EndurancePage
    ini = str(tmp_path / "end.ini")
    st = QSettings(ini, QSettings.IniFormat)
    p = EndurancePage(settings=st)
    p._set_row_checked(0, True)
    p.table.cellWidget(0, 2).setCurrentIndex(
        p.table.cellWidget(0, 2).findData(2))
    for r in (7, 6, 5):
        p.table.removeRow(r)
    p.low_spin.setValue(35)
    p.sample_spin.setValue(30)
    st.sync()

    st2 = QSettings(ini, QSettings.IniFormat)
    p2 = EndurancePage(settings=st2)
    assert p2.table.rowCount() == 5
    assert p2._row_checked(0)
    assert p2.table.cellWidget(0, 2).currentData() == 2
    assert p2._row_checked(1)   # 默认仍是勾选状态
    assert p2.low_spin.value() == 35
    assert p2.sample_spin.value() == 30


def test_loop_clean_page_restores_combos_and_thresholds(qapp, tmp_path):
    """循环清扫组合勾选/挡位与四个阈值写入 QSettings，下次打开保持。"""
    from gui.loop_clean_page import LoopCleanPage
    ini = str(tmp_path / "loop.ini")
    st = QSettings(ini, QSettings.IniFormat)
    p = LoopCleanPage(settings=st)
    p._set_row_checked(0, True)
    p.table.cellWidget(0, 2).setCurrentIndex(
        p.table.cellWidget(0, 2).findData(3))
    p._add_row("sweepMop", 5, 4, checked=True)
    p.low_spin.setValue(40)
    p.target_spin.setValue(25)
    p.rounds_spin.setValue(7)
    p.sample_spin.setValue(20)
    st.sync()

    st2 = QSettings(ini, QSettings.IniFormat)
    p2 = LoopCleanPage(settings=st2)
    assert p2.table.rowCount() == 3
    assert p2._row_values(0) == (True, "sweepOnly", 3, 0)
    assert p2._row_values(2) == (True, "sweepMop", 5, 4)
    assert p2.low_spin.value() == 40
    assert p2.target_spin.value() == 25
    assert p2.rounds_spin.value() == 7
    assert p2.sample_spin.value() == 20


def test_page_nav_hosts_global_robot_connection(qapp):
    """扫地机连接全局只有一份: 右上角导航条(用户 2026-11-27)。"""
    from gui.app_pages import PageNav
    nav = PageNav()
    assert nav.robot_ip_combo.isEditable()
    assert nav.robot_connect_btn.text() == "连接"
    assert not hasattr(nav, "robot_disconnect_btn"), "连接/断开应合并为一个按钮"
    nav.robot_ip_combo.addItem("CLA_HS4-SN1-192.168.10.9", "192.168.10.9")
    nav.robot_ip_combo.setCurrentIndex(0)
    nav.fit_robot_combo()
    assert nav.robot_ip_combo.width() >= nav._nav_combo_floor


def test_endurance_ip_prefers_connected_then_selected(qapp):
    """续航页不再有自己的 IP 框: 目标 IP 跟随设备页连接/选中(2026-11-27)。"""
    from gui.endurance_page import EndurancePage

    class Dev:
        connected_ip = "192.168.10.30"

        def _selected_ip(self):
            return "192.168.10.88"

    ep = EndurancePage(device=Dev())
    assert ep._ip() == "192.168.10.30"      # 已连接优先
    Dev.connected_ip = ""
    assert ep._ip() == "192.168.10.88"      # 未连接用设备页当前 IP


def test_loop_clean_ip_prefers_connected_then_selected(qapp):
    """循环清扫页同样只跟随设备页的 IP, 没有自己的 IP 框(2026-11-27)。"""
    from gui.loop_clean_page import LoopCleanPage

    class Dev:
        connected_ip = ""

        def _selected_ip(self):
            return "192.168.10.88"

    p = LoopCleanPage(device=Dev())
    assert p._ip() == "192.168.10.88"
    Dev.connected_ip = "192.168.10.30"
    assert p._ip() == "192.168.10.30"


def test_robot_ip_popup_second_click_does_not_flicker(qapp):
    """再次点击下拉框是"关闭", 不能在 200ms 内又重开(用户 2026-11-27)。"""
    from gui.app_pages import PageNav
    nav = PageNav()
    c = nav.robot_ip_combo
    c.addItem("CLA-SN-10.0.0.9", "10.0.0.9")
    c.showPopup()
    assert c._popup is not None and c._popup.isVisible()
    c.hidePopup()
    c.showPopup()                     # 立即再触发 = 第二次点击
    assert c._popup is None, "刚关闭就重开, 会闪动"
