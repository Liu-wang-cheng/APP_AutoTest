# -*- coding: utf-8 -*-
"""pytest 全局配置与 fixture。

★安全阀: --mode 默认 mock,真机用例在「收集阶段」就被跳过,防止裸跑
pytest tests/ 误操控真实扫地机。必须显式 --mode real 才会执行。
"""
import os
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import app_detect, session  # noqa: E402
from core.driver import BASE_DIR, load_config  # noqa: E402
from core.excel_report import ExcelReport  # noqa: E402


# ★ 这里曾有 `_clean_debug_dir()`: 在模块加载时(收集阶段)清空 Test_img/debug/,
#   本意是"残留旧图会被误认为本轮结果"。但它删的是**用户真实目录**里的产物 ——
#   实测: 只跑一个与地图无关的 tests/test_ocr_fallback.py, 真机刚生成的
#   map_zone.png 等诊断图就被删掉了。
#   现在测试产物的隔离已由 tests/conftest.py::_isolate_base_dir 从**写入端**解决
#   (map_ops/trace/vision 等的 BASE_DIR 逐个钉到临时目录), 测试不再往仓库写图,
#   因此无需再删真实目录; 真机运行留下的诊断图由用户自己决定何时清理。


def pytest_addoption(parser):
    parser.addoption("--device", default="auto", help="设备ID或 auto")
    parser.addoption("--mode", default="mock", choices=["mock", "real"],
                     help="mock=Mock服务, real=真实设备")
    parser.addoption("--case", default="", help="指定用例名(模糊匹配),空=全部")
    # ★ 按 APP 组目录筛选(2026-10-16): 不同组可能有**同名用例**
    #   (如 三星/全局清扫 与 涂鸦智能T4/全局清扫完整流程 都含"全局清扫") ——
    #   只给 --case 会一次跑两条。真机冒烟要精确挑一条时用 --group 三星。
    parser.addoption("--group", default="", help="指定 APP 组目录名(精确),空=全部")


def pytest_collection_modifyitems(config, items):
    """安全阀: 真机用例必须显式 --mode real 才会执行

    在收集阶段跳过,device 前置 fixture 完全不会启动 —— 防止裸跑
    pytest tests/ 时误操控扫地机。
    """
    if config.getoption("--mode") == "real":
        return
    skip_real = pytest.mark.skip(reason="真机用例: 需加 --mode real 才执行(防止误操控扫地机)")
    for item in items:
        # 按**文件名**精确匹配。用 `"test_yaml_runner" in item.nodeid` 做子串匹配
        # 会连名字里含该串的其他测试一起跳过 —— 比如本文件的安全阀守护测试
        # test_yaml_runner_skipped_without_real_mode,结果安全阀自己没被验证。
        if item.path.name == "test_yaml_runner.py":
            item.add_marker(skip_real)


@pytest.fixture(scope="session")
def report():
    r = ExcelReport()
    yield r
    r.save()
    print(f"\n 报告已生成: {r.path}")


@pytest.fixture(scope="session", autouse=True)
def mock_server(request):
    """mock 模式下拉起假后端。

    只在本次会话真的收集到真机用例时才启动 —— 跑单元测试时没必要占着 5000 端口
    白等 2 秒。
    """
    if request.config.getoption("--mode") != "mock":
        yield
        return
    if not any("test_yaml_runner" in item.nodeid for item in request.session.items):
        yield
        return
    script = os.path.join(BASE_DIR, "mock_server", "server.py")
    proc = subprocess.Popen([sys.executable, script], cwd=BASE_DIR)
    time.sleep(2)
    yield
    if proc.poll() is None:
        proc.terminate()


@pytest.fixture(scope="function")
def device(request, report):
    """每个用例前后重启 APP,进入设备页,确保充电且电量 >50%(见 core/session.py)"""
    import uiautomator2 as u2
    cfg = load_config()

    # 模拟器常需先 adb connect 才会出现在 adb 列表里(配置里的 host:port 地址),
    # 否则 get_device_id 解析出的地址连不上
    app_detect.ensure_connected(cfg)

    device_id = session.get_device_id(cfg, request.config.getoption("--device"))
    d = u2.connect(device_id)
    d.implicitly_wait(10)

    # 报告填入真实设备信息(SN / App 版本),失败不影响执行
    try:
        report.set_device_info(
            sn=getattr(d, "serial", "") or "",
            app_version=d.app_version(cfg["app"]["package"]),
        )
    except Exception:
        pass

    # 前置: 跑**该用例所属 APP 组的前置条件**(与 GUI 完全同一套)
    #
    # ★ 为什么不再用写死的 session.prepare()(用户 2026-10-19 实测):
    #   prepare() 只有 重启APP/充电/地图/电量, **没有组里配的 enter_device 与自定义步骤**
    #   (如「结束烘干拖布」)。于是真机冒烟在"APP 停在设备列表 / 机器人还在烘干"时会一路跑偏:
    #   没进设备页 → 地图加载失败 → 用例在列表页上瞎点, 而且**照样报通过/失败看起来像用例问题**。
    #   现在与 GUI 同源: Test_preconditions/<组>.yaml → prepare_items(), 前置不过就**直接判红**
    #   (带是哪一项、什么原因), 不再往下跑用例。
    #
    # 设备名可被环境变量覆盖: 冒烟 workflow 传 SMOKE_DEVICE_NAME(来自仓库变量),
    # 免得设备改名后要去改仓库里的配置文件(实测 APP 列表里可能同时挂着好几台)。
    if os.environ.get("SMOKE_DEVICE_NAME"):
        cfg["target_device"] = os.environ["SMOKE_DEVICE_NAME"]

    case_path = ""
    try:
        case_path = str(request.node.callspec.params.get("case_path") or "")
    except Exception:
        pass
    group = (os.path.basename(os.path.dirname(case_path)) if case_path else "") \
        or request.config.getoption("--group") or ""
    if group:
        items, src = session.preconditions_for_group(group)
        print(f"[前置] 按组「{group}」的配置({src})执行 {len(items)} 项")
        results = session.prepare_items(d, cfg, items)
        failed = [
            r["desc"] + (f"({r['reason']})" if r.get("reason") else "")
            for r in results if not r["ok"] and not r.get("skipped")
        ]
        if failed:
            pytest.fail("前置未通过(按组「%s」的配置): %s" % (group, "、".join(failed)),
                        pytrace=False)
    else:
        # 兜底: 拿不到组信息(极少数直接用了 device fixture 的场景) → 用旧的内置前置
        session.prepare(d, cfg)

    yield d

    d.app_stop(cfg["app"]["package"])
