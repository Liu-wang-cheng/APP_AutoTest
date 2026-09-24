# -*- coding: utf-8 -*-
"""测试隔离:任何测试都只能写「临时副本」,绝不碰用户真实的 config/config.yaml。

★ 事故(2026-09-24)
    `test_history_add_keeps_popup_full_height` 调用 `_save_env_field_of`(它会经
    `update_config` 写盘), 但只 mock 了 `gui.main_window.CONFIG_PATH` —— 而
    `update_config` 的**默认落盘位置**是 `core.driver.CONFIG_PATH`,两者不是一回事,
    于是每跑一次测试就把 `target_device = 又一个新的名字` 写进用户真实的配置文件。
    用户看到的现象是「设备名明明删掉了, 下次启动又出现」——因为他在 GUI 里删掉之后,
    我又跑了一次测试把它写了回去。铁证: config.yaml 的 mtime 与日志里那行
    「配置已保存: target_device = ...」精确到同一毫秒。

★ 隔离方式
    把 `core.driver.CONFIG_PATH` 指向**真实配置的临时副本**:读取依旧真实
    (load_config / load_preconditions 行为完全不变), 而所有写入(update_config /
    save_preconditions 的默认路径)只落在副本里。这样即使将来有人漏 mock, 也**不可能**
    改到真实文件 —— 结构性保证, 而不是靠每个测试自觉。

★ 同源问题:pytest 与应用**共用**同一个 logger 与同一个日志文件
    (`reports/test.log`), 跑测试会把测试行灌进用户的运行日志, 连带着让日志分析失真
    (排查上面的 config 污染时就被带偏过)。同理由 `_isolate_log_file` 把文件 handler
    换到临时文件。

    QSettings(历史记录)不在此处理: 相关测试统一用 `fake_settings` 内存替身,
    且 tests/test_gui.py 里已有「测试前后真实历史必须一致」的验证。
"""
import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _isolate_config_file(monkeypatch, tmp_path):
    """每个测试:CONFIG_PATH → 真实 config.yaml 的临时副本(读真实 / 写副本)"""
    import core.driver as driver

    real = driver.CONFIG_PATH
    sandbox = tmp_path / "config.yaml"
    if os.path.isfile(real):
        shutil.copy2(real, sandbox)      # 字节级复制, CRLF 原样保留
    else:
        sandbox.write_text("", encoding="utf-8")
    monkeypatch.setattr(driver, "CONFIG_PATH", str(sandbox), raising=False)
    yield


@pytest.fixture(scope="session", autouse=True)
def _isolate_log_file(tmp_path_factory):
    """测试期日志写到临时文件, 不污染用户的 reports/test.log。

    logger 是全局单例("vacuum_test"), `setup_logger` 只应在应用启动时调用一次;
    这里把它的文件 handler 换成指向临时文件的(真实文件即使已被打开也会被关闭),
    所以无论 logger 何时被初始化, 测试都不可能再往用户的日志里写。
    """
    import logging

    from core import logger as core_logger

    log = core_logger.get_logger()          # 未初始化则先按默认路径初始化
    for h in list(log.handlers):
        if isinstance(h, core_logger.RotatingLogHandler):
            log.removeHandler(h)
            h.close()                       # 释放对 reports/test.log 的句柄
    sandbox = tmp_path_factory.mktemp("logs") / "test.log"
    fh = core_logger.RotatingLogHandler(str(sandbox))
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(fh)
    yield
