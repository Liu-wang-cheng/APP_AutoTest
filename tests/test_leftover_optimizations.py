# -*- coding: utf-8 -*-
"""遗留项优化的守护(2026-10-15, 用户: "乙和丙可以尝试优化一下…但要保证功能不受影响")。

这里锁三件容易回退的小事:
  ① if/if not 的 `timeout` 是**检测次数**不是秒 —— 界面标签与摘要徽标必须如实反映
     (引擎语义一字未动, 只是别让用户以为填的是秒);
  ② 测试之间不许通过 `core.vision` 的**模块级全局**串味(APP 组上下文);
  ③ `config/config.example.yaml` 必须覆盖真实 config 的所有键 —— 否则新增配置项只加到
     真实配置时, 新用户生成的 config 会缺项(代码静默走默认值)。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent


def test_if_timeout_is_labelled_as_checks_not_seconds():
    """★ if 步骤的 timeout 单位是"次"(历史包袱), 界面要如实说 —— 纯文案, 不改引擎"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from gui import schema

    assert "⏱10次" in schema.step_summary({"if": "充电中", "timeout": 10})
    assert "⏱10s" in schema.step_summary({"assert": "充电中", "timeout": 10}), \
        "非 if 动作的 timeout 仍是秒"
    # 字段标签: 由 step_card 按动作替换(这里只锁数据源, 卡片渲染有 GUI 测试守着)
    gen = {f["key"]: f["label"] for f in schema.GENERIC_FIELDS}
    assert gen["timeout"] == "超时(秒)"


def test_app_group_global_is_isolated_between_tests():
    """★ APP 组上下文是模块级全局: conftest 必须前后还原, 否则串给下一个测试

    实测事故(2026-10-15): test_preconditions 把组设成「组A」, 后面 test_actions_basic
    的截图断言就落到 `Test_cases/组A/...` —— 全量跑红、单跑绿。
    """
    import inspect
    from tests import conftest

    src = inspect.getsource(conftest)
    assert "_reset_template_app_group" in src, "conftest 缺 APP 组还原夹具"
    assert "set_template_app_group" in src


def test_config_example_covers_real_config_keys():
    """★ 示例配置必须覆盖真实配置的每个键(否则新用户生成的 config 缺项)"""
    import yaml

    def _keys(d, prefix=""):
        out = set()
        for k, v in (d or {}).items():
            out.add(prefix + str(k))
            if isinstance(v, dict):
                out |= _keys(v, prefix + str(k) + ".")
        return out

    real = yaml.safe_load((ROOT / "config" / "config.yaml").read_text(encoding="utf-8"))
    example = yaml.safe_load(
        (ROOT / "config" / "config.example.yaml").read_text(encoding="utf-8"))
    missing = _keys(real) - _keys(example)
    # update 段由 bootstrap 自动补进真实配置(见 _ensure_update_section), 允许它不在示例里
    missing = {k for k in missing if not k.startswith("update")}
    assert not missing, (
        "这些键在真实 config.yaml 里有、config.example.yaml 里没有 —— "
        f"新用户生成出来的配置会缺它们: {sorted(missing)}")
