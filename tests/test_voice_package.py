# -*- coding: utf-8 -*-
"""批量测试前的机器人语音包检查(复用语音也要检查)。"""
from core.apos import voice_package


def test_voice_filename_special_q001_and_padding():
    assert voice_package.voice_filename("Q001") == "Q001.mp3"
    assert voice_package.voice_filename("Q003") == "003.mp3"
    assert voice_package.voice_filename("124") == "124.mp3"
    assert voice_package.voice_filename(1000) == "1000.mp3"
    # 四位编号的项目(ZY2L)保留四位文件名
    assert voice_package.voice_filename("0001") == "0001.mp3"
    assert voice_package.voice_filename("0600") == "0600.mp3"
    assert voice_package.voice_filename("") == ""


def test_required_files_uses_prd_voice_not_ap_sound():
    """语音包检查按 PRD 语音号评估 PRD 需求, 不按 AP 配置语音号."""
    rows = [{"qid": "Q003", "event": "try_poweroff_on_charger",
             "voice": "0003", "voice_text": "充电中关机"}]
    expect = {"try_poweroff_on_charger":
              {"sound": {"identifier": "0030"}}}
    assert set(voice_package.required_files(rows, expect)) == {"0003.mp3"}
    assert set(voice_package.required_files(rows)) == {"0003.mp3"}


def test_required_files_includes_own_and_reuse():
    rows = [{"qid": "Q138", "voice": "138", "reuse_voice": "066",
             "voice_text": "风机开路或短路"},
            {"qid": "Q124", "voice": "124", "voice_text": "轮子离地"}]
    req = voice_package.required_files(rows)
    assert set(req) == {"138.mp3", "066.mp3", "124.mp3"}
    assert req["066.mp3"][0]["field"] == "复用语音"
    assert req["066.mp3"][0]["qid"] == "Q138"


def test_required_files_skips_prd_no_voice():
    rows = [{"qid": "Q191", "voice": "191",
             "voice_text": "暂时不提供语音", "note": "PRD：集尘中断"}]
    assert voice_package.required_files(rows) == {}
    assert voice_package.check_files(rows, []) == []


def test_required_files_skips_prd_not_apply_voice():
    """PRD 明确 Not apply this voice / 划线的语音不计入必需文件(用户 2026-11-17)."""
    rows = [
        {"qid": "0042", "voice": "0042", "voice_text": "连接失败",
         "comment": "[LG_26.4.24]  Not apply this voice"},
        {"qid": "0044", "voice": "0044", "voice_text": "马上离开当前区域"},
        {"qid": "0046", "voice": "0046", "skip_voice": True},
        {"qid": "0048", "voice": "0048", "voice_text": "无需播放的提示"},
    ]
    assert set(voice_package.required_files(rows)) == {"0044.mp3"}


def test_required_files_prefers_translated_prd_content():
    """语音包检查页的 PRD 内容用中文翻译(用户 2026-11-17)."""
    rows = [{"qid": "0930", "voice": "0930",
             "voice_text": "충전대 급수통에 스케일 방지 모듈이 없습니다.",
             "prd_content_zh": "充电座供水箱没有防垢模块，请安装。"}]
    req = voice_package.required_files(rows)
    assert req["0930.mp3"][0]["condition"] == "充电座供水箱没有防垢模块，请安装。"


def test_check_files_reports_missing_with_qid():
    rows = [{"qid": "Q166", "voice": "166", "voice_text": "集尘过于频繁"},
            {"qid": "Q003", "voice": "003", "voice_text": "已开机"}]
    missing = voice_package.check_files(rows, ["003.mp3"])
    assert len(missing) == 1
    assert missing[0]["file"] == "166.mp3"
    assert missing[0]["qids"] == ["Q166"]


def test_check_files_accepts_empty_password_result_shape():
    # SSH 不可用时调用方仍能拿到统一结构, 不阻断批量测试
    res = voice_package.check_files([], [])
    assert res == []
