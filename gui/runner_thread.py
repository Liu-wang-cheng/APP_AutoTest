"""GUI 执行线程: 后台连接设备/跑前置/执行用例,信号推送进度

线程内不碰任何控件,统一走 Qt 信号跨线程刷新界面。
"""
import logging
import os

import yaml
from PySide6.QtCore import QThread, Signal

from core import session
from core.runner import ActionRunner, UserStopped
from core.driver import BASE_DIR, load_config
from core.excel_report import ExcelReport
from core.logger import get_logger

log = get_logger()

# 前置检查项 → 执行结果/报告里的步骤描述
_PRE_LABELS = {"restart": "前置-重启APP",
               "charging": "前置-等待充电",
               "map_load": "前置-地图加载",
               "battery": "前置-电量≥50%"}


def pre_step_desc(key, batt=-1):
    """前置行描述;电量类(等待充电/电量门槛)附带机器实际电量"""
    desc = _PRE_LABELS[key]
    if key in ("battery", "charging") and batt >= 0:
        desc += f"(当前电量 {batt}%)"
    return desc


def resolve_group_preconditions(pre_by_group, group, fallback):
    """按 APP 组取该组的前置项。

    ★ 必须按"键是否存在"判断, 不能用 `pre_by_group.get(group) or fallback`:
      空列表是 falsy, 会把「该组前置**全部取消勾选**」(= 这组有意不做前置)误判成
      「没配过」, 于是回退去执行 fallback(= 当前界面组的前置)。
      实测: ZZZ 组界面显示 0 条前置, 实际跑了 1 条(可能是重启 APP / 等充电这类重动作)。
    """
    return pre_by_group[group] if group in (pre_by_group or {}) else fallback


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
    case_started = Signal(str, str)   # 单条用例开始(文件路径, 用例名)→ 用例列表状态灯
    case_finished = Signal(str, str, bool)  # 单条用例结束(路径, 用例名, 是否通过)
    precondition_failed = Signal(str)  # 前置检查未通过(阻断详情)→ GUI 弹窗提醒

    def __init__(self, device_id, case_files, preconditions, repeat=1, parent=None,
                 pre_by_group=None, device_name=""):
        super().__init__(parent)
        self.device_id = device_id
        self.case_files = case_files       # list: yaml 路径
        # 前置项列表 [{type, enabled, ...参数}](旧版传 dict 时自动转换, 兼容)
        if isinstance(preconditions, dict):
            preconditions = [{"type": k, "enabled": bool(v)}
                             for k, v in preconditions.items()]
        self.pre_items = preconditions
        # ★ {APP组: [前置项]} —— 切到某组时执行那组的前置(用户要求: 前置与 APP 组绑定);
        #   没给的组回退 self.pre_items
        self.pre_by_group = pre_by_group or {}
        self.repeat = max(1, int(repeat))  # 整个队列重复的轮数
        self.runner = None
        self._stop_requested = False

    def request_stop(self):
        """界面停止按钮调用: 置位停止标志,前置等待与当前步骤尽快退出"""
        self._stop_requested = True
        if self.runner:
            self.runner.stop()

    def _emit_pre_result(self, d, module, report, r):
        """单条前置结果 → 执行结果表 + 报告(带该项自己的耗时)。

        ★ 用户要求: 前置也要**一条一条**显示结果和耗时, 不要全部执行完再刷出来;
          充电/电量行附带机器当前电量。
        """
        desc = "前置-" + r["desc"]
        if "电量" in desc or "充电" in desc:
            try:
                batt = session.get_battery_level(d)
            except Exception:
                batt = -1
            if batt >= 0:
                desc += f"(当前电量 {batt}%)"
        err = "" if r["ok"] else "前置未通过(超时或失败)"
        # ★ 报告写入失败不能拖垮执行: 组名(module)里出现非法字符曾让 create_sheet
        #   抛错 → 冒到 run() 兜底 except → 整轮以"执行异常"中断
        try:
            report.set_step_desc(module, desc)
            report.add_result(module, r["ok"], err, "")
        except Exception as e:
            log.warning(f"[report] 前置结果写入报告失败(忽略, 不影响执行): {e}")
        self.step_done.emit({"desc": desc, "passed": r["ok"], "error": err,
                             "screenshot": "", "elapsed": r.get("elapsed")})

    # ── 线程主体 ──
    def run(self):
        # ★ 不再自己挂日志 handler —— MainWindow 已有全局转发(_attach_log_handler),
        #   两边都挂会让同一条日志在运行日志里显示两遍。log_line 信号保留兼容。
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

            # 展开任务队列: 文件 → 用例
            tasks = []                     # (module, case, file)
            for fp in self.case_files:
                with open(fp, encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                module = data.get("module") or os.path.splitext(os.path.basename(fp))[0]
                for case in data.get("cases") or []:
                    tasks.append((module, case, fp))
            if not tasks:
                self.finished_run.emit(False, "用例文件中没有 cases")
                return

            report = ExcelReport()
            all_passed = True
            total_rounds = self.repeat
            prepared_group = None       # 已经做过前置的 APP 组
            for rnd in range(total_rounds):
                for module, case, fp in tasks:
                    if self._stop_requested:
                        break
                    case_name = case.get("name", "未命名用例")
                    steps = case.get("steps") or []
                    # 用例所在 APP 组 = 用例文件所在目录名
                    group = os.path.basename(
                        os.path.dirname(os.path.abspath(fp)))

                    # ★ 前置: 切到某组时按**该组**的配置全量执行一次(用户要求:
                    #   前置条件与 APP 组绑定); 同组后续用例只重启 APP。
                    if group != prepared_group:
                        items = resolve_group_preconditions(
                            self.pre_by_group, group, self.pre_items)
                        self.status.emit(
                            f"前置准备[{group or '默认组'}]({case_name})...")
                        pre_results = session.prepare_items(
                            d, cfg, items,
                            on_progress=self.status.emit,
                            should_cancel=lambda: self._stop_requested,
                            # ★ 每完成一条立刻落表(用户要求: 一条一条显示结果和耗时)
                            on_item_done=lambda r, m=module:
                            self._emit_pre_result(d, m, report, r))
                        prepared_group = group
                        # ★ 任一前置项未通过 → 阻断:本轮不再执行任何用例(用户要求)
                        failed = [r["desc"] for r in pre_results if not r["ok"]]
                        if failed:
                            detail = "、".join(failed)
                            self.log_line.emit(f"[前置] 未通过,阻断本轮执行: {detail}")
                            self.precondition_failed.emit(detail)
                            self.finished_run.emit(False, f"前置检查未通过: {detail}")
                            return
                    else:
                        self.status.emit(f"重启 APP({case_name})...")
                        session.restart_app(d, cfg)
                        desc = pre_step_desc("restart")
                        report.set_step_desc(module, desc)
                        report.add_result(module, True, "", "")
                        self.step_done.emit({"desc": desc, "passed": True,
                                             "error": "", "screenshot": ""})
                    if self._stop_requested:
                        break

                    self.status.emit(f"[第{rnd + 1}/{total_rounds}轮] 执行用例: {case_name}")
                    self.case_started.emit(fp, case_name)   # 用例列表状态灯 → 黄
                    from core import vision as _vision
                    _vision.set_template_app_group(
                        os.path.basename(os.path.dirname(fp)))   # 模板按 APP 组子目录
                    runner = ActionRunner(d, cfg["runner"],
                                          case_wait=case.get("wait"), case_name=case_name,
                                          device_name=cfg.get("target_device", ""))
                    # 包装结果回调: 计算每步耗时(相对上一步完成时刻)
                    import time as _time
                    last_ts = {"t": _time.time()}

                    def on_result(r):
                        now = _time.time()
                        r2 = dict(r)
                        r2["elapsed"] = round(now - last_ts["t"], 1)
                        last_ts["t"] = now
                        self.step_done.emit(r2)

                    runner.on_result = on_result
                    self.runner = runner
                    passed = runner.run_steps(steps)
                    self.runner = None

                    # 与 pytest 相同结构写入 Excel 报告
                    for r2 in runner.results:
                        try:
                            report.set_step_desc(module, r2.get("desc", ""))
                            report.add_result(module, r2["passed"], r2.get("error", ""),
                                              r2.get("screenshot", ""))
                        except Exception as e:
                            # 同上: 报告写入问题不该让整轮跑不下去
                            log.warning(f"[report] 步骤结果写入报告失败(忽略): {e}")

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
                            self.case_finished.emit(fp, case_name, False)   # 状态灯 → 红
                    else:
                        self.status.emit(f"用例通过: {case_name}")
                        self.case_finished.emit(fp, case_name, True)   # 状态灯 → 绿

            report.save()
            report_saved = True
            self.status.emit(f"报告已生成: {report.path}")
            if self._stop_requested:
                self.finished_run.emit(False, "已手动停止")
            elif all_passed:
                self.finished_run.emit(True, f"全部通过({total_rounds} 轮 × {len(tasks)} 个用例)")
            else:
                self.finished_run.emit(False, "存在失败步骤")
        except UserStopped:
            self.finished_run.emit(False, "已手动停止")
        except Exception as e:
            self.log_line.emit(f"[异常] {type(e).__name__}: {e}")
            self.finished_run.emit(False, f"执行异常: {e}")
        finally:
            # 中途异常也保住已执行部分的报告
            if report is not None and not report_saved:
                try:
                    report.save()
                    self.status.emit(f"报告已生成(部分执行): {report.path}")
                except Exception:
                    pass
