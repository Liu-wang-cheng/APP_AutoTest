# -*- coding: utf-8 -*-
"""本地 PRD 记忆库: 精确/向量近似命中、人工优先、持久化。"""
import pytest

from core.apos.prd_memory import PrdMemory, voice_key


def test_voice_key_normalizes():
    assert voice_key("主机风机异常，请检查") == "主机风机异常请检查"
    assert voice_key("", "  清洁完成，返回基站 ") == "清洁完成返回基站"


def test_memory_exact_and_fuzzy(tmp_path):
    p = tmp_path / "mem.json"
    m = PrdMemory(str(p))
    key = voice_key("主机风机异常，请检查")
    m.put(key, {"event": "error_fan_stuck", "kind": "故障"})
    hit, score = m.best(key)
    assert score == 1.0 and hit["event"] == "error_fan_stuck"
    # 措辞略有差异 → 本地向量近似命中
    hit2, score2 = m.best(voice_key("风机异常请检查"), threshold=0.5)
    assert hit2 and hit2["event"] == "error_fan_stuck"
    assert 0.5 <= score2 < 1.0
    # 明显无关的描述不命中
    hit3, _ = m.best(voice_key("开始回充"), threshold=0.72)
    assert hit3 is None


def test_user_result_not_overwritten_by_ai(tmp_path):
    m = PrdMemory(str(tmp_path / "mem.json"))
    key = voice_key("风机异常")
    m.put(key, {"event": "error_fan_stuck"}, source="ai")
    m.put(key, {"event": "error_kit_fan_err"}, source="user")
    m.put(key, {"event": "error_other"}, source="ai")
    assert m.exact(key)["event"] == "error_kit_fan_err"
    assert m.exact(key)["source"] == "user"


def test_memory_persists(tmp_path):
    path = tmp_path / "mem.json"
    PrdMemory(str(path)).put(voice_key("低电量"), {"event": "low_bat"})
    m2 = PrdMemory(str(path))
    assert m2.exact(voice_key("低电量"))["event"] == "low_bat"


def test_seed_export_and_load_do_not_override_local(tmp_path):
    """种子库只补空缺, 不覆盖本机学习; 导出不含 IP/SN 字段。"""
    from core.apos.prd_memory import PrdMemory
    m = PrdMemory(str(tmp_path / "mem.json"))
    m.put("风机异常", {"event": "error_fan_stuck", "event_name": "风机卡住",
                       "voice": "066", "qid": "Q066",
                       "ap_sound": "066", "verified_at": "2026-11-13"},
          source="verified")
    m.put("旧AI", {"event": "x", "voice": "999"}, source="ai")
    seed = tmp_path / "seed.json"
    assert m.export_seed(str(seed), sources=("verified",)) == 1
    data = seed.read_text(encoding="utf-8")
    assert "192.168" not in data and "sn" not in data.lower()
    # 种子只共享语义映射, 语音号/故障码/灯显不跨项目(用户 2026-11-16)
    entry = __import__("json").loads(data)["entries"][0]
    assert entry["event"] == "error_fan_stuck"
    for field in ("voice", "qid", "ap_sound", "fault_code", "led_expected"):
        assert field not in entry
    m2 = PrdMemory(str(tmp_path / "m2.json"))
    m2.put("风机异常", {"event": "user_event"}, source="user")
    assert m2.load_seed_file(str(seed)) == 0      # 已有本机映射不覆盖
    assert m2.exact("风机异常")["event"] == "user_event"
    m3 = PrdMemory(str(tmp_path / "m3.json"))
    assert m3.load_seed_file(str(seed)) == 1
    hit = m3.exact("风机异常")
    assert hit["event"] == "error_fan_stuck" and hit["source"] == "seed"


def test_seed_review_rejects_ai_flagged_rows():
    """共享种子上传前, AI 提出修正的条目一律拒绝(用户 2026-11-13)."""
    from core.apos.seed_review import review_entries
    entries = [{"key": "风机异常", "event": "error_fan_stuck",
                "voice": "066"},
               {"key": "充电", "event": "charging", "voice": "014"}]
    expect = {"error_fan_stuck": {"name": "风机卡住",
                                  "sound": {"identifier": "066"}},
              "charging": {"name": "开始充电", "sound": {"identifier": "014"}}}

    def fake(_prompt):
        return ('{"items":[{"key":"风机异常","event":"other",'
                '"reason":"不匹配"}]}')

    approved, rejected, errors = review_entries(entries, expect, fake)
    assert errors == []
    assert [e["key"] for e in approved] == ["充电"]
    assert [e["key"] for e in rejected] == ["风机异常"]
    assert rejected[0]["review_reason"] == "不匹配"


def test_seed_audit_returns_only_valid_corrections():
    """AI 审核学习库：只接受可用事件表里的修正(用户 2026-11-13)."""
    from core.apos.seed_review import audit_entries
    entries = [
        {"key": "风机异常", "event": "error_fan_stuck",
         "event_name": "风机卡住", "voice_text": "主机风机异常"},
        {"key": "充电", "event": "charging", "event_name": "开始充电",
         "voice_text": "开始充电"},
        {"key": "回充", "event": "back_home", "event_name": "回充",
         "voice_text": "开始回充"},
    ]
    allowed = {"error_fan_stuck": "风机卡住", "error_kit_fan_err": "风机异常",
               "charging": "开始充电", "back_home": "回充"}

    def fake(_prompt):
        return ('{"items":[{"key":"风机异常","event":"error_kit_fan_err",'
                '"reason":"更准确"},{"key":"充电","event":"not_exists",'
                '"reason":"非法"},{"key":"回充","event":"back_home",'
                '"reason":"未变化"},{"key":"不存在","event":"charging",'
                '"reason":"未知"}]}')

    corrections, checked, errors = audit_entries(entries, allowed, fake)
    assert errors == [] and checked == 3
    assert corrections == [{"key": "风机异常",
                            "event": "error_kit_fan_err",
                            "current_event": "error_fan_stuck",
                            "reason": "更准确"}]


def test_verified_source_is_not_overwritten_by_ai(tmp_path):
    m = PrdMemory(str(tmp_path / "m.json"))
    m.put("风机异常", {"event": "error_fan_stuck"}, source="verified")
    m.put("风机异常", {"event": "wrong_ai"}, source="ai")
    assert m.exact("风机异常")["event"] == "error_fan_stuck"
    m.put("风机异常", {"event": "corrected"}, source="ai_review",
          force=True)
    assert m.exact("风机异常")["event"] == "corrected"


def test_memory_matches_ap_event_chinese_name(tmp_path):
    """本地匹配也要参考 AP 事件中文名(用户 2026-11-12)。"""
    m = PrdMemory(str(tmp_path / "m.json"))
    m.put("右侧传感器脏污请擦拭",
          {"event": "error_psd_dirty", "event_name": "沿墙传感器异常"},
          source="ai")
    # 新 PRD 用 AP 的中文事件名描述, 也能命中已有映射
    hit, score = m.best("沿墙传感器异常", threshold=0.6)
    assert hit and hit["event"] == "error_psd_dirty"
    assert score >= 0.6


def test_library_stores_only_semantics(tmp_path):
    """学习库只存 PRD 语义 → AP 事件, 不存 Q号/语音号/故障码/灯显/机型."""
    import json

    path = tmp_path / "mem.json"
    m = PrdMemory(str(path))
    m.put("风机异常", {
        "event": "error_fan_stuck", "event_name": "风机卡住",
        "kind": "故障", "voice_text": "主机风机异常，请检查",
        "voice": "066", "qid": "Q066", "fault_code": "926",
        "fault_push": "DA_RC_E_926", "ap_sound": "066",
        "led_expected": "红灯慢闪", "model": "CLA_HS4",
        "firmware": "2.4.17", "ap_fingerprint": "abc",
        "verified_at": "2026-11-17 10:00:00"}, source="verified")
    hit = m.exact("风机异常")
    assert hit["event"] == "error_fan_stuck"
    assert hit["event_name"] == "风机卡住"
    assert hit["kind"] == "故障"
    assert hit["voice_text"] == "主机风机异常，请检查"
    assert hit["source"] == "verified"
    for field in ("voice", "qid", "fault_code", "fault_push", "ap_sound",
                  "led_expected", "model", "firmware", "ap_fingerprint"):
        assert field not in hit
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert "voice" not in saved["风机异常"]


def test_legacy_library_entries_are_cleaned_on_load(tmp_path):
    """老版本学习库里存的语音号/Q号/故障码, 加载时清掉并落盘."""
    import json

    path = tmp_path / "mem.json"
    path.write_text(json.dumps({"风机异常": {
        "event": "error_fan_stuck", "voice": "066", "qid": "Q066",
        "fault_code": "926", "voice_text": "风机异常"}},
        ensure_ascii=False), encoding="utf-8")
    m = PrdMemory(str(path))
    hit = m.exact("风机异常")
    assert hit["event"] == "error_fan_stuck"
    assert hit["voice_text"] == "风机异常"
    assert "voice" not in hit and "qid" not in hit
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert "voice" not in saved["风机异常"]
    assert "fault_code" not in saved["风机异常"]


def test_save_merges_other_instance_updates(tmp_path):
    """两个实例先后保存不能互相回滚; 删除带墓碑不会被复活."""
    from core.apos.prd_memory import PrdMemory
    path = str(tmp_path / "mem.json")
    a = PrdMemory(path)
    b = PrdMemory(path)
    a.put("甲", {"event": "e1", "event_name": "E1"}, source="user")
    b.put("乙", {"event": "e2", "event_name": "E2"}, source="user")
    assert set(PrdMemory(path).items) == {"甲", "乙"}
    # a 删除甲后保存, 磁盘上不能复活
    a.delete("甲")
    assert "甲" not in PrdMemory(path).items
    assert "乙" in PrdMemory(path).items
