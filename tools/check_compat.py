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

# ★ 控制台编码兜底: 英文 Windows(cp1252)下打印中文会 UnicodeEncodeError 崩掉,
#   重定向到文件/管道时同样按 ANSI 代码页 —— CI(windows-latest) 实测全崩。
from core.console import force_utf8_stdout          # noqa: E402
force_utf8_stdout()

# ★ 这一行**不是**没用的 import: `import core.actions` 会注册全部动作, 下面
#   `reg.dispatch_order()` 才有内容 —— 没有它, 所有动作的 priority 会退化成兜底 999,
#   "只会执行哪个" 的判断就错了。
#   实测(2026-10-10): 拿掉它输出不变, 因为同模块的 `from gui import schema` /
#   `tests.test_yaml_runner` 也顺带注册了 —— 也就是说**今天冗余, 但删了就是埋雷**
#   (哪天那两个 import 变少, 这个检查器会静默失准)。用 importlib 显式表达"为副作用
#   而导入", 比 `# noqa` 好: noqa 是给 flake8 看的, pyflakes 不认, 会被当成垃圾 import
#   反复提醒, 下一个人"顺手清掉"就出事。
import importlib

importlib.import_module("core.actions")          # 注册动作(必须, 见上)
from core import registry as reg                 # noqa: E402
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


def iter_case_files(cases_dir, only=None):
    """Test_cases/ 下的用例文件路径。

    ★ 必须**递归进 APP 组目录**: 用例早先是平铺的 `Test_cases/*.yaml`, 现在按 APP 分组
      放成 `Test_cases/<组>/<用例>.yaml`。老写法只 listdir 顶层、再筛 .yaml —— 组目录
      一律被筛掉, 于是"扫描 0 个文件"却打印"全部兼容"(实测 2026-10-10 撞到: 一个
      只会说 OK 的检查器比没有更糟)。组目录判定与 GUI 的 _list_group_dirs 一致。
    """
    if only:
        if os.path.isfile(only):                 # 直接给了文件路径
            return [os.path.abspath(only)]
        want = os.path.basename(only)            # 只按文件名过滤(在组目录里找)
        return [p for p in _walk(cases_dir) if os.path.basename(p) == want]
    return _walk(cases_dir)


def _walk(cases_dir):
    out = []
    for base, dirs, names in os.walk(cases_dir):
        dirs[:] = [d for d in dirs if not d.startswith((".", "_"))]
        for n in names:
            if n.endswith((".yaml", ".yml")):
                out.append(os.path.join(base, n))
    return sorted(out)


def main(argv=None):
    # ★ 用 argparse 而不是裸 sys.argv: 至少 `--help` 得给出说明, 而不是被当成"要查的
    #   用例文件名"(那样会老老实实报"扫描 0 个文件", 让人以为没问题)
    import argparse
    ap = argparse.ArgumentParser(
        description="检查用例步骤与执行引擎的兼容性(多动作共存 / 参数形态)")
    ap.add_argument("case", nargs="?", default="",
                    help="只查某个用例(文件名或其路径); 省略则查 Test_cases/ 下全部")
    args = ap.parse_args(argv)
    only = args.case or None
    cases_dir = os.path.join(BASE_DIR, "Test_cases")
    files = iter_case_files(cases_dir, only)
    if only and not files:
        # ★ 指名要查的用例找不到 -> 必须报错退出, 不能"扫 0 个文件 + 全部兼容 + 返回 0"
        #   (那正是这个脚本刚犯过的毛病: 一个只会说 OK 的检查器比没有更糟)
        print(f"[ERROR] 没找到用例: {only}(它不在 Test_cases/ 下?)")
        return 1

    multi_action = 0
    type_issues = 0
    total_steps = 0
    top_steps = 0

    for path in files:
        try:
            fn = os.path.relpath(path, cases_dir)  # 显示成 <组>/<用例>.yaml, 便于定位
        except ValueError:                         # 不同盘符(传了别的盘的用例)时会抛
            fn = os.path.abspath(path)
        try:
            data = load_yaml_file(path)
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
