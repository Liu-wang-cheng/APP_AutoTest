# -*- coding: utf-8 -*-
"""无设备自检: import 链 / 动作注册 24 项 / schema 26 项 / 分发顺序 / VLM 开关。

    python selfcheck.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 引擎侧注册的动作数(grab/match/screenshot 不进分发表,见下)
EXPECTED_ACTION_COUNT = 25
# schema 声明的动作数:不含 screenshot —— 它是通用字段,任何步骤都能挂
EXPECTED_SCHEMA_COUNT = 27
GENERIC_FIELD_KEYS = {"desc", "screenshot", "wait", "timeout", "retry"}


def main():
    import core.actions                      # noqa: F401 触发全部动作注册
    from core import registry as reg
    from core.actions.data_ops import extract_value  # noqa: F401
    from core.driver import BASE_DIR, load_yaml_file  # noqa: F401
    from core.runner import ActionRunner  # noqa: F401
    from core.trace import TraceRecorder  # noqa: F401
    from gui import schema
    from vlm.backend import VisionRouter

    order = [a.key for a in reg.dispatch_order()]
    assert len(order) == EXPECTED_ACTION_COUNT, \
        f"注册动作 {len(order)}/{EXPECTED_ACTION_COUNT}: {order}"

    # 分发优先级: click 必须最先(同一步里屏蔽后面的键)
    assert order.index("click") < order.index("assert") < order.index("diff"), order

    # grab/match 不进分发表(与主动作共存)
    assert "grab" not in order and "match" not in order
    assert "screenshot" not in order

    # schema 声明表(screenshot 不在其中:它是通用字段)
    assert len(schema.ACTIONS) == EXPECTED_SCHEMA_COUNT, len(schema.ACTIONS)
    schema_keys = {a["key"] for a in schema.ACTIONS}
    engine_keys = set(order) | {"grab", "match"}
    assert schema_keys == engine_keys, \
        f"schema 与引擎不一致: 仅schema {schema_keys - engine_keys}, 仅引擎 {engine_keys - schema_keys}"
    # 通用字段齐全(每个步骤都能挂)
    generic = {f["key"] for f in schema.GENERIC_FIELDS}
    assert generic == GENERIC_FIELD_KEYS, generic

    # fixed_bool 集合一致
    assert reg.fixed_bool_keys() == {"back", "room_zones", "split_zone",
                                     "merge_zones", "latest_record", "wait_loading",
                                     "spot_clean"}

    # VLM 默认关闭
    assert VisionRouter({}).vlm is None
    assert VisionRouter({"vlm": {"enabled": False}}).vlm is None

    print(f"selfcheck OK: {len(order)} 动作, {len(schema.ACTIONS)} schema 项")


if __name__ == "__main__":
    main()
