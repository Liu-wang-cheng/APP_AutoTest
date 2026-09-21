# -*- coding: utf-8 -*-
"""schema: 26 动作完整 / 校验 / 序列化清洗 / 摘要徽标。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gui import schema

# screenshot 不是动作,而是每个步骤都能挂的通用字段(见 GENERIC_FIELDS)
MUST = {"click", "assert", "input", "latest_record", "find_click", "if", "if not",
        "back", "long_click", "switch_to", "assert_switch", "add_timer", "swipe",
        "room_zones", "room_click", "merge_zones", "split_zone", "if_click",
        "compare", "set_time", "wait_loading", "wait_for", "diff",
        "grab", "match", "spot_clean"}
GENERIC = {"desc", "screenshot", "wait", "timeout", "retry"}


def test_actions_complete():
    keys = {a["key"] for a in schema.ACTIONS}
    assert keys == MUST, f"缺失 {MUST - keys}, 多余 {keys - MUST}"
    assert len(schema.ACTIONS) == 26


def test_screenshot_is_generic_field():
    """screenshot 挂在通用字段里 —— 任何步骤都能加,而不是一个独立动作"""
    assert {f["key"] for f in schema.GENERIC_FIELDS} == GENERIC


def test_every_action_has_category_and_fields():
    for a in schema.ACTIONS:
        assert a["category"], a["key"]
        assert isinstance(a["fields"], list), a["key"]
        assert a["key"] in schema.ACTION_BY_KEY


def test_validate_and_serialize():
    s = schema.new_step("click")
    s["click"] = " 开始清扫.png "
    s["desc"] = "点它"
    out = schema.serialize_step(s)
    assert out["click"] == "开始清扫.png"        # strip
    assert schema.validate_step(out) == []

    bad = schema.new_step("click")
    bad.pop("click")
    assert schema.validate_step(bad)             # 必填缺失有报错


def test_fixed_bool_keys_declared():
    """无参动作在 schema 里标了 fixed_bool(序列化时保证标量 True)"""
    assert schema.FIXED_BOOL["back"] is True
    assert schema.FIXED_BOOL["wait_loading"] is True
    assert schema.FIXED_BOOL["merge_zones"] is True
    assert schema.FIXED_BOOL["split_zone"] is True
    assert schema.FIXED_BOOL["latest_record"] is True


def test_serialize_fixed_bool_scalar():
    """fixed_bool 动作即使值写成非布尔,序列化后也要变成标量 True"""
    out = schema.serialize_step({"desc": "返回", "back": "true"})
    assert out["back"] is True


def test_serialize_empty_int4_room_zones_becomes_true():
    s = schema.new_step("room_zones")
    s["room_zones"] = []                          # int4 空 → True(默认区域)
    assert schema.serialize_step(s)["room_zones"] is True


def test_serialize_empty_int4_others_dropped():
    s = schema.new_step("switch_to")
    s["switch_to"] = "打开"
    s["switch_area"] = []                         # 非 room_zones 的空 int4 → 省略
    out = schema.serialize_step(s)
    assert "switch_area" not in out


def test_serialize_group_empty_becomes_true():
    s = schema.new_step("add_timer")
    s["add_timer"] = {"add_text": "", "add_tpl": "", "max_tasks": None}
    assert schema.serialize_step(s)["add_timer"] is True


def test_serialize_else_recursive():
    s = {"desc": "条件", "if":  "扫地机器人",
         "else": [{"desc": "子步骤", "click": " 确认 ", "timeout": None}]}
    out = schema.serialize_step(s)
    assert out["else"][0]["click"] == "确认"
    assert "timeout" not in out["else"][0]


def test_validate_else_recursive():
    s = {"if": "x", "else": [{"desc": "坏的子步骤", "click": ""}]}
    errs = schema.validate_step(s)
    assert any("else子步骤1" in e for e in errs)


def test_step_summary_badges():
    s = {"desc": "点开始", "click": "开始清扫.png", "retry": 2, "timeout": 15}
    txt = schema.step_summary(s)
    assert "点开始" in txt and "↻2" in txt and "⏱15s" in txt


def test_step_summary_else_badge():
    s = {"desc": "分支", "if": "扫地机器人", "else": [{"click": "a"}, {"click": "b"}]}
    assert "▸else 2步" in schema.step_summary(s)


def test_unknown_action_reported():
    assert schema.validate_step({"desc": "啥也不是", "unknown_key": 1})
