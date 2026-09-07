"""GUI 执行线程: 后台连接设备/跑前置/执行用例,信号推送进度

线程内不碰任何控件,统一走 Qt 信号跨线程刷新界面。
"""
import logging
import os

import yaml
from PySide6.QtCore import QThread, Signal

from common import session
from common.action_runner import ActionRunner, UserStopped
from common.driver import BASE_DIR, load_config
from common.excel_report import ExcelReport


class _QtLogHandler(logging.Handler):
    """把 vacuum_test 日志流转发到 Qt 信号(跨线程自动排队)"""

    def __init__(self, emit_fn):
        super().__init__(logging.INFO)
        self.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
        self._emit_fn = emit_fn

    def emit(self, record):
        try:
            self._emit_fn(self.format(record))
        except Exception:
            pass


class RunWorker(QThread):
    step_done = Signal(dict)          # 一条步骤结果 {"desc","passed","error","screenshot"}
    log_line = Signal(str)
    status = Signal(str)              # 阶段状态(连接设备/前置准备/执行用例...)
    finished_run = Signal(bool, str)  # 结束(是否全部通过, 摘要消息)

    def __init__(self, device_id, case_path, preconditions, parent=None):
        super().__init__(parent)
        self.device_id = device_id
        self.case_path = case_path
        self.pre = preconditions  # dict: restart/charging/map_load/battery
        self.runner = None
        self._stop_requested = False

    def request_stop(self):
        """界面停止按钮调用: 置位停止标志,前置等待与当前步骤尽快退出"""
        self._stop_requested = True
        if self.runner:
            self.runner.stop()

    # ── 线程主体 ──
    def run(self):
        handler = _QtLogHandler(self.log_line.emit)
        root_log = logging.getLogger("vacuum_test")
        root_log.addHandler(handler)
        report = None
        report_saved = False
        try:
            cfg = load_config()

            self.status.emit("连接设备...")
            import uiautomator2 as u2
            d = u2.connect(self.device_id)
            d.implicitly_wait(10)
            if self._stop_requested:
                self.finished_run.emit(False, "已停止")
                return

            with open(self.case_path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            module = data.get("module") or os.path.splitext(os.path.basename(self.case_path))[0]
            cases = data.get("cases") or []
            if not cases:
                self.finished_run.emit(False, "用例文件中没有 cases")
                return

            report = ExcelReport()
            all_passed = True
            first = True
            for case in cases:
                if self._stop_requested:
                    break
                case_name = case.get("name", "未命名用例")
                steps = case.get("steps") or []

                # 前置: 首个用例按勾选项全量执行,后续用例只重启 APP
                if first:
                    self.status.emit(f"前置准备({case_name})...")
                    session.prepare(d, cfg, should_cancel=lambda: self._stop_requested,
                                    **self.pre)
                    first = False
                else:
                    self.status.emit(f"重启 APP({case_name})...")
                    session.restart_app(d, cfg)
                if self._stop_requested:
                    break

                self.status.emit(f"执行用例: {case_name}")
                runner = ActionRunner(d, cfg["runner"],
                                      case_wait=case.get("wait"), case_name=case_name)
                runner.on_result = lambda r: self.step_done.emit(dict(r))
                self.runner = runner
                passed = runner.run_steps(steps)
                self.runner = None

                # 与 pytest 相同结构写入 Excel 报告
                for r in runner.results:
                    report.set_step_desc(module, r.get("desc", ""))
                    report.add_result(module, r["passed"], r.get("error", ""),
                                      r.get("screenshot", ""))

                if not passed:
                    all_passed = False
                    if not self._stop_requested:
                        # 用例级失败截图
                        fail_dir = os.path.join(BASE_DIR, "reports", "failures")
                        os.makedirs(fail_dir, exist_ok=True)
                        try:
                            d.screenshot(os.path.join(fail_dir, f"{case_name}.png"))
                        except Exception:
                            pass
                        self.status.emit(f"用例失败: {case_name}")
                    if self._stop_requested:
                        break
                else:
                    self.status.emit(f"用例通过: {case_name}")

            report.save()
            report_saved = True
            self.status.emit(f"报告已生成: {report.path}")
            if self._stop_requested:
                self.finished_run.emit(False, "已手动停止")
            elif all_passed:
                self.finished_run.emit(True, f"全部通过({len(cases)} 个用例)")
            else:
                self.finished_run.emit(False, "存在失败步骤")
        except UserStopped:
            self.finished_run.emit(False, "已手动停止")
        except Exception as e:
            self.log_line.emit(f"[异常] {type(e).__name__}: {e}")
            self.finished_run.emit(False, f"执行异常: {e}")
        finally:
            root_log.removeHandler(handler)
            # 中途异常也保住已执行部分的报告
            if report is not None and not report_saved:
                try:
                    report.save()
                    self.status.emit(f"报告已生成(部分执行): {report.path}")
                except Exception:
                    pass
