import pytest
import yaml
import os
from common.action_runner import ActionRunner
from common.driver import load_config

CASES_DIR = "Test_cases"


def collect_cases():
    # 支持 --case 参数过滤
    import sys
    case_filter = ""
    for i, arg in enumerate(sys.argv):
        if arg == "--case" and i + 1 < len(sys.argv):
            case_filter = sys.argv[i + 1]
            break

    cases = []
    for fname in sorted(os.listdir(CASES_DIR)):
        if not fname.endswith(".yaml"):
            continue
        if case_filter and case_filter not in fname:
            continue
        with open(os.path.join(CASES_DIR, fname), encoding="utf-8") as f:
            data = yaml.safe_load(f)
        for case in data.get("cases", []):
            cases.append(pytest.param(
                data["module"],     # 用例组名（sheet 标题）
                case["name"],
                case["steps"],
                case.get("priority", "P1"),
                case.get("wait"),   # 用例级等待（覆盖全局）
                id=f"{data['module']}::{case['name']}"
            ))
    return cases


def _extract_screenshot(step):
    if isinstance(step, dict):
        return step.get("screenshot", "")
    return ""


def _extract_desc(step):
    if isinstance(step, dict):
        return step.get("desc", "")
    return ""


@pytest.mark.parametrize("module,case_name,steps,priority,case_wait", collect_cases())
def test_yaml_case(device, report, module, case_name, steps, priority, case_wait):
    cfg = load_config()
    runner = ActionRunner(device, cfg["runner"], case_wait=case_wait)
    passed = runner.run_steps(steps)

    # 失败自动截图
    if not passed and cfg["runner"].get("screenshot_on_fail", True):
        os.makedirs("reports/failures", exist_ok=True)
        fail_path = f"reports/failures/{case_name}.png"
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
        report.add_result(module, False, error="用例执行失败，自动截图")

    assert passed, f"{case_name} 执行失败"
