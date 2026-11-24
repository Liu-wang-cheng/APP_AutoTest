# -*- coding: utf-8 -*-
"""HS4 项目清单（灯显/语音/故障码 xlsx）解析与对照。"""
import json

import pytest

pytest.importorskip("openpyxl")
import openpyxl  # noqa: E402

from core.apos import hs4_config  # noqa: E402


def _make_xlsx(path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "主机语音定义"
    ws.append(["语音编号", "触发条件", "语音内容参考示范", "中文",
               "英文", "场景类型", "机器人停机状态", "备注"])
    ws.append(["Q066", "主机风机异常", "主机风机异常，请检查",
               "主机风机异常，请检查", "Fan abnormal", "错误", "停机", ""])
    ws.append(["Q006", "开始清扫", "开始清洁", "开始清洁", "Start",
               "通用", "不停机", ""])
    ws2 = wb.create_sheet("主机灯显定义")
    ws2.append(["", "分类", "过程", "灯显"])
    ws2.append(["", "待机", "开机", "白色快闪"])
    ws2.append(["", "故障", "风机异常", "红色慢闪"])
    ws2.append([])
    ws2.append(["", "序号", "状态/事件", "清扫指示灯", "回充指示灯"])
    ws2.append(["", "1", "配网失败", "红色常亮", "红色常亮"])
    ws3 = wb.create_sheet("SS故障消息上报260904")
    ws3.append(["Push ID", "QDMS ID\n错误码", "Cleaning Report", "Note",
                "prd中文文案", "Push Message（EN）", "Normal/Error",
                "使用方式", "used"])
    ws3.append(["DA_RC_E_156", "ErrorCode_156", "ErrorCode_156",
                "", "刷子上卡有异物", "Brush stuck", "Error，停机", "", ""])
    ws3.append(["DA_RC_E_914", "DA_RC_E_914", "", "", "尘袋未安装",
                "", "Error", "", ""])
    wb.save(path)


@pytest.fixture
def project(tmp_path):
    p = tmp_path / "HS4_LED_Voice_Config.xlsx"
    _make_xlsx(p)
    return hs4_config.load_project(str(p))


def test_load_project_normalizes_ids(project):
    assert hs4_config.voice_entry(project, "066")["qid"] == "Q066"
    assert "风机异常" in hs4_config.voice_entry(project, "066")["condition"]
    assert hs4_config.fault_entry(project, "156")["push"] == "DA_RC_E_156"
    # DA_RC_E_914 没有 ErrorCode 列, 从 Push ID 取号
    assert hs4_config.fault_entry(project, "914")["scene"] == "尘袋未安装"
    # 灯显词表含中文效果
    assert hs4_config.led_known(project, "红色慢闪")
    assert hs4_config.led_known(project, "红灯慢闪")      # 红→红色 归一化
    assert not hs4_config.led_known(project, "彩虹灯")


def test_led_scene_table_feeds_expected_led(project):
    """灯显期望取 PRD 灯显清单的场景表, 不取 AP 事件配置(用户 2026-11-17)."""
    scenes = project["led_scenes"]
    assert any(s["scenario"] == "配网失败" for s in scenes)
    scene = hs4_config.led_scene_for(project, "密码错误")
    assert scene["power"] == "红色常亮"
    assert scene["home"] == "红色常亮"
    # 中文灯效 → 灯效 identifier 只做词表翻译
    assert hs4_config.led_effect_id(
        "红色常亮", {"red_on": "红灯常亮"}) == "red_on"
    assert hs4_config.led_effect_id(
        "白色常亮", {"white1_on": "白灯1长亮"}) == "white1_on"
    assert hs4_config.led_effect_id("红色常亮", {}) == ""


def _expected():
    return {"error_fan_stuck": {
        "name": "风机卡住", "sound": "066",
        "led": {"rgb_led__home": "red_slow_flick"},
        "led_names": {"rgb_led__home": {
            "device": "RGB回充灯设备", "effect": "红色慢闪",
            "effect_id": "red_slow_flick"}}}}


def test_verify_matches_config_and_project(project):
    actual = {"beep": "066", "led": {"rgb_led__home": "red_slow_flick"},
              "code": "156"}
    res = hs4_config.verify(actual, _expected(), "error_fan_stuck", project)
    assert res["voice"]["status"] == "通过"
    assert "Q066" in res["voice"]["note"]
    assert res["led"]["status"] == "通过"
    assert res["led"]["actual"] == "回充灯=红色慢闪"
    assert res["led"]["note"] == "清单词条 ✓"
    assert res["code"]["status"] == "通过"
    assert "DA_RC_E_156" in res["code"]["note"]
    assert res["overall"] == "通过"


def test_verify_mismatch_and_missing(project):
    res = hs4_config.verify({"beep": "999",
                             "led": {"rgb_led__home": "all_off"}},
                            _expected(), "error_fan_stuck", project)
    assert res["voice"]["status"] == "不一致" and res["fail"]
    assert res["led"]["status"] == "不一致"
    assert res["overall"] == "不一致"
    miss = hs4_config.verify({}, _expected(), "error_fan_stuck", project)
    assert miss["voice"]["status"] == "未捕获"
    assert miss["led"]["status"] == "未捕获"
    assert miss["code"]["status"] == "无"


def test_verify_uncovered_is_not_failure(project):
    actual = {"beep": "066", "led": {"rgb_led__home": "red_slow_flick"},
              "code": "926"}
    res = hs4_config.verify(actual, _expected(), "error_fan_stuck", project)
    # 故障码只检查是否上报, 不再判断与 PRD 期望是否一致(用户 2026-11-16)
    assert res["code"]["status"] == "通过"
    assert res["fail"] is False
    assert res["overall"] == "通过"
    # 配置里没有的事件 → 未配置(不算失败)
    res2 = hs4_config.verify(actual, _expected(), "error_drop", project)
    assert res2["overall"] == "AP平台未配置" and res2["fail"] is False


def test_make_report_roundtrip(tmp_path, project):
    rows = [{
        "time": "2026-11-12 10:00:00", "event": "error_fan_stuck",
        "qid": "Q066", "prd_content": "主机风机异常，请检查",
        "name": "风机卡住", "group": "硬件故障", "overall": "通过",
        "voice_expected": "066", "voice_actual": "066",
        "voice_status": "通过", "voice_note": "Q066 主机风机异常",
        "led_expected": "回充灯=红色慢闪", "led_actual": "回充灯=红灯慢闪",
        "led_status": "通过", "led_note": "清单词条 ✓",
        "code_expected": "156", "code_actual": "156",
        "code_status": "通过",
        "code_note": "DA_RC_E_156 刷子上卡有异物", "note": "",
    }, {
        "time": "2026-11-12 10:01:00", "event": "error_collision_stuck",
        "qid": "Q031", "prd_content": "前挡卡住，请检查",
        "name": "前撞异常", "group": "硬件故障", "overall": "不一致",
        "voice_expected": "031", "voice_actual": "111",
        "voice_status": "不一致", "voice_note": "",
        "led_expected": "回充灯=红灯慢闪", "led_actual": "未捕获",
        "led_status": "未捕获", "led_note": "",
        "code_expected": "194", "code_actual": "926",
        "code_status": "通过", "code_note": "", "note": "",
    }]
    out = tmp_path / "report.xlsx"
    hs4_config.make_report(str(out), rows, project["path"], "192.168.10.63",
                           started="2026-11-12 09:59",
                           device_info={"model": "CLA_HS4",
                                        "firmware_version": "1.2.3"})
    wb = openpyxl.load_workbook(out, data_only=True)
    assert wb.sheetnames == ["汇总", "明细"]
    ws = wb["汇总"]
    assert ws["A1"].value == "故障模拟批量测试报告"
    assert ws["A1"].font.name == "Microsoft YaHei"
    assert ws["A6"].font.name == "Microsoft YaHei"
    assert ws["A1"].alignment.wrap_text is True
    assert ws["A6"].alignment.wrap_text is True
    assert ws["C11"].alignment.wrap_text is True
    assert [ws.cell(row=6, column=i).value for i in range(1, 8)] == [
        "总数", "通过", "不一致", "清单未覆盖", "AP平台未配置",
        "语音取消", "通过率"]
    assert [ws.cell(row=7, column=i).value for i in range(1, 7)] == [
        "2", "1", "1", "0", "0", "0"]
    # 问题事件只列失败项, 并写明对应 PRD 事件内容
    assert ws["B11"].value == "Q031"
    assert ws["C11"].value == "前挡卡住，请检查"
    assert ws["D11"].value == "error_collision_stuck"
    assert "语音不一致" in ws["G11"].value
    assert ws["G11"].alignment.horizontal == "left"
    assert "故障码不一致" not in ws["G11"].value
    assert "CLA_HS4" in ws["A3"].value
    assert "192.168.10.63" not in ws["A3"].value   # 报告不写设备 IP
    ws2 = wb["明细"]
    assert ws2["J3"].alignment.horizontal == "left"
    assert ws2["A1"].font.name == "Microsoft YaHei"
    # 带数字的单元格按文本格式(0001 不变 1, 用户 2026-11-17)
    assert ws2["C2"].number_format == "@"
    assert ws["A7"].number_format == "@"
    assert ws["A7"].value == "2"
    assert ws2["B2"].font.name == "Microsoft YaHei"
    assert ws2["B2"].value == "2026-11-12 10:00:00"
    assert ws2["C2"].value == "Q066"
    assert ws2["D2"].value == "主机风机异常，请检查"
    assert ws2["E2"].value == "error_fan_stuck\n风机卡住"
    assert ws2["I2"].value == "通过"
    assert ws2["J2"].value in (None, "")
    assert ws2["E3"].value == "error_collision_stuck\n前撞异常"
    assert ws2["I3"].value == "不一致"
    assert "灯显未捕获" in ws2["J3"].value
    # 语音/灯显不一致的单元格标黄
    assert str(ws2["F3"].fill.fgColor.rgb).endswith("FFF2CC")
    assert str(ws2["G3"].fill.fgColor.rgb).endswith("FFF2CC")


def test_report_shows_voice_index_when_prd_has_no_q(tmp_path):
    """PRD 只有语音序号时, 报告列名/内容都不带 Q(用户 2026-11-17)。"""
    rows = [{
        "time": "2026-11-17 16:22:19", "event": "base_station_temperature_sensor_error",
        "qid": "0830", "prd_content": "高温检测",
        "name": "基站温度传感器异常", "group": "故障", "overall": "AP平台未配置",
        "voice_expected": "0830", "voice_actual": "",
        "voice_status": "AP平台未配置", "led_status": "AP平台未配置",
        "code_status": "AP平台未配置", "note": "",
    }]
    out = tmp_path / "report.xlsx"
    hs4_config.make_report(str(out), rows, "", "",
                           voice_check={"checked": True, "missing": [{
                               "file": "0830.mp3", "qids": ["0830"],
                               "sources": [{"qid": "0830", "condition": "高温",
                                            "field": "PRD语音"}]}]})
    wb = openpyxl.load_workbook(out, data_only=True)
    ws = wb["汇总"]
    assert ws["B10"].value == "语音序号"
    assert ws["B11"].value == "0830"
    assert ws["C5"].value.startswith("缺失 1 个文件（0830）")
    assert wb["明细"]["C1"].value == "语音序号"
    assert wb["明细"]["C2"].value == "0830"
    assert wb["语音包检查"]["A1"].value == "语音序号"
    assert wb["语音包检查"]["A2"].value == "0830"


def test_fit_height_grows_with_wrapped_content():
    """行高按内容自适应, 两行事件名/长说明不被截断(用户 2026-11-17)."""
    short = hs4_config._fit_height(["短"], [10])
    multi = hs4_config._fit_height(["a\nb\nc"], [20])
    long_cn = hs4_config._fit_height(["很长的中文内容" * 6], [10])
    assert short <= 21
    assert multi >= 51
    assert long_cn > 21


def test_report_lists_rows_without_ap_event(tmp_path):
    """PRD 有、AP 无对应事件的行也要写进报告(用户 2026-11-17)."""
    rows = [{
        "time": "2026-11-17 17:00:00", "event": "system_start",
        "qid": "0001", "prd_content": "开机", "name": "系统启动完成事件",
        "group": "提醒", "overall": "通过",
        "voice_expected": "0001", "voice_actual": "0001",
        "voice_status": "通过", "led_status": "通过",
        "code_status": "无", "note": "",
    }]
    unmatched = [{"qid": "0826", "voice": "0826",
                  "prd_content": "基站清洗盘清洗提醒"}]
    out = tmp_path / "report.xlsx"
    hs4_config.make_report(str(out), rows, "", "", unmatched=unmatched)
    wb = openpyxl.load_workbook(out, data_only=True)
    ws = wb["汇总"]
    assert "无对应事件" in str(ws["A8"].value)
    assert ws["A9"].value == "问题事件（1 条）"
    assert ws["B11"].value == "0826"
    assert ws["F11"].value == "无对应事件"
    assert "AP平台" in str(ws["G11"].value)
    ws2 = wb["明细"]
    last = ws2.max_row
    assert ws2.cell(row=last, column=3).value == "0826"
    assert ws2.cell(row=last, column=9).value == "无对应事件"
    footer = next(str(ws.cell(row=r, column=1).value)
                  for r in range(1, ws.max_row + 1)
                  if str(ws.cell(row=r, column=1).value or "")
                  .startswith("全部事件:"))
    assert "无对应事件: 1" in footer


def test_report_notes_missing_voice_package(tmp_path, project):
    """批量报告写明语音包缺失; 缺失文件单独列在「语音包检查」页."""
    rows = [{
        "time": "2026-11-16 10:00:00", "event": "collect_dust_start",
        "qid": "Q166", "prd_content": "集尘过于频繁",
        "name": "频繁集尘", "group": "提醒", "overall": "AP平台未配置",
        "voice_expected": "166", "voice_actual": "",
        "voice_status": "AP平台未配置", "led_status": "AP平台未配置",
        "code_status": "无", "note": "",
    }]
    out = tmp_path / "voice.xlsx"
    hs4_config.make_report(
        str(out), rows, project["path"], "",
        started="2026-11-16 09:59",
        device_info={"model": "CLA_HS4"},
        voice_check={"checked": True, "error": "",
                     "missing": [{"file": "166.mp3", "qids": ["Q166"],
                                  "sources": [{"qid": "Q166",
                                               "condition": "集尘过于频繁",
                                               "field": "PRD语音"}]}]})
    wb = openpyxl.load_workbook(out, data_only=True)
    assert wb.sheetnames == ["汇总", "明细", "语音包检查"]
    assert wb["汇总"]["A5"].value == "语音包检查"
    assert "缺失 1 个" in wb["汇总"]["C5"].value
    assert "Q166" in wb["汇总"]["C5"].value
    ws3 = wb["语音包检查"]
    assert ws3["A1"].font.name == "Microsoft YaHei"
    assert ws3["A2"].font.name == "Microsoft YaHei"
    assert ws3["A2"].value == "Q166"
    assert ws3["B2"].value == "166.mp3"
    assert ws3["E2"].value == "缺失"


def test_report_voice_missing_notes_both_config_and_package(tmp_path):
    """0921/0922: 语音配错 + 语音包缺失, 两个结论都要写(用户 2026-11-17)."""
    rows = [{
        "time": "2026-11-17 19:20:00",
        "event": "empty_robot_water_tank_done", "qid": "0921",
        "prd_content": "残留水清除已完成", "name": "结束水箱排水",
        "overall": "不一致", "prd_voice": "0921",
        "voice_expected": "0921", "voice_actual": "0824",
        "voice_status": "不一致", "led_status": "通过",
        "code_status": "无", "note": "",
    }, {
        "time": "2026-11-17 19:20:10",
        "event": "drainage_water_without_base_station_tray", "qid": "0922",
        "prd_content": "充电座上没有拖布清洁板", "name": "基站托盘不在位开启一键排水",
        "overall": "通过", "prd_voice": "0922",
        "voice_expected": "0922", "voice_actual": "0922",
        "voice_status": "通过", "led_status": "通过",
        "code_status": "无", "note": "",
    }]
    out = tmp_path / "both.xlsx"
    hs4_config.make_report(
        str(out), rows, "", "", voice_check={
            "checked": True, "error": "",
            "missing": [{"file": "0921.mp3", "qids": ["0921"]},
                        {"file": "0922.mp3", "qids": ["0922"]}]})
    wb = openpyxl.load_workbook(out, data_only=True)
    ws2 = wb["明细"]
    texts = {}
    for r in range(2, ws2.max_row + 1):
        texts[str(ws2.cell(row=r, column=3).value)] = (
            str(ws2.cell(row=r, column=9).value),
            str(ws2.cell(row=r, column=10).value))
    # 0921: 语音不一致 + 语音包缺失(按 1./2. 分条)
    assert texts["0921"][0] == "不一致"
    assert texts["0921"][1].startswith("1. 语音包缺失")
    assert "\n2. 语音不一致" in texts["0921"][1]
    assert "语音包缺失（0921.mp3）" in texts["0921"][1]
    # 0922: 语音号对得上但文件不存在 → 语音包缺失 + 配置指向无文件
    assert texts["0922"][0] == "语音包缺失"
    assert "语音包缺失（0922.mp3）" in texts["0922"][1]
    assert "机器人无该文件" in texts["0922"][1]


def test_report_marks_voice_package_missing_as_problem(tmp_path):
    """语音包缺文件的事件要出现在问题事件里, 不能算通过(用户 2026-11-17)."""
    rows = [{
        "time": "2026-11-17 19:20:00", "event": "empty_robot_water_tank_start",
        "qid": "0920", "prd_content": "开始清除残留水",
        "name": "开始清空机器人水箱", "overall": "通过",
        "voice_expected": "920/801", "voice_actual": "0801",
        "voice_status": "通过", "led_status": "通过",
        "code_status": "无", "note": "",
    }]
    out = tmp_path / "vp.xlsx"
    hs4_config.make_report(
        str(out), rows, "", "", voice_check={
            "checked": True, "error": "",
            "missing": [{"file": "0920.mp3", "qids": ["0920"]}]})
    wb = openpyxl.load_workbook(out, data_only=True)
    ws = wb["汇总"]
    assert ws["B7"].value == "0"        # 不再算通过
    assert ws["A9"].value == "问题事件（1 条）"
    assert ws["B11"].value == "0920"
    assert ws["F11"].value == "语音包缺失"
    assert "0920.mp3" in str(ws["G11"].value)
    assert "语音包缺失: 1" in str(ws["A8"].value)
    assert "语音包缺失: 1" in str(ws.cell(
        row=13, column=1).value or "")


def test_voice_cancelled_is_not_uncovered(project):
    """PRD 标注暂不提供语音 → 语音取消, 不按清单未覆盖处理."""
    exp = {"evt": {"name": "集尘中断", "sound": "",
                   "voice_cancelled": True, "led": {},
                   "code": ""}}
    res = hs4_config.verify({}, exp, "evt", project)
    assert res["voice"]["status"] == "语音取消"
    assert res["overall"] == "语音取消"
    assert res["fail"] is False


def test_expected_code_from_prd(project):
    """故障码只检查是否有上报, 不与 PRD 期望值比较(用户 2026-11-16)."""
    exp = {"evt": {"name": "风机卡住", "sound": "066",
                   "led": {"rgb_led__home": "red_slow_flick"},
                   "led_names": {}, "code": "156"}}
    ok = hs4_config.verify({"beep": "066",
                            "led": {"rgb_led__home": "red_slow_flick"},
                            "code": "156"}, exp, "evt", project)
    assert ok["code"]["status"] == "通过" and ok["fail"] is False
    # 实际码与期望码不同也通过, 只要设备有上报
    other = hs4_config.verify({
        "beep": "066", "led": {"rgb_led__home": "red_slow_flick"},
        "code": "926"}, exp, "evt", project)
    assert other["code"]["status"] == "通过" and other["fail"] is False
    missing = hs4_config.verify({
        "beep": "066", "led": {"rgb_led__home": "red_slow_flick"}},
        exp, "evt", project)
    assert missing["code"]["status"] == "未捕获"
    assert missing["fail"] is True
    assert missing["overall"] == "不一致"
