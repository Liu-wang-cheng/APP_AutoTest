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


def _clean_debug_dir():
    """清空 reports/debug —— room_zones 会往这里写标注图,历史产物越积越多,
    残留的旧图会让人误以为是本轮结果。"""
    debug_dir = os.path.join(BASE_DIR, "reports", "debug")
    if not os.path.isdir(debug_dir):
        return
    for f in os.listdir(debug_dir):
        try:
            os.remove(os.path.join(debug_dir, f))
        except OSError:
            pass


_clean_debug_dir()   # 模块加载时清一次(收集阶段就执行)


def pytest_addoption(parser):
    parser.addoption("--device", default="auto", help="设备ID或 auto")
    parser.addoption("--mode", default="mock", choices=["mock", "real"],
                     help="mock=Mock服务, real=真实设备")
    parser.addoption("--case", default="", help="指定用例名(模糊匹配),空=全部")


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

    # 前置: 重启APP + 进入设备页 + 充电 + 地图加载 + 电量达标
    session.prepare(d, cfg)

    yield d

    d.app_stop(cfg["app"]["package"])
