import os
import subprocess
import sys
import time

import pytest

from common.driver import BASE_DIR, load_config
from common.excel_report import ExcelReport
from common.logger import setup_logger
from common import session


# 初始化循环日志（5MB）
log = setup_logger(os.path.join(BASE_DIR, "reports", "test.log"))


def _clean_debug_dir():
    """每次运行前清空 debug 调试目录（避免历史调试产物越积越多）"""
    debug_dir = os.path.join(BASE_DIR, "reports", "debug")
    if os.path.isdir(debug_dir):
        for f in os.listdir(debug_dir):
            try:
                os.remove(os.path.join(debug_dir, f))
            except OSError:
                pass


# 模块加载时清空 debug 目录
_clean_debug_dir()


def pytest_addoption(parser):
    parser.addoption("--device", default="auto", help="设备ID或 auto")
    parser.addoption("--mode", default="mock", choices=["mock", "real"],
                     help="mock=Mock服务, real=真实设备")
    parser.addoption("--case", default="", help="指定用例名（模糊匹配），空=全部")


@pytest.fixture(scope="function")
def device(request, report):
    """每个用例前后重启 APP，进入设备页，确保充电且电量 >50%(流程见 common/session.py)"""
    import uiautomator2 as u2
    cfg = load_config()

    device_id = session.get_device_id(cfg, request.config.getoption("--device"))
    d = u2.connect(device_id)
    d.implicitly_wait(10)

    # 报告填入真实设备信息（SN / App 版本），失败不影响执行
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


@pytest.fixture(scope="session")
def report():
    r = ExcelReport()
    yield r
    r.save()
    print(f"\n 报告已生成: {r.path}")


@pytest.fixture(scope="session", autouse=True)
def mock_server(request):
    if request.config.getoption("--mode") == "mock":
        script = os.path.join(BASE_DIR, "mock_server", "server.py")
        proc = subprocess.Popen([sys.executable, script], cwd=BASE_DIR)
        time.sleep(2)
        yield
        if proc.poll() is None:
            proc.terminate()
    else:
        yield
