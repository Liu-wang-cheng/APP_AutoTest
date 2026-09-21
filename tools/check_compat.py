# -*- coding: utf-8 -*-
"""检查用例步骤与执行引擎的兼容性。

    python tools/check_compat.py [用例文件]

查两类问题:

1. **一步里有多个动作键** —— 引擎 `_execute` 按 priority 找到第一个命中的动作
   就 break,其余动作被**静默忽略**。写用例时的直觉是"这些都会执行",所以这里
   把每个多动作步骤都列出来,并标明实际会执行哪个、哪些会被丢掉。

2. **参数形态与实现预期不符** —— 如 room_click 给了非数字、set_time 给了非数字、
   add_timer 给了非 mapping 等,这类要到真机跑起来才炸。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.actions  # noqa: F401 触发动作注册
from core import registry as reg
from core.driver import BASE_DIR, load_yaml_file
from gui import schema
from tests.test_yaml_runner import count_steps, iter_all_steps, iter_modules

ACTION_KEYS = {a["key"] for a in schema.ACTIONS}
# 这些键在 runner._execute 里于主动作之后**单独**执行,可与主动作共存于一步,
# 不构成"多动作互斥"。判据见 core/runner.py::_execute 的 2/3/4 段。
COEXIST_KEYS = {"screenshot", "grab", "match", "wait_for"}
PRIORITY = {a.key: a.priority for a in reg.dispatch_order()}
META = {"desc", "wait", "timeout", "retry", "threshold", "duration", "circular",
        "switch_area", "switch_tpl", "switch_label", "else", "wait_after"}

# 各动作对参数形态的预期
INT_LIKE = {"room_click", "set_time"}
DICT_LIKE = {"add_timer"}
STR_OR_LIST = {"find_click", "grab", "match"}


def check_step(step):
    """返回该步骤的问题列表"""
    problems = []
    keys = set(step) - META
    actions = (keys & ACTION_KEYS) - COEXIST_KEYS

    if len(actions) > 1:
        ordered = sorted(actions, key=lambda k: PRIORITY.get(k, 999))
        problems.append(
            f"多动作共存: {ordered} —— 只会执行「{ordered[0]}」"
            f"(priority={PRIORITY.get(ordered[0])}), "
            f"其余 {ordered[1:]} 被忽略")

    for key in actions:
        val = step[key]
        if key in INT_LIKE and not isinstance(val, (int, float)) and not isinstance(val, bool):
            if not (isinstance(val, str) and val.strip().lstrip("-").isdigit()):
                problems.append(f"{key} 期望数字, 实际 {type(val).__name__}={val!r}")
        if key in DICT_LIKE and not isinstance(val, dict):
            problems.append(f"{key} 期望 mapping(如 add_text/add_tpl), "
                            f"实际 {type(val).__name__}={val!r}")
        if key in STR_OR_LIST and not isinstance(val, (str, list)):
            problems.append(f"{key} 期望字符串或列表, 实际 {type(val).__name__}")

    # click 值的形态提示(不报错,只统计)
    return problems


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    cases_dir = os.path.join(BASE_DIR, "Test_cases")
    if only:
        files = [os.path.basename(only)]
    else:
        files = [f for f in sorted(os.listdir(cases_dir)) if f.endswith((".yaml", ".yml"))]

    multi_action = 0
    type_issues = 0
    total_steps = 0
    top_steps = 0

    for fn in files:
        try:
            data = load_yaml_file(os.path.join(cases_dir, fn))
        except Exception as e:
            print(f"[跳过] {fn}: {e}")
            continue
        for module, cases in iter_modules(data, fn):
            for case in cases:
                label = f"{module}/{case.get('name', '')}"
                steps = case.get("steps") or []
                top, _ = count_steps(steps)
                top_steps += top
                # 连同 else 子步骤一起查 —— 子步骤同样是会执行的步骤
                for si, (step, depth) in enumerate(iter_all_steps(steps)):
                    total_steps += 1
                    for p in check_step(step):
                        if p.startswith("多动作"):
                            multi_action += 1
                        else:
                            type_issues += 1
                        desc = str(step.get("desc", "")).strip() or "(无描述)"
                        prefix = "  " * depth
                        print(f"[{label}] 步骤{si + 1} {prefix}「{desc}」\n    {p}")

    print(f"\n扫描 {len(files)} 个文件 / 顶层 {top_steps} 步 / 展开后 {total_steps} 步")
    print(f"多动作共存: {multi_action} 处")
    print(f"参数形态问题: {type_issues} 处")
    if not multi_action and not type_issues:
        print("全部兼容")
    return 1 if (multi_action or type_issues) else 0


if __name__ == "__main__":
    sys.exit(main())
