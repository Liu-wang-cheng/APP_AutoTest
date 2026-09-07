import os

import pytest
import yaml

from common.action_runner import ActionRunner
from common.driver import BASE_DIR, load_config

CASES_DIR = os.path.join(BASE_DIR, "Test_cases")


def collect_cases(case_filter=""):
    """扫描 Test_cases 目录,收集所有 YAML 用例(支持 --case 模糊过滤)"""
    cases = []
    for fname in sorted(os.listdir(CASES_DIR)):
        if not fname.endswith(".yaml"):
            continue
        if case_filter and case_filter not in fname:
            continue
        with open(os.path.join(CASES_DIR, fname), encoding="utf-8") as f:
            data = yaml.safe_load(f)
        for case in data.get("cases", []):
            priority = case.get("priority", "P1")
            # P0 用例映射 smoke 标记,可 -m smoke 单独跑
            marks = [pytest.mark.smoke] if priority == "P0" else []
            cases.append(pytest.param(
                data["module"],     # 用例组名（sheet 标题）
                case["name"],
                case["steps"],
                priority,
                case.get("wait"),   # 用例级等待（覆盖全局）
                marks=marks,
                id=f"{data['module']}::{case['name']}"
            ))
    return cases


def pytest_generate_tests(metafunc):
    """收集 YAML 用例;--case 通过 pytest 官方配置读取(兼容 --case xxx 与 --case=xxx)"""
    if "case_name" in metafunc.fixturenames:
        metafunc.parametrize(
            "module,case_name,steps,priority,case_wait",
            collect_cases(metafunc.config.getoption("--case")))


def test_yaml_case(device, report, module, case_name, steps, priority, case_wait):
    cfg = load_config()
    runner = ActionRunner(device, cfg["runner"], case_wait=case_wait, case_name=case_name)
    passed = runner.run_steps(steps)

    # 失败自动截图
    if not passed and cfg["runner"].get("screenshot_on_fail", True):
        fail_dir = os.path.join(BASE_DIR, "reports", "failures")
        os.makedirs(fail_dir, exist_ok=True)
        fail_path = os.path.join(fail_dir, f"{case_name}.png")
        device.screenshot(fail_path)

    # 记录每步到报告（直接从 runner.results 读取，包含 else 子步骤）
    for r in runner.results:
        desc = r.get("desc", "")
        report.set_step_desc(module, desc)
        report.add_result(
            case_name=module,
            passed=r["passed"],
            error=r["error"],
            screenshot=r.get("screenshot", ""),
        )

    # 失败截图追加到报告
    if not passed and cfg["runner"].get("screenshot_on_fail", True):
        report.set_step_desc(module, "失败截图")
        report.add_result(module, False, error="用例执行失败，自动截图",
                          screenshot=os.path.join(BASE_DIR, "reports", "failures", f"{case_name}.png"))

    assert passed, f"{case_name} 执行失败"
