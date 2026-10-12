"""GUI 执行线程: 后台连接设备/跑前置/执行用例,信号推送进度

线程内不碰任何控件,统一走 Qt 信号跨线程刷新界面。
"""
import logging
import os
import re

import yaml
from PySide6.QtCore import QThread, Signal

from core import session
from core.runner import ActionRunner, UserStopped
from core.driver import BASE_DIR, load_config
from core.excel_report import ExcelReport, case_sheet_name
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


def _shot_dir_name(case_key, run_no):
    """截图子目录名: 「用例键-第N次」—— bootstrap 的封顶清理(trim/prune)按这个名解析。

    ★ 以前直接用报告表名(「用例名-N」)当目录名, 与清理代码认的「用例名-第N次」
      对不上, 逐用例封顶静默失效(审查 2026-10-12 抓到)。顺带洗掉文件系统非法字符。
    """
    return re.sub(r'[\\/:*?"<>|]+', "_", f"{case_key}-第{run_no}次")


class RunWorker(QThread):
    step_done = Signal(dict)          # 一条步骤结果 {"desc","passed","error","screenshot"}
    #: 步骤**开始** → GUI 先插一行「执行中」, 跑完(step_done)再就地改成 PASS/FAIL
    step_started = Signal(dict)
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
        # ★ {APP组: [前置项]} —— **每个用例执行前**都跑它所在组的那套前置(2026-10-11 用户
        #   要求"每次执行都要跑前置"); 组没给配置时回退 self.pre_items
        self.pre_by_group = pre_by_group or {}
        self.repeat = max(1, int(repeat))  # 整个队列重复的轮数
        #: 当前这一"次执行"的标签(用例名(第几次)) —— 结果表/报告里要能看出跑的是第几轮。
        #  ★ 必须在这里初始化: 前置跑在用例之前, 那时循环里还没设过它。
        self._exec_label = ""
        self.runner = None
        self._stop_requested = False

    def request_stop(self):
        """界面停止按钮调用: 置位停止标志,前置等待与当前步骤尽快退出"""
        self._stop_requested = True
        if self.runner:
            self.runner.stop()

    def _emit_step(self, payload):
        """发一条结果到执行结果表 —— 统一盖上"用例名(第几次)", 用 户才看得出这是第几轮"""
        p = dict(payload)
        if self._exec_label:
            p.setdefault("case", self._exec_label)
        self.step_done.emit(p)

    def _emit_pre_result(self, d, sheet, report, r, shot_dir=None):
        """单条前置结果 → 执行结果表 + 报告(带该项自己的耗时)。

        shot_dir: 前置截图放的目录名(screenshots/ 下) —— 缺省退回表名(旧调用)。

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
        sub = r.get("sub") or []
        shot = ""
        if not sub:
            # ★ 前置也默认截图(用户要求 2026-10-12), 放进报告 —— 只有自定义步骤型前置
            #   不带这里(它内部每一步自己有截图, 见下)
            shot = self._pre_screenshot(d, shot_dir or sheet, desc)
        # ★ 报告写入失败不能拖垮执行: 组名(module)里出现非法字符曾让 create_sheet
        #   抛错 → 冒到 run() 兜底 except → 整轮以"执行异常"中断
        try:
            report.set_step_desc(sheet, desc)
            report.add_result(sheet, r["ok"], err, shot)
        except Exception as e:
            log.warning(f"[report] 前置结果写入报告失败(忽略, 不影响执行): {e}")
        self._emit_step({"desc": desc, "passed": r["ok"], "error": err,
                         "screenshot": shot, "elapsed": r.get("elapsed")})
        # 自定义步骤型前置: 内部每一步也各占一行(带自己的截图与耗时)
        for i, sr in enumerate(sub):
            sdesc = f"{desc}: {sr.get('desc', '')}"
            sshot = sr.get("screenshot", "") or ""
            serr = sr.get("error", "") if not sr.get("passed") else ""
            try:
                report.set_step_desc(sheet, sdesc)
                report.add_result(sheet, bool(sr.get("passed")), serr, sshot)
            except Exception as e:
                log.warning(f"[report] 前置子步骤写入报告失败(忽略): {e}")
            self._emit_step({"desc": sdesc, "passed": bool(sr.get("passed")),
                             "error": serr, "screenshot": sshot,
                             "elapsed": sr.get("elapsed")})

    def _pre_screenshot(self, d, sheet, desc):
        """给一条前置项截一张图(尽力而为, 失败只告警) —— 返回路径或空串"""
        try:
            from core import vision as _vision
            bad = set("\\/:*?\"<>|")
            safe = "".join(c for c in str(desc) if c not in bad)[:40]
            path = os.path.join(BASE_DIR, "Test_cases",
                                _vision.current_app_group() or "未分组", "screenshots",
                                str(sheet), f"前置_{safe}.png")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            d.screenshot(path)
            return path
        except Exception as e:
            log.warning(f"[前置] 截图失败(忽略): {type(e).__name__}: {e}")
            return ""

    # ── 线程主体 ──
    def run(self):
        # ★ 不再自己挂日志 handler —— MainWindow 已有全局转发(_attach_log_handler),
        #   两边都挂会让同一条日志在运行日志里显示两遍。log_line 信号保留兼容。
        # ★ 「空闲闸门」: 从这里到 finally 之间算"正在执行用例" —— 封顶清理/更新残渣
        #   清理会推迟到本轮结束再做(用户要求 2026-10-12: 减少执行期间的内存占用,
        #   见 core/idle.py)。begin/end 必须成对, 所以 end 放在 finally 里 ——
        #   少放一次, 清理就永久不做了。
        from core import idle
        # ★ 计数令牌(审查 2026-10-12): end_run 带上自己那次 begin 的令牌 —— 万一将来
        #   出现"上一轮的收尾还没跑完、新一轮已经开始"的重叠, 迟到的 end 不会把
        #   新一轮的计数清掉、误触封顶清理。
        run_token = idle.begin_run()
        report = None
        report_saved = False
        try:
            cfg = load_config()

            self.status.emit("连接设备...")
            import uiautomator2 as u2
            d = u2.connect(self.device_id)
            d.implicitly_wait(10)
            if self._stop_requested:
                # ★ 统一消息(审查 2026-10-12): 界面按"消息 == 已手动停止"识别中性停止,
                #   两种写法会让其中一种被渲染成红色"未通过"
                self.finished_run.emit(False, "已手动停止")
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

            # ★ 同名用例消歧(审查 2026-10-12): 两个文件各有一个「冒烟」时, 报告表键
            #   只由「用例名+轮次」组成 → 两次执行并进同一张表、共用同一批截图。
            #   检测到重名就给表名/目录带上文件名后缀 —— 与遍历顺序无关, 任何一轮都稳定。
            from collections import Counter
            _dup_names = {n for n, c in Counter(
                c.get("name", "未命名用例") for _m, c, _f in tasks).items() if c > 1}

            report = ExcelReport()
            all_passed = True
            total_rounds = self.repeat
            for rnd in range(total_rounds):
                for module, case, fp in tasks:
                    if self._stop_requested:
                        break
                    case_name = case.get("name", "未命名用例")
                    # ★ 重名用例带文件名后缀(见 _dup_names), 表键/截图目录/结果标签共用
                    case_key = (f"{case_name}({os.path.splitext(os.path.basename(fp))[0]})"
                                if case_name in _dup_names else case_name)
                    steps = case.get("steps") or []
                    # ★ 报告按「**一次执行**」分表(用户要求 2026-10-11): 同一用例跑 N 轮各占
                    #   一张表(用例名-1 / 用例名-2 …), 不再把几轮结果堆在同一张表里;
                    #   顺带也解决"同一个用例文件里有多个用例时共用一张表"。
                    sheet = case_sheet_name(case_key, rnd + 1)
                    # ★ 截图目录与表键**解耦**(审查 2026-10-12): 目录必须是「用例名-第N次」,
                    #   封顶清理才认得; 表名是给 openpyxl 的(它自己会洗非法字符/去重)
                    run_dir = _shot_dir_name(case_key, rnd + 1)
                    # ★ 本次执行的标签: 结果表每行都带它, 一眼看出"现在跑的是第几轮"
                    self._exec_label = f"{case_key}(第{rnd + 1}次)"
                    # 用例所在 APP 组 = 用例文件所在目录名
                    group = os.path.basename(
                        os.path.dirname(os.path.abspath(fp)))

                    # ★ 前置: **每个用例执行前**都按该组的配置全量跑一遍(2026-10-11 用户要求:
                    #   "每次执行都要跑前置")。前置项按 APP 组配置(resolve_group_preconditions),
                    #   但**不再**同组复用 —— 重复轮次里的同一个用例也会重跑。
                    #   · 代价: 一轮 N 个用例 = N 次全量前置(充电等待这类是"看到即返回",
                    #     缺东西才等超时), 偶发失败的阻断影响面也相应变大;
                    #   · 是否重启 APP 由前置列表里的 restart 项决定(不再硬编码"同组之间重启")。
                    items = resolve_group_preconditions(
                        self.pre_by_group, group, self.pre_items)
                    self.status.emit(
                        f"前置准备[{group or '默认组'}]({case_name})...")
                    pre_results = session.prepare_items(
                        d, cfg, items,
                        on_progress=self.status.emit,
                        should_cancel=lambda: self._stop_requested,
                        # ★ 每完成一条立刻落表(用户要求: 一条一条显示结果和耗时)
                        on_item_done=lambda r, m=sheet, sd=run_dir:
                        self._emit_pre_result(d, m, report, r, shot_dir=sd),
                        # ★ 前置的内部步骤截图也进本轮目录(审查 2026-10-12): 否则它退回
                        #   用前置名当目录, 每轮写同一路径互相覆盖, 且封顶清理永远不收
                        shot_subdir=run_dir)
                    # ★ 任一前置项未通过 → 阻断:本轮不再执行任何用例(用户要求)
                    failed = [r["desc"] for r in pre_results if not r["ok"]]
                    if failed:
                        detail = "、".join(failed)
                        self.log_line.emit(f"[前置] 未通过,阻断本轮执行: {detail}")
                        self.precondition_failed.emit(detail)
                        self.finished_run.emit(False, f"前置检查未通过: {detail}")
                        return
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
                        self._emit_step(r2)

                    def on_step_start(desc, attempt=1):
                        # ★ 步骤**开始**就通知界面(先插一行「执行中」), 跑完再由 on_result
                        #   就地改成 PASS/FAIL —— 以前要等整步跑完才看得到任何东西,
                        #   长步骤(如 60s 断言)期间界面像卡住(用户要求 2026-10-12)。
                        last_ts["t"] = _time.time()      # 耗时从"本步开始"算
                        p = {"desc": desc, "attempt": attempt}
                        if self._exec_label:
                            p["case"] = self._exec_label
                        self.step_started.emit(p)

                    runner.shot_subdir = run_dir     # 截图进 screenshots/<用例名-第N次>/
                    runner.on_step_start = on_step_start
                    runner.on_result = on_result
                    self.runner = runner
                    passed = runner.run_steps(steps)
                    self.runner = None

                    # 与 pytest 相同结构写入 Excel 报告
                    for r2 in runner.results:
                        try:
                            report.set_step_desc(sheet, r2.get("desc", ""))
                            report.add_result(sheet, r2["passed"], r2.get("error", ""),
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
                                from core import bootstrap as _bs
                                _bs.trim_failures(fail_dir)      # 到上限就循环覆盖最旧的
                            except Exception:
                                pass
                            self.status.emit(f"用例失败: {case_name}")
                            self.case_finished.emit(fp, case_name, False)   # 状态灯 → 红
                    else:
                        self.status.emit(f"用例通过: {case_name}")
                        self.case_finished.emit(fp, case_name, True)   # 状态灯 → 绿

                    # ★ 本用例截图封顶(审查 2026-10-12): 每个用例跑完就地收一次自己的
                    #   旧截图目录。以前这调用在**轮次循环之后**、且传的是裸用例名 ——
                    #   一旦首轮开始前就停止, case_name 未赋值直接 NameError(被吞);
                    #   defer 按用例键去重(bootstrap 里同名只排一次)。
                    try:
                        from core import bootstrap as _bs, vision as _vs
                        _bs.trim_case_screenshots(
                            os.path.join(BASE_DIR, "Test_cases",
                                         _vs.current_app_group() or "未分组",
                                         "screenshots"),
                            case_name=run_dir.rsplit("-第", 1)[0])
                    except Exception:
                        pass

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
            # ★ 收尾: 本轮结束 = 空闲了, 把运行期间推迟的封顶清理补做一遍(后台线程)。
            #   放在 finally 的最后 —— 报告都已落盘, 清理看到的是完整产物。
            idle.end_run(run_token)
