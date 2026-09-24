# -*- coding: utf-8 -*-
"""★真机入口: 扫 Test_cases/ 把每个 case 参数化执行。

这是唯一需要真机的测试文件,由 conftest 的安全阀保护 —— 不加
--mode real 时在收集阶段即被跳过。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.driver import (BASE_DIR, is_case_file, load_config,  # noqa: E402
                         load_yaml_file)
from core.runner import ActionRunner  # noqa: E402


def iter_modules(data, filename):
    """产出 (module_name, cases) —— 兼容两种文件格式

    单模块文件:  {module: X, cases: [...]}
    多模块汇总:  {modules: [{module: X, cases: [...]}, ...]}
                (汇总文件便于整体导出/评审,单独一个文件也能直接跑)
    """
    if isinstance(data.get("modules"), list):
        for m in data["modules"]:
            if isinstance(m, dict) and m.get("cases"):
                yield m.get("module") or filename, m["cases"]
        return
    if data.get("cases"):
        yield data.get("module") or os.path.splitext(filename)[0], data["cases"]


def iter_all_steps(steps, depth=0):
    """递归遍历步骤(含 else 子步骤),产出 (step, depth)

    else 分支里的子步骤同样是会执行的真实步骤 —— 统计/校验时只数顶层会漏掉
    它们。"快速建图" 顶层才 4 步,展开后是 9 步。
    """
    for s in steps or []:
        yield s, depth
        if isinstance(s.get("else"), list):
            yield from iter_all_steps(s["else"], depth + 1)


def count_steps(steps):
    """→ (顶层步数, 展开后总步数)"""
    all_steps = list(iter_all_steps(steps))
    return len(steps or []), len(all_steps)


def collect_cases(name_filter=""):
    """扫 Test_cases/ 下**组目录**(<APP名>/*.yaml)与根目录 → [(module, case, steps, priority, wait)]

    组目录 = APP 分组(如 涂鸦智能/);文件按顶层 case_order 升序(GUI 拖动
    箭头维护的执行顺序),没写的排末尾再按文件名 —— 与 GUI 列表顺序一致。
    """
    cases_dir = os.path.join(BASE_DIR, "Test_cases")
    out = []
    if not os.path.isdir(cases_dir):
        return out
    entries = []

    def _scan(scan_dir, group_name):
        for fn in sorted(os.listdir(scan_dir)):
            full = os.path.join(scan_dir, fn)
            if not is_case_file(full):      # 前置条件等非用例 yaml 不算用例
                continue
            if not os.path.isfile(full):
                continue
            try:
                data = load_yaml_file(full)
            except Exception as e:
                print(f"[跳过] {group_name}/{fn}: {e}")
                continue
            order = data.get("case_order") if isinstance(data, dict) else None
            if not isinstance(order, (int, float)):   # 缺失/非法一律按未排序处理
                order = None
            entries.append(((order is None, order or 0, fn), data, fn, full))

    # 组目录(APP 分组)优先,根目录散落文件兼容收集
    for sub in sorted(os.listdir(cases_dir)):
        sub_full = os.path.join(cases_dir, sub)
        if os.path.isdir(sub_full) and not sub.startswith((".", "_")):
            _scan(sub_full, sub)
    _scan(cases_dir, "")
    entries.sort(key=lambda e: e[0])
    for _, data, fn, full in entries:
        for module, cases in iter_modules(data, fn):
            for case in cases:
                name = case.get("name", "")
                if name_filter and name_filter not in (name + module):
                    continue
                out.append((module, name, case.get("steps") or [],
                            case.get("priority", "P1"), case.get("wait"), full))
    return out


def pytest_generate_tests(metafunc):
    """收集 YAML 用例;--case 通过 pytest 官方配置读取(兼容 --case xxx 与 --case=xxx)"""
    if "case_name" in metafunc.fixturenames:
        metafunc.parametrize(
            "module,case_name,steps,priority,case_wait,case_path",
            collect_cases(metafunc.config.getoption("--case")),
            ids=lambda v: str(v)[:30])


def test_yaml_case(device, report, module, case_name, steps, priority, case_wait, case_path):
    cfg = load_config()
    from core import vision as _vision
    _vision.set_template_app_group(os.path.basename(os.path.dirname(case_path)))
    runner = ActionRunner(device, cfg["runner"], case_wait=case_wait,
                          case_name=case_name,
                          device_name=cfg.get("target_device", ""))
    passed = runner.run_steps(steps)

    # 失败自动截图
    if not passed and cfg["runner"].get("screenshot_on_fail", True):
        fail_dir = os.path.join(BASE_DIR, "Test_img", "debug", "failures")
        os.makedirs(fail_dir, exist_ok=True)
        fail_path = os.path.join(fail_dir, f"{case_name}.png")
        device.screenshot(fail_path)

    # 记录每步到报告(直接从 runner.results 读取,包含 else 子步骤)
    for r in runner.results:
        report.set_step_desc(module, r.get("desc", ""))
        report.add_result(
            case_name=module,
            passed=r["passed"],
            error=r["error"],
            screenshot=r.get("screenshot", ""),
        )

    # 失败截图追加到报告
    if not passed and cfg["runner"].get("screenshot_on_fail", True):
        report.set_step_desc(module, "失败截图")
        report.add_result(module, False, error="用例执行失败,自动截图",
                          screenshot=os.path.join(BASE_DIR, "Test_img", "debug", "failures",
                                                  f"{case_name}.png"))

    assert passed, f"{case_name} 执行失败"
