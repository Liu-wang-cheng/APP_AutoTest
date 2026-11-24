# -*- coding: utf-8 -*-
"""AI 解析 PRD: 文本提取、提示词、返回解析、中转站调用。"""
import json

import pytest

from core.apos import ai_prd

EVENTS = [{"event": "error_fan_stuck", "name": "风机卡住"},
          {"event": "error_drop", "name": "地检异常"}]


def test_extract_text_txt_and_xlsx(tmp_path):
    p = tmp_path / "prd.txt"
    p.write_text("风机异常：语音 066，红灯慢闪，故障码 156", encoding="utf-8")
    assert "066" in ai_prd.extract_text(str(p))

    import openpyxl
    x = tmp_path / "prd.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "配置"
    ws.append(["事件", "语音", "灯显", "故障码"])
    ws.append(["error_fan_stuck", 66, "红灯慢闪", 156])
    wb.save(x)
    text = ai_prd.extract_text(str(x))
    assert "error_fan_stuck" in text and "156" in text and "### Sheet" in text

    empty = tmp_path / "empty.txt"
    empty.write_text("   ", encoding="utf-8")
    with pytest.raises(ValueError):
        ai_prd.extract_text(str(empty))


def test_build_messages_only_fault_voice_and_code():
    msgs = ai_prd.build_messages("PRD", EVENTS)
    text = msgs[1]["content"]
    assert "error_fan_stuck | 风机卡住" in text
    assert "只输出 JSON" in msgs[0]["content"]
    # 事件选择必须按 PRD 中文关键字/语义, 不能只看语音号(用户 2026-11-16)
    assert "中文关键字" in text and "不能只因为语音号" in msgs[0]["content"]
    # 灯显改为人工在界面选择, 不再发给 AI
    assert "灯显不要输出" in text and "led_home" not in text


def test_parse_response_fenced_and_filtered():
    payload = """```json
    {"items":[
      {"event":"error_fan_stuck","voice":"066","voice_text":"风机异常",
       "led_home":"red_slow_flick","led_home_text":"红灯慢闪",
       "led_start":"","led_start_text":"","fault_code":"156",
       "fault_push":"DA_RC_E_156","note":""},
      {"event":"不存在的event","voice":"999"}
    ]}
    ```"""
    items = ai_prd.parse_response(payload, EVENTS)
    assert len(items) == 1
    it = items[0]
    assert it["event"] == "error_fan_stuck"
    assert it["name"] == "风机卡住"          # 事件列表里的中文名要带上
    assert it["voice"] == "066" and it["fault_code"] == "156"
    assert it["led_home"] == "red_slow_flick"


def test_parse_response_bad_json():
    with pytest.raises(ValueError):
        ai_prd.parse_response("这不是 JSON", EVENTS)


def test_call_relay_uses_configurable_endpoint(monkeypatch):
    seen = {}

    class Resp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "{\"items\": []}"}}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.update(url=url, headers=headers, json=json, timeout=timeout)
        return Resp()

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    out = ai_prd.call_relay("https://relay.example/v1/", "sk-test",
                            "model-x", [{"role": "user", "content": "hi"}])
    assert "items" in out
    assert seen["url"] == "https://relay.example/v1/chat/completions"
    assert seen["headers"]["Authorization"] == "Bearer sk-test"
    assert seen["json"]["model"] == "model-x"
    assert seen["json"]["max_tokens"] == ai_prd.DEFAULT_MAX_TOKENS
    # 已带完整 endpoint 时不再重复拼接
    ai_prd.call_relay("https://relay.example/v1/chat/completions", "",
                      "m", [])
    assert seen["url"].endswith("/v1/chat/completions")
    assert "Authorization" not in seen["headers"]
    with pytest.raises(ValueError):
        ai_prd.call_relay("", "", "m", [])


def test_parse_prd_end_to_end(monkeypatch, tmp_path):
    p = tmp_path / "prd.txt"
    p.write_text("风机异常 066 红灯慢闪 156", encoding="utf-8")
    monkeypatch.setattr(ai_prd, "call_relay",
                        lambda *a, **k: json.dumps({"items": [{
                            "event": "error_fan_stuck", "voice": "066",
                            "fault_code": "156"}]}))
    items = ai_prd.parse_prd(str(p), EVENTS, "https://x/v1", "", "m")
    assert items[0]["event"] == "error_fan_stuck"


def test_is_1m_model_marker():
    assert ai_prd.is_1m_model("gemini-2.5-pro-1m") is True
    assert ai_prd.is_1m_model("some_1m_model") is True
    assert ai_prd.is_1m_model("xxx 1M") is True
    assert ai_prd.is_1m_model("gpt-4o") is False
    assert ai_prd.is_1m_model("31m-model") is False
    assert ai_prd.is_1m_model("1million") is False


def test_fetch_models_openai_style(monkeypatch):
    class Resp:
        status_code = 200

        def json(self):
            return {"data": [{"id": "gpt-4o"},
                             {"id": "gemini-2.5-pro-1m"}]}

    seen = {}

    def fake_get(url, headers=None, timeout=None):
        seen.update(url=url, headers=headers)
        return Resp()

    import requests
    monkeypatch.setattr(requests, "get", fake_get)
    out = ai_prd.fetch_models("http://relay:8081/v1/", "sk-x")
    assert out == ["gemini-2.5-pro-1m", "gpt-4o"]
    assert seen["url"] == "http://relay:8081/v1/models"
    assert seen["headers"]["Authorization"] == "Bearer sk-x"
    # 传完整 chat endpoint 也能找到 /models
    ai_prd.fetch_models("http://relay:8081/v1/chat/completions", "sk")
    assert seen["url"] == "http://relay:8081/v1/models"


def test_extract_text_respects_1m_max_chars(tmp_path):
    p = tmp_path / "big.txt"
    p.write_text("A" * 1000, encoding="utf-8")
    assert len(ai_prd.extract_text(str(p), max_chars=100)) < 200
    assert "已截断" in ai_prd.extract_text(str(p), max_chars=100)
    assert len(ai_prd.extract_text(str(p), max_chars=2000)) == 1000


def test_call_relay_reports_truncated_reasoning(monkeypatch):
    """推理模型把输出长度占满、没给正文时要给出可读错误(用户 2026-11-12)。"""
    class Resp:
        status_code = 200

        def json(self):
            return {"choices": [{"finish_reason": "length", "message": {
                "role": "assistant", "reasoning_content": "想了很久" * 100}}],
                "usage": {"completion_tokens": 8192}}

    import requests
    monkeypatch.setattr(requests, "post",
                        lambda *a, **k: Resp())
    with pytest.raises(RuntimeError) as ei:
        ai_prd.call_relay("http://x/v1", "", "m", [])
    assert "截断" in str(ei.value) and "最大输出 tokens" in str(ei.value)


def test_claude_uses_messages_api(monkeypatch):
    """Claude 模型走 Anthropic Messages: x-api-key + system 独立字段。"""
    seen = {}

    class Resp:
        status_code = 200

        def json(self):
            return {"content": [{"type": "text",
                                 "text": "{\"items\": []}"}],
                    "stop_reason": "end_turn"}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.update(url=url, headers=headers, json=json)
        return Resp()

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    out = ai_prd.call_relay(
        "http://relay:8081/v1", "sk-ant", "claude-sonnet-5-5",
        [{"role": "system", "content": "SYS"},
         {"role": "user", "content": "PRD"}], max_tokens=4096)
    assert "items" in out
    assert seen["url"] == "http://relay:8081/v1/messages"
    assert seen["headers"]["x-api-key"] == "sk-ant"
    assert seen["headers"]["anthropic-version"] == "2023-06-01"
    assert seen["json"]["system"] == "SYS"
    assert seen["json"]["messages"] == [{"role": "user", "content": "PRD"}]
    assert seen["json"]["max_tokens"] == 4096


def test_claude_truncated_and_empty(monkeypatch):
    class Resp:
        status_code = 200

        def json(self):
            return {"content": [], "stop_reason": "max_tokens",
                    "usage": {"output_tokens": 4096}}

    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: Resp())
    with pytest.raises(RuntimeError) as ei:
        ai_prd.call_relay("http://r/v1", "k", "claude-sonnet-5-5", [])
    assert "截断" in str(ei.value)


def test_claude_falls_back_to_openai_endpoint(monkeypatch):
    calls = []

    class NotFound:
        status_code = 404
        text = "not found"

    class Ok:
        status_code = 200
        text = ""

        def json(self):
            return {"choices": [{"message": {"content": "{\"items\": []}"},
                                 "finish_reason": "stop"}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(url)
        return NotFound() if len(calls) == 1 else Ok()

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    out = ai_prd.call_relay("http://relay:8081", "k",
                            "claude-sonnet-5-5", [])
    assert "items" in out
    assert calls[0].endswith("/v1/messages")
    assert calls[1].endswith("/chat/completions")


def test_parse_response_tolerates_tool_markers_and_trailing_text():
    raw = ('<|tool_call>PRD_CONTENT> [{"event":"error_fan_stuck",'
           '"voice":"066"},{"event":"error_drop","voice":"117"}] '
           '注意：以上为结果')
    items = ai_prd.parse_response(raw, EVENTS)
    assert [i["event"] for i in items] == ["error_fan_stuck", "error_drop"]
    # 前后说明文字 + {"items"} 包装也能取到
    raw2 = ('以下是解析结果：\n{"items":[{"event":"error_drop",'
            '"voice":"117"}]}\n完毕。')
    assert ai_prd.parse_response(raw2, EVENTS)[0]["event"] == "error_drop"


def _voice_xlsx(path):
    import openpyxl
    from openpyxl.styles import Font
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "主机语音定义"
    ws.append(["语音编号", "触发条件", "语音内容参考示范", "中文", "英文",
               "场景类型", "机器人停机状态", "备注"])
    ws.append(["Q066", "风机异常", "风机异常，请检查", "风机异常，请检查",
               "", "错误", "停机", ""])
    ws.append(["Q001", "开机", "开机音乐", "已开机", "", "", "不停机",
               "三星项目Q001取消播报"])
    ws.append(["Q020", "配网完成", "联网成功", "联网成功", "", "", "", ""])
    ws.append(["Q060", "旧语音", "旧语音", "旧语音", "", "", "", ""])
    ws.cell(row=5, column=2).font = Font(strike=True)   # 划线取消(Q060)
    ws.append(["Q030", "风机开路", "风机异常", "[复用Q066]", "", "", "", ""])
    ws.append(["Q040", "静音", "静音", "静音", "", "", "", "暂不提供语音"])
    ws.append(["Q050", "清扫完成", "清扫完成", "清扫完成", "", "", "", ""])
    ws.append(["Q166", "集尘过于频繁", "集尘过于频繁", "【无此功能】",
               "", "", "", ""])
    wb.save(path)


def test_voice_catalog_filters_and_resolves_reuse(tmp_path):
    p = tmp_path / "prd.xlsx"
    _voice_xlsx(p)
    cat = ai_prd.extract_voice_catalog(str(p))
    assert cat is not None
    assert 66 in cat["active"] and 50 in cat["active"]
    assert cat["active"][30]["final"] == 66        # 复用 Q066
    assert cat["active"][66]["kind"] == "错误"     # 场景类型
    assert cat["map"][30] == 66
    assert cat["excluded"][1] == "取消播报"
    assert cat["excluded"][60] == "划线取消"       # 划线取消
    assert cat["excluded"][40] == "暂不提供"
    assert cat["excluded"][166] == "无此功能"

    msgs = ai_prd.build_messages("PRD", EVENTS, voice_catalog=cat)
    text = msgs[1]["content"]
    assert "PRD 可用语音清单" in text and "Q030" in text and "最终 Q066" in text
    assert "禁止使用" in text and "Q040" in text

    items = [
        {"event": "error_fan_stuck", "voice": "030", "note": ""},
        {"event": "error_drop", "voice": "001", "note": ""},
        {"event": "error_drop", "voice": "040", "note": ""},
        {"event": "error_drop", "voice": "050", "note": ""},
    ]
    out = ai_prd.apply_voice_catalog(items, cat)
    assert [i["voice"] for i in out] == ["066", "050"]
    assert out[0]["note"].startswith("复用Q066")
    assert out[0]["kind"] == "故障"


def test_parse_prd_applies_voice_catalog(monkeypatch, tmp_path):
    p = tmp_path / "prd.xlsx"
    _voice_xlsx(p)
    cat = ai_prd.extract_voice_catalog(str(p))
    monkeypatch.setattr(ai_prd, "call_relay",
                        lambda *a, **k: json.dumps({"items": [
                            {"event": "error_fan_stuck", "voice": "030"},
                            {"event": "error_drop", "voice": "001"}]}))
    items = ai_prd.parse_prd(str(p), EVENTS, "http://x/v1", "", "m",
                             voice_catalog=cat)
    assert len(items) == 1 and items[0]["voice"] == "066"


def test_kind_inference_when_prd_missing():
    """PRD 没标场景类型时按事件/文案推断, 保证类型列不空(用户 2026-11-12)。"""
    assert ai_prd.infer_kind("error_fan_stuck", "风机卡住",
                             "主机风机异常，请检查") == "故障"
    assert ai_prd.infer_kind("clean_finish", "清扫完成事件",
                             "清洁完成，返回基站") == "提醒"
    assert ai_prd.infer_kind("clean_start", "开始清扫",
                             "开始清洁") == "通用"
    assert ai_prd.infer_kind("station_clean_water_pump_fault",
                             "基站清水泵故障") == "基站"
    msgs = ai_prd.build_messages("PRD", EVENTS)
    assert "推断 kind" in msgs[1]["content"]


def test_reuse_note_is_final_voice_only():
    """备注复用说明统一写最终编号: 复用Q020(用户 2026-11-12)。"""
    assert ai_prd._reuse_note("复用自 Q033→最终 Q020（尘满取出尘盒）",
                              20) == "复用Q020"
    assert ai_prd._reuse_note("", 66) == "复用Q066"
    assert ai_prd._reuse_note("说明；复用自Q033→最终Q020", 20) == "说明；复用Q020"


def test_rows_from_catalog_carry_voice_key_and_source():
    cat = {"active": {
        66: {"qid": "Q066", "kind": "错误", "condition": "风机异常",
             "text": "风机异常", "chinese": "主机风机异常，请检查",
             "final": 66}},
        "excluded": {}, "map": {66: 66}}
    rows = ai_prd.rows_from_catalog(
        [{"event": "error_fan_stuck", "voice": "066",
          "voice_text": "风机异常", "source": "memory"}], cat)
    assert len(rows) == 1
    assert rows[0]["voice_key"] == "风机异常"
    assert rows[0]["source"] == "memory"


def test_placeholder_chinese_uses_trigger_condition():
    """中文列是【无此功能】等占位时用触发条件当名称(如 Q166 集尘过于频繁)。"""
    assert ai_prd.voice_display_text("集尘过于频繁", "【无此功能】") == "集尘过于频繁"
    assert ai_prd.voice_display_text("风机异常", "主机风机异常，请检查") ==         "主机风机异常，请检查"
    cat = {"active": {166: {"qid": "Q166", "kind": "提醒",
                            "condition": "集尘过于频繁",
                            "text": "集尘过于频繁", "chinese": "【无此功能】",
                            "final": 166}},
           "excluded": {}, "map": {166: 166}}
    rows = ai_prd.rows_from_catalog(
        [{"event": "base_station_collect_dust_frequently", "voice": "166"}],
        cat)
    assert rows[0]["name"] == "集尘过于频繁"
    assert rows[0]["voice_text"] == "集尘过于频繁"


def test_single_voice_note_is_not_reuse():
    """没有复用/合并的语音, 备注不能写成复用Qxxx(用户 2026-11-12)。"""
    cat = {"active": {66: {"qid": "Q066", "kind": "错误",
                            "condition": "风机异常", "text": "风机异常",
                            "chinese": "主机风机异常，请检查", "final": 66}},
           "excluded": {}, "map": {66: 66}}
    rows = ai_prd.rows_from_catalog(
        [{"event": "error_fan_stuck", "voice": "066"}], cat)
    assert rows[0]["note"] == "PRD：风机异常\n（Q066）"
    assert "复用" not in rows[0]["note"]


def test_generic_voice_sheet_with_four_digit_ids(tmp_path):
    """其它项目的语音表(语音-故障-推送, 四位语音序号)也要能解析(用户 2026-11-17)."""
    import openpyxl
    p = tmp_path / "prd.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "语音-故障-推送"
    ws.append(["", "触发场景说明", "属性", "语音序号",
               "语音播报提醒（中文）", "语音播报提醒（英文）", "故障序号"])
    ws.append(["开关机", "开机", "正常操作", "0001",
               "纯音效（音效X）", "", "/"])
    ws.append(["", "人为主动关机", "正常操作", "0002",
               "关机", "Turning off", "/"])
    ws.append(["", "仅推送无语音", "正常操作", "0003", "/", "", "1"])
    ws.append(["故障", "边刷卡住", "异常", "0004",
               "边刷被卡住", "", "0600"])
    wb.save(p)
    cat = ai_prd.extract_voice_catalog(str(p))
    assert cat is not None
    assert cat["voice_width"] == 4
    assert set(cat["active"]) == {1, 2, 4}       # 无语音的 0003 跳过
    assert cat["active"][2]["condition"] == "人为主动关机"
    assert cat["active"][2]["kind"] == "开关机"  # 合并分类列向下沿用
    rows = ai_prd.rows_from_catalog([], cat)
    assert [r["voice"] for r in rows] == ["0001", "0002", "0004"]
    # PRD 没有 Q 号 → 显示实际语音序号, 不补 Q(用户 2026-11-17)
    assert [r["qid"] for r in rows] == ["0001", "0002", "0004"]
    assert "（0002）" in rows[1]["note"]


def test_generic_voice_sheet_marks_not_apply_comment(tmp_path):
    """PRD Comment 写 Not apply this voice 的语音带 skip_voice(用户 2026-11-17)."""
    import openpyxl
    p = tmp_path / "prd.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["触发场景说明", "语音序号", "语音播报提醒（中文）", "Comment"])
    ws.append(["配网失败", "0042", "连接失败", "[LG]  Not apply this voice"])
    ws.append(["跳过区域", "0044", "马上离开当前区域", ""])
    wb.save(p)
    cat = ai_prd.extract_voice_catalog(str(p))
    assert cat["active"][42]["skip_voice"] is True
    assert cat["active"][44]["skip_voice"] is False
    rows = ai_prd.rows_from_catalog([], cat)
    by_voice = {r["voice"]: r for r in rows}
    assert by_voice["0042"]["skip_voice"] is True
    assert by_voice["0042"]["comment"].startswith("[LG]")
    assert by_voice["0044"]["skip_voice"] is False


def test_voice_not_needed_handles_cross_line_condition():
    """跨行的 If ... not apply 仍是条件句, 不能误判为取消(用户 2026-11-17)."""
    assert not ai_prd.voice_not_needed(
        "If there is no such detection,\nWe will not apply this voice.")
    assert not ai_prd.voice_not_needed(
        "If so not apply this voice")
    assert ai_prd.voice_not_needed(
        "this voice is not necessary. Please do not apply this voice.")


def test_translate_texts_parses_json(monkeypatch):
    """PRD 英文/韩文内容翻译成中文, 报告里显示(用户 2026-11-17)."""
    monkeypatch.setattr(
        ai_prd, "call_relay",
        lambda *a, **k: '{"hello": "你好", "bad": ""}')
    assert ai_prd.translate_texts(
        ["hello", "bad"], "u", "k", "m") == {"hello": "你好"}
    monkeypatch.setattr(ai_prd, "call_relay", lambda *a, **k: "not json")
    assert ai_prd.translate_texts(["x"], "u", "k", "m") == {}


def test_generic_sheet_excludes_not_applied_ref_model(tmp_path):
    """Ref Model / LITE Voice Index 写 Not apply 的语音不适用(用户 2026-11-17)."""
    import openpyxl
    p = tmp_path / "prd.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["触发场景说明", "语音序号", "语音播报提醒（中文）",
               "Ref Model", "LITE Voice Index"])
    ws.append(["基站清洗盘被取出", "0822", "仅旋律", "Not apply", "Not apply"])
    ws.append(["基站清洗盘被放回", "0823", "仅旋律", "Not apply", "Not apply"])
    ws.append(["跳过区域", "0044", "马上离开当前区域", "New", "17"])
    wb.save(p)
    cat = ai_prd.extract_voice_catalog(str(p))
    assert cat["excluded"].get(822) == "不适用"
    assert cat["excluded"].get(823) == "不适用"
    assert 44 in cat["active"]
    rows = ai_prd.rows_from_catalog([], cat)
    assert {r["voice"] for r in rows} == {"0044"}


def test_generic_tag_only_condition_uses_translation(tmp_path):
    """场景列只是 Please add this voice 时用韩文/英文当含义(用户 2026-11-17)."""
    import openpyxl
    p = tmp_path / "prd.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["语音播报场景", "语音序号", "中文", "Korea", "English",
               "Comment"])
    ws.append(["Please add this voice", "0930", "", "防垢模块未安装",
               "Anti-scale module is not installed.",
               "[LG 2026.10.26]  Please add this voice"])
    wb.save(p)
    cat = ai_prd.extract_voice_catalog(str(p))
    info = cat["active"][930]
    assert info["condition"] == "防垢模块未安装"
    assert ai_prd.voice_display_text(
        info["condition"], info["chinese"], info["text"]) == "防垢模块未安装"


def test_generic_voice_sheet_uses_english_korea_taiwan(tmp_path):
    """中文列为空时, AI 清单一并给出英文/韩文/台湾中文语义(用户 2026-11-17)."""
    import openpyxl
    p = tmp_path / "prd.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "MR_sound"
    ws.append(["", "MR Voice Index(Silver Star)", "语音播报场景",
               "SS ZY2X 中文", "SS ZY2X 英文", "Korea ('26 6/30)",
               "Taiwan"])
    ws.append(["硬件", "0602", "边扫马达开路短路", "", "",
               "측면 회전솔 모터 문제", "檢測到邊刷馬達出現問題"])
    wb.save(p)
    cat = ai_prd.extract_voice_catalog(str(p))
    assert cat is not None and 602 in cat["active"]
    info = cat["active"][602]
    assert info["korea"].startswith("측면")
    assert info["taiwan"].startswith("檢測")
    text = ai_prd.build_messages("PRD", [], voice_catalog=cat)[1]["content"]
    assert "측면 회전솔" in text and "檢測到邊刷" in text
