# -*- coding: utf-8 -*-
"""设备参数页 / 顶部双页面 / 曲线（离屏）。"""
import json
import time

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QSettings  # noqa: E402
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
    assert page.device_combo.maximumWidth() <= 560


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
    assert cards[0].plot.getViewBox().autoRangeEnabled()[1] is True


def test_main_window_top_nav(qapp, monkeypatch, tmp_path):
    from gui import main_window as mw
    monkeypatch.setattr(mw, "CASES_DIR", str(tmp_path / "Test_cases"), raising=False)
    w = mw.MainWindow()
    try:
        assert w.pages.count() == 2
        # 默认打开自动化测试页
        assert w.pages.currentWidget() is w.test_page
        assert hasattr(w, "robot_ip_combo")
        w._show_page("device")
        assert w.pages.currentWidget() is w.device_page
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
        def isRunning(self):
            return True

        def cancel(self):
            calls["cancel"] = True

        def wait(self, timeout):
            calls["wait"] = timeout
            return True

    page._scan_thread = FakeThread()
    page.shutdown()
    assert calls.get("cancel") is True and calls.get("wait") == 2000


def test_catalog_param_names_are_chinese():
    from core.apos import catalog
    labels = [catalog.item_label(it) for g in catalog.PARAM_GROUPS
              for it in g["items"]]
    assert "档位" in labels and "电流" in labels
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
    assert "/MotorVibration/Speed" in seen["paths"]
    assert "/MotorFan/Speed" in seen["paths"]
    assert set(seen["paths"]) >= set(catalog.all_param_paths())


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
    assert "已连接" in nav.dev_status.text() and "10.0.0.9" in nav.dev_status.text()
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
