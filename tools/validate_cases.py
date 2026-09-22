# -*- coding: utf-8 -*-
"""校验 Test_cases/*.yaml: 结构完整 + 每步动作键都在引擎注册表/schema 里。

    python tools/validate_cases.py

支持两种文件格式(与 test_yaml_runner.collect_cases 一致):
    单模块:  {module: X, cases: [...]}
    多模块汇总: {modules: [{module: X, cases: [...]}, ...]}
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.driver import BASE_DIR, load_yaml_file
from gui import schema
from tests.test_yaml_runner import iter_all_steps, iter_modules

KNOWN = {a["key"] for a in schema.ACTIONS}
# 通用字段也能单独成步(如纯截图/纯等待步骤),所以它们也算"有效键"
KNOWN |= {f["key"] for f in schema.GENERIC_FIELDS}
# 修饰参数: 可与主动作共存,本身不算动作键。
# 注意 screenshot **不在**此列 —— 它既是附带截图参数,也能单独作为主动作
# (独立的截图步骤)。算进 META 会让"只有 screenshot 的步骤"被误报为空步骤。
META = {"desc", "wait", "timeout", "retry", "threshold", "duration", "circular",
        "switch_area", "switch_tpl", "switch_label", "else", "wait_after"}


def check_case(fn, module, ci, case):
    """校验单个用例,返回问题数"""
    bad = 0
    name = f"{module}/{case.get('name') or f'第{ci + 1}个'}"
    if case.get("priority") not in ("P0", "P1"):
        print(f"[FAIL] {fn} 「{name}」: priority 必须是 P0/P1")
        bad += 1
    # 连同 else 子步骤一起校验 —— 它们同样是会执行的步骤
    for si, (step, depth) in enumerate(iter_all_steps(case.get("steps") or [])):
        where = f"步骤{si + 1}" + ("(else子步骤)" if depth else "")
        keys = set(step) - META
        if not (keys & KNOWN):
            if "wait" in step:
                # 纯等待步骤(只有 desc + wait)合法: 引擎跳过动作、只等待
                continue
            print(f"[FAIL] {fn} 「{name}」{where}: 无有效动作键 {sorted(keys)}")
            bad += 1
        for err in schema.validate_step(step):
            if "必填" in err:
                print(f"[FAIL] {fn} 「{name}」{where}: {err}")
                bad += 1
    return bad


def main():
    bad = 0
    cases_dir = os.path.join(BASE_DIR, "Test_cases")
    if not os.path.isdir(cases_dir):
        print(f"用例目录不存在: {cases_dir}")
        return 1
    # 组目录(APP 分组, 如 涂鸦智能/)内的用例 + 根目录散落用例, 都要校验
    files = []
    for sub in sorted(os.listdir(cases_dir)):
        sub_full = os.path.join(cases_dir, sub)
        if os.path.isdir(sub_full):
            for fn in sorted(os.listdir(sub_full)):
                if fn.endswith((".yaml", ".yml")):
                    files.append(os.path.join(sub, fn))
        elif sub.endswith((".yaml", ".yml")):
            files.append(sub)

    for fn in files:
        full = os.path.join(cases_dir, fn)
        try:
            data = load_yaml_file(full)
        except Exception as e:
            # 单个文件解析失败不能中断整轮校验 —— 否则修好一个才发现下一个
            print(f"[FAIL] {fn}: {e}")
            bad += 1
            continue
        if not isinstance(data, dict):
            print(f"[FAIL] {fn}: 顶层不是 mapping")
            bad += 1
            continue
        modules = list(iter_modules(data, fn))
        if not modules:
            print(f"[FAIL] {fn}: 缺少 module/cases 或 modules 结构")
            bad += 1
            continue
        for module, cases in modules:
            for ci, case in enumerate(cases):
                bad += check_case(fn, module, ci, case)

    print(f"检查 {len(files)} 个文件: " + ("OK" if bad == 0 else f"{bad} 个问题"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
