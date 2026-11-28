# -*- coding: utf-8 -*-
"""续航测试：满电开始清扫 → 低电量回充，记录实际清扫时间与每 10% 电量变化。

流程（用户 2026-11-24 确认）：
  1. 等机器人满电（100%，在桩上）；
  2. 设置清扫模式/吸力/水量，启动总清扫；
  3. 清扫到低电量（默认 20%）自动回充为止；
  4. 若提前清扫完成但电量充足（> 低电阈值），自动重新开始清扫，
     续航时间从开始清扫到低电回充（含转场/重定位/越坎）；
  5. 每 15 秒采样一次，输出每 10% 电量节点与完整报告。
"""
import csv
import json
import os
import re
import shutil
import time
from datetime import datetime

from core.apos.faults import event_name, is_fault_event
from core.apos.mcu import McuStream
from core.apos.service import ParamService
from core.apos.stream import UploadStream
from core.driver import BASE_DIR, REPORT_SUBDIRS, split_texts
from core.logger import get_logger

log = get_logger()

CLEAN_STATUSES = frozenset({
    "work_status_total_clean", "work_status_area_clean",
    "work_status_segment_clean", "work_status_curpoint_clean",
    "work_status_clean",
})
#: 满电时如果处于这些状态，说明还有旧的清扫/暂停任务，要先停掉再新开
TASK_ACTIVE_STATUSES = CLEAN_STATUSES | {
    "work_status_pause", "work_status_base_station_pause",
    "work_status_back_charge_pause",
}
MODE_TO_CLEAN_TYPE = {
    "sweepOnly": "CLEAN_TYPE_SWEEP",
    "mopOnly": "CLEAN_TYPE_MOP",
    "sweepMop": "CLEAN_TYPE_SWEEP_AND_MOP",
    "sweepThenMop": "CLEAN_TYPE_SWEEP_THEN_MOP",
}
RETURN_STATUSES = frozenset({
    "work_status_back_charge", "work_status_go_charge",
    "work_status_charging", "work_status_full_charging",
    "work_status_back_charge_pause",
})
#: 电量充足时这些状态都不能停，必须立即重新开始清扫
RESTART_STATUSES = RETURN_STATUSES | {
    "work_status_idle", "work_status_dormant",
    "work_status_base_station", "work_status_collect_dust",
    "work_status_dry_mop", "work_status_wash_mop",
}
#: 拖地类模式：清扫中途会回站洗拖布，期间不能当「提前完成」重启任务
MOP_MODES = frozenset({"sweepMop", "mopOnly", "sweepThenMop"})
#: 回站（洗拖布/集尘）相关状态；烘干/充电表示回站已经完成
WASH_STATUSES = frozenset({
    "work_status_back_charge", "work_status_go_charge",
    "work_status_base_station", "work_status_collect_dust",
    "work_status_wash_mop",
})
#: 回充成功后进入的状态：充电/充满/烘干 → 立即下发新的清扫任务
RETURN_DONE_STATUSES = frozenset({
    "work_status_charging", "work_status_full_charging",
    "work_status_dry_mop",
})
#: 拖地模式回站洗拖布后等机器人自动继续清扫的最长时间
WASH_RESUME_TIMEOUT = 10 * 60


def is_wash_wait(mode, status):
    """是否是「拖地回站洗拖布」的等待状态（不是任务完成/低电回充）。"""
    return mode in MOP_MODES and status in WASH_STATUSES


SETTING_CHANNELS = ("mode", "fan", "water")


def suite_total_timeout(max_duration, n_combos):
    """连续续航的总超时：每个组合 max_duration × 组合数（用户 2026-11-26）。"""
    return max(1, int(max_duration)) * max(1, int(n_combos))


def _ctype_label(ctype, area=None):
    """清扫类型的中文说明（与循环清扫一致）。"""
    if ctype == "rooms":
        return "选区 %s" % ",".join(str(x) for x in (area or []))
    if ctype == "zones":
        return "划区 %d 个" % len(area or [])
    if ctype == "point":
        pt = list(area or [])[:2]
        return ("定点 %d,%d" % tuple(pt)) if len(pt) == 2 else "定点"
    if ctype == "curpoint":
        return "原地定点"
    return "全局"


def plan_settings(applied, mode, fan_level, water_level, only=None):
    """计算本次需要下发的挡位通道。

    · 默认只发与上次成功下发值不同的通道（用户 2026-11-26：没变化就别重复发）；
    · only 指定强制下发的通道（观测到偏离时恢复用，如 ("fan",)）。
    """
    desired = {"mode": mode, "fan": fan_level, "water": water_level}
    if only is not None:
        return {ch: desired[ch] for ch in only if ch in desired}
    return {ch: v for ch, v in desired.items() if applied.get(ch) != v}
#: 机器人已经关机/下电的状态名（不同固件叫法可能不同）
SHUTDOWN_STATUSES = frozenset({
    "work_status_shutdown", "work_status_power_off", "work_status_off",
})
#: 固件默认低于 5% 自动关机
SHUTDOWN_BATTERY = 5
SAMPLES_NAME = "samples.csv"


#: 挡位变化记录里的“当前状态”中文标签
_STATUS_LABELS = {
    "work_status_charging": "充电中",
    "work_status_full_charging": "充满",
    "work_status_back_charge": "回充中",
    "work_status_go_charge": "回充中",
    "work_status_idle": "待机",
    "work_status_dormant": "休眠",
    "work_status_error": "故障",
    "work_status_pause": "暂停",
    "work_status_base_station": "基站",
    "work_status_base_station_pause": "基站暂停",
    "work_status_collect_dust": "集尘中",
    "work_status_dry_mop": "烘干中",
    "work_status_wash_mop": "洗拖布",
    "work_status_explore_map": "建图中",
    "work_status_remote": "遥控中",
    "work_status_shutdown": "关机",
}


def _status_label(status):
    """把 work_status_* 转成挡位记录里可读的中文状态。"""
    if status in CLEAN_STATUSES:
        return "清扫中"
    if status in _STATUS_LABELS:
        return _STATUS_LABELS[status]
    return str(status or "运行中").replace("work_status_", "") or "运行中"


class _EnduranceAbort(Exception):
    """续航流程中可预期的终止（非异常），end_reason 直接采用消息。"""


def charge_decision(battery, status, attempts, now, next_try):
    """开测前「等满电/回充」状态机（纯函数，便于单测）。

    返回:
      ready   满电且不在清扫 → 可以开测
      wait    在充电/回充，或状态/电量未知 → 继续等
      attempt 不在充电/回充 → 立即发一次 find_charger
      fail    已尝试 3 次仍未进入充电/回充 → 测试失败
    """
    # 满电即可开测：如果还存在清扫/暂停任务，由调用方先停掉再重新开始
    if battery >= 100:
        return "ready"
    if battery < 0 or not status:
        return "wait"
    if battery < 100 and status not in RETURN_STATUSES and now >= next_try:
        return "fail" if attempts >= 3 else "attempt"
    return "wait"


_LOCK_PATH = os.path.join(
    os.environ.get("TEMP") or os.environ.get("TMP") or ".",
    "autotest_endurance.lock")


def _pid_alive(pid):
    try:
        if os.name == "nt":
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(
                handle, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(handle)
            return code.value == 259      # STILL_ACTIVE
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _acquire_lock():
    """单实例锁：后台启动器可能把同一命令跑两份，两个 APOS 客户端会互相打架。"""
    for _ in range(3):
        try:
            fd = os.open(_LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.write(fd, str(os.getpid()).encode("ascii"))
            return fd
        except FileExistsError:
            pid = 0
            try:
                pid = int(open(_LOCK_PATH, encoding="ascii").read().strip())
            except Exception:
                pass
            if pid and _pid_alive(pid):
                raise RuntimeError(
                    "已有续航测试在运行（PID %d），禁止并发控制机器人" % pid)
            try:
                os.remove(_LOCK_PATH)
            except OSError:
                pass
    raise RuntimeError("无法获取续航测试锁: %s" % _LOCK_PATH)


def _release_lock(fd):
    try:
        os.close(fd)
    except Exception:
        pass
    try:
        os.remove(_LOCK_PATH)
    except OSError:
        pass


def _fmt_duration(seconds):
    seconds = int(max(0, seconds))
    return "%d:%02d:%02d" % (seconds // 3600, seconds % 3600 // 60,
                             seconds % 60)


def _pct_text(battery):
    """电量文本：未知(推送未就绪, -1/None)显示「未知」，不显示 -1%。"""
    try:
        b = int(battery)
    except (TypeError, ValueError):
        return "未知"
    return "%d%%" % b if b >= 0 else "未知"


_LOG_TS_RE = re.compile(
    r"^(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{1,2}):(\d{1,2})\.(\d{3})")


def _line_ts(line, now, state):
    """设备日志行内的时间 → 本机 epoch（自动校准机器人时钟偏移）。

    例：`11-26 19:1:39.597/SW D/...`。机器人时钟可能与本机有偏差（实测
    +1 小时），首次解析用 (now - 行时间) 作为固定偏移，既保证间隔计算正确，
    又让报告时间仍能和本机日志对上。
    """
    m = _LOG_TS_RE.match(line)
    if not m:
        return now
    mon, day, hh, mm, ss, ms = (int(x) for x in m.groups())
    try:
        raw = time.mktime((datetime.now().year, mon, day, hh, mm, ss,
                           0, 0, -1))
    except (ValueError, OverflowError):
        return now
    raw += ms / 1000.0
    if "ts_offset" not in state:
        off = now - raw
        state["ts_offset"] = off if abs(off) <= 24 * 3600 else 0.0
    return raw + state["ts_offset"]


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _abs_out_dir(path):
    """结果目录一律转绝对路径。

    默认的 "reports/..." 若按进程工作目录解析, 从别处启动 GUI/计划任务时
    报告会散落到其他目录, 「打开结果目录」也就可能找不到。统一锚到 BASE_DIR。
    """
    return path if os.path.isabs(path) else os.path.join(BASE_DIR, path)


#: 续航结果最多保留最近 10 次（用户 2026-11-26：目录不能无限增大）
KEEP_RUNS = 10

_RUN_DIR_PREFIXES = ("endurance_suite_", "endurance_", "shutdown_",
                     "loop_")
_RUN_XLSX_PREFIX = "续航测试"


def prune_runs(root=None, keep=KEEP_RUNS, protect=(), log_fn=None):
    """只保留最近 keep 次续航结果, 删除更早的目录与导出 Excel。

    root 默认 BASE_DIR/reports; protect 内的路径（当前这次运行）永不删除。
    返回删除的条目数。
    """
    root = root or os.path.join(BASE_DIR, "reports",
                              REPORT_SUBDIRS["endurance"])
    keep = max(1, int(keep))
    protect = {os.path.normcase(os.path.abspath(p)) for p in protect if p}
    try:
        names = os.listdir(root)
    except OSError:
        return 0

    dirs, xlsx = [], []
    for n in names:
        p = os.path.join(root, n)
        if os.path.normcase(os.path.abspath(p)) in protect:
            continue
        try:
            if os.path.isdir(p) and n.startswith(_RUN_DIR_PREFIXES):
                dirs.append((os.path.getmtime(p), p))
            elif (os.path.isfile(p) and n.startswith(_RUN_XLSX_PREFIX)
                  and n.lower().endswith(".xlsx")):
                xlsx.append((os.path.getmtime(p), p))
        except OSError:
            continue

    dirs.sort(reverse=True)
    victims = [p for _, p in dirs[keep:]]
    # 根目录下的单次报告 Excel: 删掉比「第 keep 个结果目录」还老的,
    # 保证它不会脱离对应结果目录单独残留。没有目录时按数量兜底。
    if dirs:
        cutoff = dirs[min(keep, len(dirs)) - 1][0]
        victims += [p for ts, p in xlsx if ts < cutoff]
    else:
        xlsx.sort(reverse=True)
        victims += [p for _, p in xlsx[keep:]]

    removed = 0
    for p in sorted(set(victims)):
        try:
            if os.path.isdir(p):
                shutil.rmtree(p)
            else:
                os.remove(p)
            removed += 1
        except OSError as e:
            if log_fn is not None:
                log_fn("[清理] 删除失败 %s: %s" % (p, e))
    if removed and log_fn is not None:
        log_fn("[清理] 续航结果只保留最近 %d 次, 已删除 %d 项旧记录"
               % (keep, removed))
    return removed


def _status(st):
    v = st.values().get("/CleanTask/CleanAppStatus")
    if isinstance(v, dict):
        return str(v.get("CleanAppStatus") or "")
    return str(v or "")


def _keepalive(st, state, log_fn, ip, paths):
    """推送流看门狗：断了就重新 Init（退避重试，失败 30 秒后再来一轮）。

    ★ 不要用 `UploadStream.restart()` 单次调用：它失败后会把值清空、
      句柄变成 -1，后续挡位事件全部发不出去（实测）。这里直接 start 重试。
    ★ 机器人侧释放旧会话需要几秒，1 秒间隔连续重试基本都撞在释放窗口里
      （实测：3 次全失败，30 秒后一次成功），所以改成 0/3/5 秒退避。
    """
    now = time.time()
    # 先做一次保活读：和上位机一样主动维持推送会话，别等它静默后再重连
    _poke_stream(st, paths, state)
    if now < state.get("next_try", 0.0):
        return False                  # 失败后的冷却期（不分句柄是否还在）
    if st.running:
        last = st.last_event() or 0.0
        since = state.setdefault("since", now)
        if not last or last < since:
            # 刚订阅/刚重连还没收到第一条推送：给 90 秒宽限。
            # ★ 没有这个宽限，刚开流就会因为"从未收到事件"被判死 → 重启 →
            #   又没等到第一条 → 再重启，循环里永远收不到数据（电量一直是 -1）。
            if now - since < 90:
                return False
        elif now - last < 60:
            return False
    state["last_restart"] = now
    for delay in (0.0, 3.0, 5.0):
        if delay:
            time.sleep(delay)
        try:
            if st.start(ip, paths, preserve=True):
                state["next_try"] = 0.0
                state["since"] = time.time()   # 重新计时宽限
                log_fn("[推送] 数据流已重新订阅")
                return True
        except Exception:
            pass
    state["next_try"] = time.time() + 30
    log_fn("[推送] 重连未成功，30 秒后重试")
    return False


def _battery(st):
    v = st.values().get("/BatteryBattery/BatteryLevel")
    try:
        return int(v)
    except (TypeError, ValueError):
        return -1


def _stream_fresh(st, max_age=20.0):
    """推送通道是否新鲜（最近 max_age 秒内收到过推送）。

    ★ 会话静默后 SDK 的 SendEvent 仍可能返回"成功"，但事件到不了机器人
      （实测：APP 把吸力切到 1，续航重发 4 无效，直到推流重建后才生效）。
    """
    if st is None or not getattr(st, "running", False):
        return False
    try:
        last = st.last_event() or 0.0
    except Exception:
        return False
    return bool(last) and (time.time() - last) <= max_age


def _poke_stream(st, paths, state, interval=20.0):
    """在推流句柄上做一次轻量读，保活订阅（上位机同款做法）。

    ★ 机器人侧推送约 35 秒无交互会自己停；设备参数页的 ParamService 就是靠
      周期性的"保活读"维持的（见 service.py 的 _loop）。续航只被动收推送，
      空闲（等充满电/等低电）时必然掉订阅 —— 这是反复"重新订阅"的根源。
    """
    now = time.time()
    if now - state.get("last_poke", 0.0) < interval:
        return False
    state["last_poke"] = now
    if st is None:
        return False
    try:
        st.read(paths, timeout_ms=1500)
        return True
    except Exception:
        return False


def _battery_vol(st):
    """电池电压，统一成 V（设备属性是 mV，如 22895）。"""
    v = st.values().get("/BatteryBattery/BatteryVol")
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f / 1000.0, 2) if f > 100 else round(f, 2)


def compute_ten_percent(samples, start_battery=100, low_battery=20):
    """从采样里取每 10% 电量节点（含起始电量与低电阈值）。

    samples: [{ts, battery, clean_seconds, status}]，按时间升序。
    返回 [{level, ts, battery, clean_seconds, status}]。
    """
    out = []
    levels = list(range(int(start_battery), int(low_battery) - 1, -10))
    if levels and levels[-1] != low_battery:
        levels.append(int(low_battery))
    for level in levels:
        for s in samples:
            if s["battery"] <= level and s.get("clean_seconds", 0) is not None:
                out.append({"level": level, "battery": s["battery"],
                            "ts": s["ts"], "clean_seconds":
                            s.get("clean_seconds", 0),
                            "status": s.get("status", "")})
                break
    return out


def compute_percent_steps(samples, start_battery=20, end_battery=5, step=1):
    """按每 `step`% 取电量节点（低电→关机测试用）。

    返回 [{level, battery, ts, elapsed}]，elapsed 为距首采样的秒数。
    """
    if not samples:
        return []
    t0 = samples[0]["ts"]
    out = []
    level = int(start_battery)
    while level >= int(end_battery):
        for s in samples:
            b = s.get("battery")
            if isinstance(b, int) and 0 <= b <= level:
                out.append({"level": level, "battery": b, "ts": s["ts"],
                            "elapsed": s["ts"] - t0})
                break
        level -= step
    return out


def shutdown_reached(battery, status, offline_seconds, shutdown_battery=5):
    """低电关机判定（纯函数，便于单测）。

    关机后 APOS 推送会断，所以电量到阈值后还要确认离线；个别固件会
    先上报 `work_status_shutdown`。
    """
    if status in SHUTDOWN_STATUSES:
        return True
    if battery < 0:
        return offline_seconds >= 30
    if battery <= 1:
        return offline_seconds >= 10
    if battery <= shutdown_battery:
        return offline_seconds >= 45
    # 最后一次推送可能停在 5~7%（固件按电压判断关机），离线够久也算关机
    if battery <= shutdown_battery + 2:
        return offline_seconds >= 180
    return False


def render_shutdown_report(result):
    """生成「低电回充 → 低电关机」Markdown 报告。"""
    lines = ["# 低电至关机续航报告", ""]
    lines += ["## 测试条件", "",
              "| 项目 | 值 |", "|---|---|",
              "| 设备IP | %s |" % result.get("ip", ""),
              "| 设备SN | %s |" % (result.get("sn") or "—"),
              "| 固件版本 | %s |" % (result.get("firmware") or "—"),
              "| 起始电量 | %s%%（低电回充状态） |"
              % result.get("start_battery"),
              "| 关机电量阈值 | ≤%s%% |" % result.get("shutdown_battery"),
              "| 采样间隔 | %s 秒 |" % result.get("sample_interval"),
              ""]
    lines += ["## 汇总", "",
              "| 项目 | 值 |", "|---|---|",
              "| 开始时间 | %s |" % result.get("start_time", ""),
              "| 关机时间 | %s |" % result.get("end_time", ""),
              "| 低电至关机续航 | %s（%d 分钟） |"
              % (_fmt_duration(result.get("shutdown_seconds", 0)),
                 int(float(result.get("shutdown_seconds", 0)) / 60 + 0.5)),
              "| 结束电量 | %s%% |" % result.get("end_battery", "—"),
              "| 结果 | %s |" % ("完成" if result.get("completed")
                                else "未完成"),
              "| 结束原因 | %s |" % result.get("end_reason", ""),
              ""]
    steps = result.get("percent_steps") or []
    if steps:
        lines += ["## 每 1% 用时", "",
                  "| 电量 | 到达时间 | 距开始(分钟) |", "|---|---|---|"]
        for s in steps:
            lines.append("| %s%% | %s | %d |"
                         % (s["level"],
                            datetime.fromtimestamp(s["ts"]).strftime(
                                "%H:%M:%S"),
                            int(s["elapsed"] / 60 + 0.5)))
        lines.append("")
    lines += ["## 详细数据", "",
              "| 时间 | 电量 | 电池电压(V) | 状态 | 距开始(分钟) |",
              "|---|---|---|---|---|"]
    t0 = (result.get("samples") or [{}])[0].get("ts", 0)
    for s in result.get("samples") or []:
        v = s.get("battery_vol")
        lines.append("| %s | %s%% | %s | %s | %d |"
                     % (datetime.fromtimestamp(s["ts"]).strftime("%H:%M:%S"),
                        s.get("battery"), "%.2f" % v
                        if isinstance(v, (int, float)) else "—",
                        s.get("status", ""),
                        int((s["ts"] - t0) / 60 + 0.5)))
    lines.append("")
    return "\n".join(lines)


def run_shutdown_endurance(ip, low_battery=20, shutdown_battery=SHUTDOWN_BATTERY,
                           sample_interval=15.0, wait_low_timeout=1800,
                           max_duration=8 * 3600, out_dir=None, log_fn=None):
    """低电回充 → 低电关机续航测试。

    前提：机器人处于低电回充状态（电量 ≤ low_battery），且充电桩不可用/
    未接触，电量会持续下降到固件关机阈值（默认 5%）后自动关机。
    本测试只维持"回充"状态并记录，不主动清扫；检测到电量回升（接上
    充电器）会提前结束并标记未完成。
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = _abs_out_dir(out_dir or os.path.join("reports", REPORT_SUBDIRS["endurance"], "shutdown_%s" % stamp))
    os.makedirs(base, exist_ok=True)
    csv_path = os.path.join(base, SAMPLES_NAME)
    run_log = os.path.join(base, "run.log")

    def _log(msg):
        line = "[%s] %s" % (_now(), msg)
        try:
            with open(run_log, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
        if log_fn is not None:
            log_fn(msg)

    st = UploadStream()
    paths = ["/BatteryBattery/BatteryLevel", "/BatteryBattery/BatteryVol",
             "/CleanTask/CleanAppStatus"]
    if not st.start(ip, paths):
        raise RuntimeError("APOS 推送流连接失败: %s" % ip)
    svc = ParamService(None, stream=st)
    result = {"kind": "shutdown", "ip": ip, "low_battery": low_battery,
              "shutdown_battery": shutdown_battery,
              "sample_interval": sample_interval, "completed": False,
              "events": [], "samples": []}
    samples = result["samples"]
    ka = {"last_restart": 0.0}

    def event(name, detail=""):
        result["events"].append({"ts": time.time(), "name": name,
                                 "detail": str(detail)})
        _log("[事件] %s %s" % (name, detail))

    def sample(battery, status):
        if battery is None or battery < 0:
            return                        # 推送未就绪时不写无效电量
        samples.append({"ts": time.time(), "battery": battery,
                        "battery_vol": _battery_vol(st),
                        "status": status})
        _write_csv(csv_path, samples)

    try:
        try:
            info = st.read(["/System/CustomSN", "/System/FirmwareInfo"],
                           timeout_ms=2000) or {}
            result["sn"] = info.get("/System/CustomSN") or ""
            fw = info.get("/System/FirmwareInfo")
            if isinstance(fw, str):
                fw = json.loads(fw)
            if isinstance(fw, dict):
                result["firmware"] = "%s %s" % (fw.get("Model", ""),
                                                fw.get("Version", ""))
        except Exception:
            pass

        # 1) 等到低电回充状态
        _log("[准备] 等待进入低电回充状态（≤%s%%），最多 %d 分钟…"
             % (low_battery, wait_low_timeout // 60))
        deadline = time.time() + wait_low_timeout
        last_note = 0.0
        while True:
            battery = _battery(st)
            status = _status(st)
            _keepalive(st, ka, _log, ip, paths)
            if (battery >= 0 and battery <= low_battery
                    and status not in CLEAN_STATUSES):
                break
            if time.time() > deadline:
                raise RuntimeError("等待低电超时（当前电量：%s，状态：%s）"
                                   % (_pct_text(battery), status or "未知"))
            if time.time() - last_note > 60:
                last_note = time.time()
                _log("[准备] 当前电量：%s，状态：%s，继续等待…"
                     % (_pct_text(battery), status or "未知"))
            time.sleep(10)

        t0 = time.time()
        result["start_time"] = _now()
        result["start_battery"] = battery
        result["start_status"] = status
        sample(battery, status)
        event("低电就绪", "当前电量：%s，状态：%s"
              % (_pct_text(battery), status or "未知"))
        # 保持回充状态（已低电回充时重发一次无害）
        svc.start_find_charger()

        charge_guard = None
        last_retry = 0.0
        last_sample = time.time()
        end_ts = t0
        end_reason = ""
        while True:
            now = time.time()
            _keepalive(st, ka, _log, ip, paths)
            battery = _battery(st)
            status = _status(st)
            offline = now - (st.last_event() or now)

            # 维持"回充"状态：不在清扫/回充态就再次发起回充
            if status in CLEAN_STATUSES:
                if now - last_retry > 30:
                    last_retry = now
                    event("停止清扫", "保持低电回充")
                    svc.stop_clean()
            elif status not in RETURN_STATUSES and now - last_retry > 60:
                last_retry = now
                event("重发回充", status)
                svc.start_find_charger()

            # 接上充电器时电量会回升，测不到关机
            if (battery >= 0 and battery > result["start_battery"] + 2
                    and status in ("work_status_charging",
                                   "work_status_full_charging")):
                if charge_guard is None:
                    charge_guard = now
                elif now - charge_guard > 120:
                    end_reason = "检测到充电（电量回升），无法完成至关机"
                    result["return_status"] = status
                    result["return_time"] = now
                    end_ts = now
                    break
            else:
                charge_guard = None

            if now - last_sample >= sample_interval:
                last_sample = now
                sample(battery, status)
                _log("[进度] %s 当前电量：%s，状态：%s，已持续 %s"
                     % (_now(), _pct_text(battery), status or "未知",
                        _fmt_duration(now - t0)))

            if shutdown_reached(battery, status, offline, shutdown_battery):
                end_reason = "低电关机（%s）" % _pct_text(battery)
                result["end_battery"] = battery
                result["return_status"] = status
                result["return_time"] = now
                end_ts = min(now, st.last_event() or now)
                sample(battery, status)
                break

            if now - t0 > max_duration:
                end_reason = "测试超时（%d 小时）" % (max_duration // 3600)
                result["end_battery"] = battery
                result["return_status"] = status
                result["return_time"] = now
                end_ts = now
                break

            time.sleep(1.0)

        result["shutdown_seconds"] = max(0.0, end_ts - t0)
        result["wall_seconds"] = result["shutdown_seconds"]
        result["completed"] = end_reason.startswith("低电关机")
        result["end_reason"] = end_reason
        result["end_time"] = _now()
        result["percent_steps"] = compute_percent_steps(
            samples, result.get("start_battery", low_battery),
            shutdown_battery)
        event("测试结束", end_reason)
    except Exception as e:
        result["completed"] = False
        result["end_reason"] = "异常: %s" % e
        result["end_time"] = _now()
        result["shutdown_seconds"] = 0.0
        result["wall_seconds"] = 0.0
        result["percent_steps"] = []
        event("异常", e)
    finally:
        st.stop()

    result.setdefault("shutdown_seconds", 0.0)
    result.setdefault("wall_seconds", 0.0)
    result.setdefault("end_battery", None)
    result.setdefault("percent_steps", [])
    with open(os.path.join(base, "result.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    with open(os.path.join(base, "report.md"), "w", encoding="utf-8") as f:
        f.write(render_shutdown_report(result))
    _log("[完成] 报告: %s" % os.path.join(base, "report.md"))
    prune_runs(protect=[base], log_fn=_log)
    return result, base


#: 连续续航组合（设计文档表格：仅扫 1/2/3/4；扫拖 1+1、2+2、3+3、4+3）
DEFAULT_COMBOS = (
    ("sweepOnly", 1, 0), ("sweepOnly", 2, 0),
    ("sweepOnly", 3, 0), ("sweepOnly", 4, 0),
    ("sweepMop", 1, 1), ("sweepMop", 2, 2),
    ("sweepMop", 3, 3), ("sweepMop", 4, 3),
)


def parse_combos(text):
    """解析 `mode:fan[:water]` 逗号串；空串返回 None（用默认组合）。"""
    if not str(text or "").strip():
        return None
    out = []
    for part in split_texts(text):
        bits = part.split(":")
        if len(bits) < 2:
            raise ValueError("组合格式应为 mode:fan[:water]: %s" % part)
        mode, fan = bits[0].strip(), int(bits[1])
        water = int(bits[2]) if len(bits) > 2 and bits[2].strip() else 0
        if mode not in MODE_TO_CLEAN_TYPE:
            raise ValueError("未知清扫模式: %s" % mode)
        out.append((mode, fan, water))
    return out


def run_endurance_suite(ip, combos=None, out_dir=None, stop_on_fail=False,
                        report=True, shutdown_after=False, shutdown_kwargs=None,
                        log_fn=None, stop_event=None, on_state=None,
                        max_duration=8 * 3600, stream=None, **run_kwargs):
    """连续跑多个模式/挡位组合，每个组合等满电后开始，结束生成汇总 Excel。

    shutdown_after=True 时，最后一个组合低电回充后不再充电，直接转入
    「低电回充→低电关机」监测（需现场保证充电器不可用）。

    ★ 超时（用户 2026-11-26）：
      · 每个组合最多 `max_duration`（默认 8 小时，从实际开始清扫计时）；
      · 整个任务的总超时 = `max_duration × 组合数`。
    返回 (results, suite_dir, report_path)。
    """
    combos = list(combos or DEFAULT_COMBOS)
    max_duration = int(run_kwargs.pop("max_duration", max_duration))
    suite_deadline = time.time() + suite_total_timeout(max_duration,
                                                       len(combos))
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = _abs_out_dir(out_dir or os.path.join("reports", REPORT_SUBDIRS["endurance"], "endurance_suite_%s" % stamp))
    os.makedirs(base, exist_ok=True)
    suite_log = os.path.join(base, "suite.log")

    def _log(msg):
        line = "[%s] %s" % (_now(), msg)
        try:
            with open(suite_log, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
        if log_fn is not None:
            log_fn(msg)

    entries = []
    dirs = []
    for idx, combo in enumerate(combos, 1):
        # 兼容旧三元组 (mode, fan, water) 和循环清扫的五元组
        # (mode, fan, water, clean_type, area)
        vals = list(combo) + [None] * (5 - len(combo))
        mode, fan, water, ctype, area = vals[:5]
        ctype = ctype or "total"
        area = list(area or [])
        if stop_event is not None and stop_event.is_set():
            _log("[组合] 用户停止，结束后续组合")
            break
        remaining = suite_deadline - time.time()
        if remaining <= 0:
            _log("[组合] 总任务超时（每个组合 %d 小时 × %d 个组合），结束后续组合"
                 % (max_duration // 3600, len(combos)))
            break
        name = "%02d_%s_fan%s" % (idx, mode, fan)
        if mode == "sweepMop":
            name += "_water%s" % water
        if ctype == "rooms":
            name += "_room%s" % ",".join(str(x) for x in area)
        elif ctype == "zones":
            name += "_zone%d" % len(area)
        elif ctype == "point":
            name += "_point"
            if len(area) >= 2:
                name += "_%s_%s" % (area[0], area[1])
        elif ctype == "curpoint":
            name += "_point"
        run_dir = os.path.join(base, name)
        _log("[组合 %d/%d] %s 吸力%s 水量%s%s 开始（超时上限 %s）"
             % (idx, len(combos), mode, fan, water,
                "" if ctype == "total" else "（%s）" % _ctype_label(ctype, area),
                _fmt_duration(min(max_duration, remaining))))
        result, d = run_endurance(ip, mode=mode, fan_level=fan,
                                  water_level=water, out_dir=run_dir,
                                  log_fn=_log, stop_event=stop_event,
                                  on_state=on_state,
                                  max_duration=min(max_duration, remaining),
                                  stream=stream, clean_type=ctype, area=area,
                                  **run_kwargs)
        dirs.append(d)
        entries.append({"mode": mode, "fan": fan, "water": water,
                        "clean_type": ctype, "area": area,
                        "dir": d, "completed": bool(result.get("completed")),
                        "seconds": result.get("shutdown_seconds")
                        if result.get("kind") == "shutdown"
                        else result.get("wall_seconds"),
                        "reason": result.get("end_reason")})
        _log("[组合 %d/%d] 结束：%s（%s）"
             % (idx, len(combos),
                "完成" if result.get("completed") else "未完成",
                result.get("end_reason")))
        if stop_on_fail and not result.get("completed"):
            _log("[组合] 配置为失败即停，终止后续组合")
            break

    if shutdown_after and entries and entries[-1].get("completed"):
        sd_dir = os.path.join(base, "低电至关机")
        _log("[关机续航] 从低电回充状态持续到关机（注意保持充电器不可用）…")
        sd_result, d = run_shutdown_endurance(
            ip, out_dir=sd_dir, log_fn=_log, **(shutdown_kwargs or {}))
        dirs.append(d)
        entries.append({"mode": "shutdown", "fan": None, "water": None,
                        "dir": d, "completed": bool(sd_result.get("completed")),
                        "seconds": sd_result.get("shutdown_seconds"),
                        "reason": sd_result.get("end_reason")})

    report_path = None
    if report and dirs:
        try:
            from tools.endurance_report import build_report
            report_path = os.path.join(base, "续航测试_汇总.xlsx")
            report_path = build_report(dirs, report_path)
        except Exception as e:
            _log("[报告] 生成异常: %s" % e)
            report_path = None

    with open(os.path.join(base, "suite.json"), "w", encoding="utf-8") as f:
        json.dump({"ip": ip, "combos": combos, "runs": entries,
                   "report": report_path}, f, ensure_ascii=False, indent=1)
    _log("[完成] 连续续航测试结束，报告: %s" % (report_path or "未生成"))
    prune_runs(protect=[base], log_fn=_log)
    return entries, base, report_path


def render_report(result):
    """生成 Markdown 测试报告。"""
    lines = []
    lines.append("# 续航测试报告 · %s" % result.get("title", ""))
    lines.append("")
    lines.append("## 测试条件")
    lines.append("")
    lines.append("| 项目 | 内容 |")
    lines.append("| --- | --- |")
    lines.append("| 设备 IP | %s |" % result.get("ip", ""))
    lines.append("| SN | %s |" % (result.get("sn") or "—"))
    lines.append("| 固件 | %s |" % (result.get("firmware") or "—"))
    lines.append("| 清扫模式 | %s |" % result.get("mode", ""))
    lines.append("| 吸力挡位 | %s |" % result.get("fan_level", ""))
    lines.append("| 水量挡位 | %s |" % result.get("water_level", ""))
    lines.append("| 低电回充阈值 | %s%% |" % result.get("low_battery", ""))
    if result.get("low_battery_config") is not None:
        lines.append("| 机器人低电配置 | %s%%（start_clean_from_chg_bat_value） |"
                     % result.get("low_battery_config"))
    lines.append("| 开始条件 | 电量 %s%%，%s |"
                 % (result.get("start_battery", ""),
                    result.get("start_status", "")))
    lines.append("")
    lines.append("## 测试结果")
    lines.append("")
    ok = result.get("completed")
    lines.append("- 测试状态：**%s**" % ("完成" if ok else "未完成"))
    lines.append("- 开始时间：%s" % result.get("start_time", ""))
    lines.append("- 结束时间：%s" % result.get("end_time", ""))
    task = result.get("task_seconds") or result.get("wall_seconds", 0)
    lines.append("- **续航时间（开始清扫→低电回充，含转场/重定位/越坎）：%s**"
                 % _fmt_duration(task))
    lines.append("- 回充时电量：%s%%" % result.get("return_battery", ""))
    lines.append("- 回充时状态：%s" % result.get("return_status", ""))
    lines.append("- 提前完成自动重启次数：%s"
                 % result.get("restarts", 0))
    if result.get("water_total_ml"):
        lines.append("- 总出水量：%.2f mL" % float(result["water_total_ml"]))
    if result.get("fan_user_observed") is not None:
        lines.append("- 设备用户挡位（FanLevelUserSet）：%s"
                     % result.get("fan_user_observed"))
    if result.get("fan_level_observed") is not None:
        lines.append("- 设备实际挡位（FanLevelCurrentWork）：%s"
                     % result.get("fan_level_observed"))
    lines.append("")
    ten = result.get("ten_percent") or []
    if ten:
        lines.append("## 每 10% 电量监测")
        lines.append("")
        lines.append("| 电量 | 到达时间 | 距上一档 | 累计用时 | 当时状态 |")
        lines.append("| --- | --- | --- | --- | --- |")
        start_ts = result.get("clean_start_ts") or ten[0]["ts"]
        prev_ts = None
        for item in ten:
            delta = "—"
            if prev_ts is not None:
                delta = _fmt_duration(item["ts"] - prev_ts)
            lines.append("| %d%% | %s | %s | %s | %s |"
                         % (item["level"],
                            datetime.fromtimestamp(item["ts"])
                            .strftime("%H:%M:%S"),
                            delta,
                            _fmt_duration(item["ts"] - start_ts),
                            item.get("status", "")))
            prev_ts = item["ts"]
        lines.append("")
    # 清扫模式全程记录
    mode_changes = result.get("mode_changes") or []
    lines.append("## 清扫模式全程记录")
    lines.append("")
    if mode_changes:
        lines.append("| 时间 | 设备清扫类型 |")
        lines.append("| --- | --- |")
        for c in mode_changes:
            lines.append("| %s | %s |"
                         % (datetime.fromtimestamp(c["ts"])
                            .strftime("%H:%M:%S"), c["value"]))
    else:
        lines.append("- 全程未检测到清扫模式变化（设定 %s）"
                     % result.get("mode", ""))
    lines.append("")
    # 吸力挡位全程记录
    samples = result.get("samples") or []
    speeds = [s.get("fan_speed") for s in samples
              if isinstance(s.get("fan_speed"), (int, float))
              and s.get("fan_speed") > 0]
    lines.append("## 吸力挡位全程记录")
    lines.append("")
    if speeds:
        lines.append("- 风机转速：最低 %d / 最高 %d / 平均 %d rpm（采样 %d 次）"
                     % (min(speeds), max(speeds),
                        sum(speeds) / len(speeds), len(speeds)))
    fan_changes = result.get("fan_changes") or []
    if fan_changes:
        lines.append("")
        lines.append("| 时间 | 类型 | 挡位 |")
        lines.append("| --- | --- | --- |")
        for c in fan_changes:
            lines.append("| %s | %s | %s |"
                         % (datetime.fromtimestamp(c["ts"])
                            .strftime("%H:%M:%S"), c["kind"], c["value"]))
    else:
        lines.append("- 全程未检测到吸力挡位变化（用户挡位 %s）"
                     % result.get("fan_level", ""))
    lines.append("")
    # 水量挡位与出水记录
    lines.append("## 水量挡位与出水记录")
    lines.append("")
    water_changes = result.get("water_changes") or []
    if water_changes:
        lines.append("| 时间 | 类型 | 挡位 |")
        lines.append("| --- | --- | --- |")
        for c in water_changes:
            lines.append("| %s | %s | %s |"
                         % (datetime.fromtimestamp(c["ts"])
                            .strftime("%H:%M:%S"), c["kind"], c["value"]))
        lines.append("")
    water_sets = result.get("water_sets") or []
    if water_sets:
        levels = sorted({(s["level"], s.get("kind") or "设置",
                          s["open_ms"], s["period_ms"])
                         for s in water_sets})
        lines.append("- 水泵参数：" + "；".join(
            "挡位 %d（%s）：出水 %dms / 周期 %dms" % item
            for item in levels))
        lines.append("")
    outflows = result.get("water_outflows") or []
    if outflows:
        lines.append("### 出水脉冲（间隔=挡位固定周期）")
        lines.append("")
        lines.append("| 序号 | 开始时间 | 出水时长 | 距上次出水 | 出水量(mL)"
                     " | 累计(mL) | 挡位 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for i, item in enumerate(outflows, 1):
            dur = ("%.1f 秒" % item["duration_s"]
                   if item.get("duration_s") is not None else "未结束")
            gap = ("%.1f 秒" % item["interval_s"]
                   if item.get("interval_s") is not None else "—")
            ml = ("%.1f" % item["water_ml"]
                  if item.get("water_ml") is not None else "—")
            total = ("%.2f" % item["total_ml"]
                     if item.get("total_ml") is not None else "—")
            lines.append("| %d | %s | %s | %s | %s | %s | %s |"
                         % (i,
                            datetime.fromtimestamp(item["start_ts"])
                            .strftime("%H:%M:%S"),
                            dur, gap, ml, total, item.get("level", "—")))
    else:
        lines.append("- 全程未检测到出水（仅扫模式 / 水量挡位 0）。")
    sessions = result.get("water_sessions") or []
    if sessions:
        lines.append("")
        lines.append("### 连续出水汇总（出水量为设备上报）")
        lines.append("")
        lines.append("| 时间 | 持续 | 出水量(mL) | 累计(mL) | 挡位 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for item in sessions:
            el = ("%.1f 秒" % item["elapsed_s"]
                  if item.get("elapsed_s") is not None else "—")
            ml = ("%.2f" % item["water_ml"]
                  if item.get("water_ml") is not None else "—")
            total = ("%.2f" % item["total_ml"]
                     if item.get("total_ml") is not None else "—")
            lines.append("| %s | %s | %s | %s | %s |"
                         % (datetime.fromtimestamp(item["ts"])
                            .strftime("%H:%M:%S"), el, ml, total,
                            item.get("level", "—")))
    lines.append("")
    faults = result.get("faults") or []
    if faults:
        lines.append("## 故障事件")
        lines.append("")
        lines.append("| 时间 | 故障事件 | 中文含义 | 当时状态 | 设备日志 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for f in faults:
            lines.append("| %s | %s | %s | %s | %s |"
                         % (datetime.fromtimestamp(f["ts"])
                            .strftime("%H:%M:%S"), f.get("event", ""),
                            f.get("name") or event_name(f.get("event", "")),
                            f.get("status", ""),
                            str(f.get("detail", "")).replace("|", "\\|")[:160]))
        lines.append("")
    events = result.get("events") or []
    if events:
        lines.append("## 关键事件")
        lines.append("")
        lines.append("| 时间 | 事件 | 详情 |")
        lines.append("| --- | --- | --- |")
        for e in events[-40:]:
            lines.append("| %s | %s | %s |"
                         % (datetime.fromtimestamp(e["ts"])
                            .strftime("%H:%M:%S"),
                            e.get("name", ""), e.get("detail", "")))
        lines.append("")
    lines.append("## 说明")
    lines.append("")
    lines.append("- 续航时间从开始清扫计到低电回充，包含转场、重定位、越坎等"
                 "任务过程时间，不按挡位拆分。")
    lines.append("- 采样间隔 %s 秒，原始数据见 `%s`。"
                 % (result.get("sample_interval", ""), SAMPLES_NAME))
    lines.append("- 低电阈值取自机器人配置 `start_clean_from_chg_bat_value`"
                 "（当前 20%）。")
    lines.append("- 吸力/水量按 APP 用户挡位下发：`change_fan_level` / "
                 "`change_water_pump` 的载荷字段为 `data`（如 "
                 "`{\"data\":4}`）；挡位变化与出水记录取自设备 7809 调试日志。")
    lines.append("- 机器人进入/退出“transition path”（转场/过坎）时会主动"
                 "短暂 `SetFanLevel: 0`，随后恢复用户挡位，属正常组件控制，"
                 "不计为挡位偏离。")
    return "\n".join(lines) + "\n"


def _write_csv(path, samples):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["时间", "时间戳", "电量", "电池电压(V)", "状态",
                    "累计清扫秒", "风机转速", "风机值", "水量挡位", "正在出水"])
        for s in samples:
            vol = s.get("battery_vol")
            vol = ("%.2f" % vol) if isinstance(vol, (int, float)) else ""
            w.writerow([datetime.fromtimestamp(s["ts"]).strftime(
                "%Y-%m-%d %H:%M:%S"), s["ts"], s["battery"], vol,
                s["status"], int(s.get("clean_seconds", 0)),
                s.get("fan_speed", ""), s.get("fan_value", ""),
                s.get("water_level", ""),
                "是" if s.get("water_on") else "否"])


def run_endurance(ip, mode="sweepOnly", fan_level=4, water_level=0,
                  low_battery=20, sample_interval=15.0,
                  wait_full_timeout=4 * 3600, max_duration=8 * 3600,
                  out_dir=None, log_fn=None, stop_event=None,
                  on_state=None, stream=None,
                  clean_type="total", area=None):
    """执行一次完整续航测试，返回结果 dict 并落盘报告。

    stop_event: threading.Event；GUI 点停止时置位，正常收尾并写部分报告。
    on_state:   callable(dict)，约每秒回调一次实时状态。
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = _abs_out_dir(out_dir or os.path.join("reports", REPORT_SUBDIRS["endurance"], "endurance_%s" % stamp))
    os.makedirs(base, exist_ok=True)
    csv_path = os.path.join(base, SAMPLES_NAME)
    run_log = os.path.join(base, "run.log")

    def _log(msg):
        line = "[%s] %s" % (_now(), msg)
        try:
            with open(run_log, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
        if on_log is not None:
            on_log(msg)

    on_log = log_fn
    log_fn = _log

    # ★ 共用推送会话(用户 2026-11-27): 传入 stream=设备页的流就直接用,
    #   不再另开会话 —— 多个 libapos 推送会话会互相挤掉订阅。
    #   设备页的宽订阅本来就包含下面 4 条路径, 会话活着时无需重订。
    own_stream = stream is None
    st = stream if stream is not None else UploadStream()
    paths = ["/BatteryBattery/BatteryLevel", "/BatteryBattery/BatteryVol",
             "/CleanTask/CleanAppStatus", "/MotorFan/Speed"]
    if own_stream or not getattr(st, "running", False):
        connected = False
        for _ in range(3):
            if st.start(ip, paths):
                connected = True
                break
            time.sleep(1.5)
        if not connected:
            raise RuntimeError("APOS 推送流连接失败: %s" % ip)
    mcu = McuStream()
    try:
        mcu.start(ip)
    except Exception:
        mcu = None
    svc = ParamService(None, stream=st, mcu=mcu)

    # 清扫类型(与循环清扫一致): total / curpoint / rooms / zones
    clean_type = str(clean_type or "total")
    area = list(area or [])
    zone_used = set()
    result = {
        "ip": ip, "mode": mode, "fan_level": fan_level,
        "water_level": water_level, "low_battery": low_battery,
        "sample_interval": sample_interval, "completed": False,
        "restarts": 0, "events": [], "samples": [],
        "clean_type": clean_type, "area": area,
    }
    samples = result["samples"]
    clean_started = False       # 只有真正启动过清扫，结束时才清任务
    result["start_time"] = _now()
    result["title"] = "%s · 吸力%s · %s" % (
        {"sweepOnly": "仅扫模式", "sweepMop": "扫拖模式",
         "mopOnly": "单拖模式",
         "sweepThenMop": "先扫后拖模式"}.get(mode, mode),
        fan_level, _now()[:10])

    def event(name, detail=""):
        e = {"ts": time.time(), "name": name, "detail": str(detail)}
        result["events"].append(e)
        log_fn("[事件] %s %s" % (name, detail))

    def stop_requested():
        return stop_event is not None and stop_event.is_set()

    def start_clean():
        """按清扫类型下发（与循环清扫同一套链路）。

        选区 → StartSegmentClean(FILL_TYPE_AUTO, 机器人房间分段)
        划区 → SetAreaInfo 临时区域 + StartAreaClean(FILL_TYPE_AREA)
        定点 → 该点 1.5m 见方临时区域 + StartAreaClean(扫 2 遍)
        其它 → 全局清扫
        """
        if clean_type == "rooms":
            ok = svc.start_segment_clean(area)
            if not ok:
                log_fn("[启动] 选区启动失败（房间 %s）" % area)
            return ok
        if clean_type in ("zones", "point"):
            from core.loop_clean import point_area_entry, zone_area_entries
            if clean_type == "point":
                entry = point_area_entry(area)
                entries = [entry] if entry else []
            else:
                entries = zone_area_entries(area)
            if not entries:
                log_fn("[启动] %s没有有效区域"
                       % ("定点" if clean_type == "point" else "划区"))
                return False
            if not svc.set_area_info(entries):
                log_fn("[启动] 区域信息写入失败")
                return False
            zone_used.add("1")
            ok = svc.start_area_clean([e["id"] for e in entries])
            if not ok:
                log_fn("[启动] %s启动失败"
                       % ("定点" if clean_type == "point" else "划区"))
            return ok
        if clean_type == "curpoint":
            return svc.start_curpoint_clean()
        return svc.start_total_clean()

    last_state = [0.0]

    def emit_state(battery, status, clean_seconds=0.0, fan=None, water=None):
        if on_state is None:
            return
        now = time.time()
        if now - last_state[0] < 1.0:
            return
        last_state[0] = now
        try:
            on_state({
                "battery": battery, "status": status,
                "clean_seconds": clean_seconds,
                "fan_level": fan_level, "water_level": water_level,
                "fan_current": fan, "water_current": water,
                "water_total_ml": result.get("water_total_ml") or 0,
                "outflows": len(result.get("water_outflows") or []),
                "faults": len(result.get("faults") or []),
                "restarts": result.get("restarts", 0),
                "completed": bool(result.get("completed")),
            })
        except Exception:
            pass

    try:
        # 读设备信息（拿不到不影响测试）
        try:
            info = st.read(["/System/CustomSN", "/System/FirmwareInfo"],
                           timeout_ms=2000) or {}
            result["sn"] = info.get("/System/CustomSN") or ""
            fw = info.get("/System/FirmwareInfo")
            if isinstance(fw, str):
                fw = json.loads(fw)
            if isinstance(fw, dict):
                result["firmware"] = "%s %s" % (
                    fw.get("Model", ""), fw.get("Version", ""))
        except Exception:
            pass

        # 机器人当前的低电回充阈值（配置 start_clean_from_chg_bat_value）
        try:
            raw = st.read_file(
                "/data/bin/app/config/task_node_clean.json") or ""
            cfg = json.loads(raw)
            value = (cfg.get("property_set") or {}).get(
                "start_clean_from_chg_bat_value")
            if value is not None:
                result["low_battery_config"] = int(value)
                if int(value) != int(low_battery):
                    log_fn("[警告] 机器人低电阈值配置为 %s%%，与测试参数 %s%% "
                           "不一致" % (value, low_battery))
        except Exception:
            pass

        # 1) 等满电
        log_fn("[准备] 等待满电（最多 %d 分钟）…" % (wait_full_timeout // 60))
        full_deadline = time.time() + wait_full_timeout
        last_note = 0
        ka = {"last_restart": 0.0}
        charge_attempts = 0
        next_charge_try = 0.0
        while True:
            now = time.time()
            battery = _battery(st)
            status = _status(st)
            _keepalive(st, ka, log_fn, ip, paths)
            if stop_requested():
                result["failure_reason"] = "manual_stop"
                raise _EnduranceAbort("用户停止")
            emit_state(battery, status, 0.0)
            if now > full_deadline:
                raise RuntimeError("等待满电超时（当前电量：%s，状态：%s）"
                                   % (_pct_text(battery), status or "未知"))
            action = charge_decision(battery, status, charge_attempts,
                                     now, next_charge_try)
            if action == "ready":
                break
            if action == "fail":
                result["failure_reason"] = "not_charging"
                result["charge_attempts"] = charge_attempts
                raise _EnduranceAbort(
                    "未进入充电状态（已尝试回充 %d 次，当前电量：%s，"
                    "状态：%s），测试失败"
                    % (charge_attempts, _pct_text(battery),
                       status or "未知"))
            if action == "attempt":
                charge_attempts += 1
                result["charge_attempts"] = charge_attempts
                if status in CLEAN_STATUSES:
                    event("停止清扫", "准备回充")
                    svc.stop_clean()
                    time.sleep(2)
                event("尝试回充", "第 %d/3 次（当前电量：%s，状态：%s）"
                      % (charge_attempts, _pct_text(battery),
                         status or "未知"))
                svc.start_find_charger()
                next_charge_try = now + 60
            if now - last_note > 60:
                last_note = now
                log_fn("[准备] 当前电量：%s，状态：%s，继续等待…"
                       % (_pct_text(battery), status or "未知"))
            for _ in range(10):
                if stop_requested():
                    break
                time.sleep(1)
        result["start_battery"] = _battery(st)
        result["start_status"] = _status(st)
        event("满电就绪", "%s%% %s" % (result["start_battery"],
                                       result["start_status"]))
        if result["start_status"] in TASK_ACTIVE_STATUSES:
            # 满电但已有清扫/暂停任务：先停掉、转空闲，再开一个干净的测试任务
            # （与循环清扫一致，用户 2026-11-28：有任务必须先转空闲再下发）
            result["restarted_task"] = True
            event("重新开始清扫", "满电但已有清扫/暂停任务（%s），先停止再新开"
                  % result["start_status"])
            svc.stop_clean()
            time.sleep(1)
            if _status(st) in TASK_ACTIVE_STATUSES:
                svc.post_event("/event/task_manager", "kill_all_nodes")
            svc.post_event("/event/task_node", "clear_last_clean_id")
            idle_end = time.time() + 30
            while (time.time() < idle_end and not stop_requested()
                   and _status(st) in TASK_ACTIVE_STATUSES):
                time.sleep(1)
            event("转空闲完成", "当前状态 %s" % (_status(st) or "未知"))

        # 2) 先设清扫模式并启动；吸力/水量必须等清扫真正开始后再下发
        #    （用户 2026-11-24：开始清扫后就要设置吸力挡位）
        svc.set_clean_mode(mode)
        time.sleep(1)
        start_clean()
        clean_started = True
        starts_sent = 1
        last_start_ts = time.time()
        event("启动清扫", "mode=%s fan=%s water=%s type=%s"
              % (mode, fan_level, water_level, clean_type))

        applied = {"mode": None, "fan": None, "water": None}

        def apply_settings(only=None):
            """下发用户挡位；默认只发变化过的通道，避免重复下发。

            重复下发会把 APP/云端的用户挡位又顶一遍，也容易和 APP 抢状态；
            only=("fan",) 这类用于观测到偏离后的定点恢复。
            """
            plan = plan_settings(applied, mode, fan_level, water_level, only)
            if not plan:
                return True
            # ★ 先确认推送通道还活着：静默会话上 SendEvent 会"假成功"，
            #   挡位实际没变（用户 2026-11-26：APP 切 1 档后重发 4 档不生效）。
            if not _stream_fresh(st):
                try:
                    rebuilt = bool(st.start(ip, paths, preserve=True))
                except Exception:
                    rebuilt = False
                ka["since"] = time.time()
                ka["next_try"] = 0.0
                log_fn("[推送] 下发前数据流%s" % (
                    "已重建" if rebuilt else "不可用（本次下发可能不生效）"))
            oks = {}
            if "mode" in plan:
                oks["mode"] = bool(svc.set_clean_mode(mode))
            if "water" in plan:
                oks["water"] = bool(svc.set_water_level(water_level))
            if "fan" in plan:
                oks["fan"] = bool(svc.set_fan_level(fan_level))
            time.sleep(2)
            sp = st.values().get("/MotorFan/Speed")
            for ch, ok in oks.items():
                if ok:
                    applied[ch] = plan[ch]
            ok_all = all(oks.values())
            names = {"mode": "模式", "fan": "吸力", "water": "水量"}
            desc = " ".join("%s=%s" % (names[ch], plan[ch])
                            for ch in ("mode", "fan", "water") if ch in plan)
            log_fn("[设置] 下发 %s（当前转速 %s%s）"
                   % (desc, sp, "" if ok_all else "，下发失败"))
            return ok_all

        # 3) 主循环
        t0 = None
        segment_start = None
        clean_seconds = 0.0
        last_status = ""
        last_sample = 0.0
        last_reassert = 0.0
        restart_guard = 0.0
        start_fail_streak = 0
        wash_since = None          # 拖地回站洗拖布的开始时间
        wash_timeout_noted = False
        wash_signal_ts = 0.0       # 设备日志里最近一次回洗拖布信号
        wash_continue = False      # 回洗后进入充电/烘干，紧接着要重启清扫
        return_battery = None
        end_reason = ""
        last_mcu_ts = time.time()
        last_pulse_ts = 0.0        # 上一次水泵开泵时间（算周期间隔）
        open_pulse = None          # 当前正在开的这一次泵（周期脉冲）
        fan_obs = None
        fan_bad_since = None
        water_cur = None
        water_bad_since = None
        water_user = None
        water_on = False
        mode_obs = None
        mode_bad_since = None
        in_transition = False
        reloc_active = False
        last_reloc_ts = 0.0
        location_ok = False       # 收到「定位成功，继续清扫」(clean_location_success)
        initial_settings_sent = False
        result["fan_changes"] = []
        result["water_changes"] = []
        result["water_outflows"] = []
        result["water_sets"] = []
        result["mode_changes"] = []
        result["water_total_ml"] = 0
        result["faults"] = []

        def fault_summary(limit=6):
            events = []
            for f in (result.get("faults") or [])[-limit:]:
                if f["event"] not in events:
                    events.append(f["event"])
            return "、".join(events) if events else "未捕获到故障事件"

        while True:
            now = time.time()
            reconnected = _keepalive(st, ka, log_fn, ip, paths)
            battery = _battery(st)
            status = _status(st)
            cleaning = status in CLEAN_STATUSES

            # 拖地模式中途会回站洗拖布：这个 back_charge/wash_mop 不是任务完成，
            # 也不是低电回充，等机器人自动继续清扫；超过等待上限才按提前完成处理。
            if cleaning:
                if wash_since is not None and t0 is not None:
                    event("回洗完成", "已自动继续清扫（回站 %s）"
                          % _fmt_duration(now - wash_since))
                    wash_since = None
                    wash_timeout_noted = False
                    apply_settings()   # 回洗后确认挡位还生效（没变化就不重发）
            elif t0 is not None and is_wash_wait(mode, status):
                if wash_since is None:
                    wash_since = now
                    if status == "work_status_wash_mop":
                        why = "正在洗拖布"
                    elif wash_signal_ts and now - wash_signal_ts <= 180:
                        why = "设备事件回洗拖布"
                    elif (battery > low_battery + 10
                          and (water_level > 0 or water_on)):
                        why = "拖地中途回站洗拖布（推断）"
                    else:
                        why = "回站（原因待确认）"
                    result.setdefault("wash_returns", []).append(
                        {"ts": now, "reason": why, "battery": battery,
                         "status": status})
                    event("回站洗拖布",
                          "%s，当前电量 %s，状态 %s，等待自动继续清扫"
                          % (why, _pct_text(battery), status or "未知"))
                elif (now - wash_since > WASH_RESUME_TIMEOUT
                      and not wash_timeout_noted):
                    wash_timeout_noted = True
                    event("回洗等待超时",
                          "已等待 %d 分钟，按提前完成处理"
                          % (WASH_RESUME_TIMEOUT // 60))
            elif (t0 is not None and wash_since is not None
                  and status in RETURN_DONE_STATUSES):
                # 回站后已经进入充电/烘干：回洗结束，立即重新下发清扫任务
                event("回洗完成",
                      "进入 %s，重新下发清扫任务" % (status or "未知"))
                wash_continue = True
                wash_since = None
                wash_timeout_noted = False
            else:
                wash_since = None
                wash_timeout_noted = False

            if stop_requested():
                result["failure_reason"] = "manual_stop"
                end_reason = "用户停止"
                break
            if reconnected and cleaning:
                apply_settings()
            emit_state(battery, status,
                       clean_seconds + ((now - segment_start)
                                        if segment_start else 0),
                       fan_obs, water_cur)

            # 从 7809 调试日志记录全程挡位/出水数据
            if mcu is not None:
                for _t, line in mcu.log_since(last_mcu_ts):
                    # 回洗拖布信号：任务中回洗/回洗拖布相关事件（不同固件叫法
                    # 可能是 go_back_mop_after_clean / back_wash / wash_mop…）
                    low = line.lower()
                    ts = _line_ts(line, now, ka)
                    if any(k in low for k in ("go_back_mop", "back_wash",
                                              "wash_mop")):
                        wash_signal_ts = now
                    if "clean_location_success" in line:
                        location_ok = True
                    if "Enter transition path type" in line:
                        in_transition = True
                        continue
                    if "Exit transition path type" in line:
                        in_transition = False
                        continue
                    # 重定位窗口：用于给 SetFanLevel:0 标注原因
                    if ("OnRelocation ---" in line
                            or "Relocation ---------------------------" in line
                            or "OnRelocaWaitLocaRet" in line):
                        reloc_active = True
                        last_reloc_ts = now
                    if ("relocation success" in line
                            or "relocation fail" in line
                            or "relocation time out" in line
                            or "Relocation---------" in line):
                        reloc_active = False
                    # 故障事件：同一事件 5 秒内只记一次（一条故障会打多行日志）
                    fm = re.search(
                        r"(?:Recv event_id\s+|tpc:/hardware/error id:"
                        r"|event_id:\s*|event:)([a-z][a-z0-9_]+)", line)
                    if fm and is_fault_event(fm.group(1)):
                        ev = fm.group(1)
                        items = result["faults"]
                        if not (items and items[-1]["event"] == ev
                                and now - items[-1]["ts"] < 5):
                            try:
                                cn = event_name(ev) or ev
                            except Exception:
                                cn = ev
                            items.append({"ts": ts, "event": ev, "name": cn,
                                          "status": status,
                                          "detail": line.strip()[:200]})
                            event("故障事件", ev)
                    m = re.search(r"SetFanLevel:\s*(\d+)", line)
                    if m:
                        val = int(m.group(1))
                        # 0 档要区分是转场还是重定位主动关风机；
                        # 只记录，不作为“用户挡位偏离”依据。
                        if val == 0 and in_transition:
                            kind = "转场"
                        elif val == 0 and (
                                reloc_active
                                or (last_reloc_ts
                                    and now - last_reloc_ts <= 30)):
                            kind = "重定位"
                        else:
                            # 不写笼统的“设备设置”，按当时状态标注（清扫中/回充中…）
                            kind = _status_label(status)
                        result["fan_changes"].append(
                            {"ts": ts, "kind": kind, "value": val})
                    m = re.search(r'"FanLevelCurrentWork"\s*:\s*(\d+)', line)
                    if m:
                        val = int(m.group(1))
                        if val != fan_obs:
                            fan_obs = val
                            result["fan_level_observed"] = val
                            result["fan_changes"].append(
                                {"ts": ts, "kind": "当前挡位", "value": val})
                            event("设备挡位", "FanLevelCurrentWork=%d" % val)
                        continue
                    m = re.search(r'"FanLevelUserSet"\s*:\s*(\d+)', line)
                    if m:
                        val = int(m.group(1))
                        if val != result.get("fan_user_observed"):
                            result["fan_user_observed"] = val
                            result["fan_changes"].append(
                                {"ts": ts, "kind": "用户挡位", "value": val})
                            event("用户挡位", "FanLevelUserSet=%d" % val)
                        continue
                    m = re.search(r'"WaterLevelCurrentWork"\s*:\s*(\d+)',
                                  line)
                    if m:
                        val = int(m.group(1))
                        if val != water_cur:
                            water_cur = val
                            result["water_level_observed"] = val
                            if val == 0 and in_transition:
                                kind = "转场"
                            elif val == 0 and (
                                    reloc_active
                                    or (last_reloc_ts
                                        and now - last_reloc_ts <= 30)):
                                kind = "重定位"
                            else:
                                kind = _status_label(status)
                            result["water_changes"].append(
                                {"ts": ts, "kind": kind, "value": val})
                        continue
                    m = re.search(r'"WaterLevelUserSet"\s*:\s*(\d+)', line)
                    if m:
                        val = int(m.group(1))
                        if val != water_user:
                            water_user = val
                            result["water_changes"].append(
                                {"ts": ts, "kind": "用户挡位", "value": val})
                        continue
                    m = re.search(
                        r"SetWaterPump\s+Level:\s*(\d+)\s+open_time:\s*"
                        r"(\d+)ms\s+period:\s*(\d+)ms", line)
                    if m:
                        result["water_sets"].append(
                            {"ts": ts, "level": int(m.group(1)),
                             "open_ms": int(m.group(2)),
                             "period_ms": int(m.group(3)),
                             "kind": "设置"})
                        continue
                    # 实际工作周期（湿拖循环用）：period_open_time/period_time
                    m = re.search(
                        r"period_open_time:(\d+)ms\s+period_time:(\d+)ms",
                        line)
                    if m:
                        result["water_sets"].append(
                            {"ts": ts,
                             "level": water_cur if water_cur is not None
                             else water_user,
                             "open_ms": int(m.group(1)),
                             "period_ms": int(m.group(2)),
                             "kind": "实际"})
                        continue
                    m = re.search(r"SetCleanType:\s*(CLEAN_TYPE_[A-Z_]+)",
                                  line)
                    if not m:
                        m = re.search(
                            r'"CleanType"\s*:\s*"(CLEAN_TYPE_[A-Z_]+)"', line)
                    if m:
                        val = m.group(1)
                        if val != mode_obs:
                            mode_obs = val
                            result["mode_changes"].append(
                                {"ts": ts, "value": val})
                            event("清扫模式", val)
                        continue
                    if "Water pump started" in line:
                        # 只是"开始长时间出水"（会话开始），真正的出水脉冲
                        # 由下面的 period_2_open/close_pump 记录
                        water_on = True
                        continue
                    # 水泵周期脉冲：湿拖时按挡位固定节拍开/关泵（实测 3 秒一档）
                    m = re.search(
                        r"period_2_open_pump\s+level\s*=\s*:\s*(\d+)", line)
                    if m:
                        lvl = int(m.group(1))
                        if lvl <= 0:
                            continue
                        interval = (ts - last_pulse_ts) if last_pulse_ts \
                            else None
                        last_pulse_ts = ts
                        rec = {"start_ts": ts, "stop_ts": None,
                               "duration_s": None, "interval_s": interval,
                               "water_ml": None, "total_ml": None,
                               "level": lvl}
                        result["water_outflows"].append(rec)
                        open_pulse = rec
                        continue
                    if "period_2_close_pump" in line:
                        if open_pulse is not None:
                            open_pulse["stop_ts"] = ts
                            open_pulse["duration_s"] = round(
                                max(0.0, ts - open_pulse["start_ts"]), 3)
                            open_pulse = None
                        water_on = False
                        continue
                    pm = re.search(
                        r"UpdateWaterVolume[^\n]*?increment:([\d.]+)ml"
                        r"[^\n]*?shm:(\d+)ml->(\d+)ml", line)
                    if pm:
                        try:
                            inc = float(pm.group(1))
                            tot = int(pm.group(3))
                        except ValueError:
                            continue
                        if tot >= 0:
                            result["water_total_ml"] = max(
                                int(result.get("water_total_ml") or 0), tot)
                        em = re.search(r"elapsed:(\d+)ms", line)
                        result.setdefault("water_sessions", []).append({
                            "ts": ts,
                            "water_ml": round(inc, 2),
                            "total_ml": tot,
                            "elapsed_s": (int(em.group(1)) / 1000.0
                                          if em else None),
                            "level": water_cur if water_cur is not None
                            else water_user})
                        if "Water pump closed" in line:
                            water_on = False
                        continue
                last_mcu_ts = now

            if t0 is not None and cleaning and fan_obs is not None \
                    and fan_obs != fan_level and not in_transition:
                if fan_bad_since is None:
                    fan_bad_since = now
                elif now - fan_bad_since > 30 and now - last_reassert > 30:
                    event("挡位偏离", "当前 %d 档，重发用户挡位 %d"
                          % (fan_obs, fan_level))
                    apply_settings(only=("fan",))
                    fan_bad_since = None
            else:
                fan_bad_since = None

            # 水量偏离：APP/机器人转场后可能按 user_set 恢复成旧挡位，
            # 本地用户挡位要重新下发（扫拖/单拖才校验，仅扫水量本来就是 0）
            if (t0 is not None and cleaning and water_cur is not None
                    and water_level > 0 and water_cur != water_level
                    and not in_transition):
                if water_bad_since is None:
                    water_bad_since = now
                elif now - water_bad_since > 30 and now - last_reassert > 30:
                    event("水量偏离", "当前 %d 档，重发用户挡位 %d"
                          % (water_cur, water_level))
                    apply_settings(only=("water",))
                    water_bad_since = None
            else:
                water_bad_since = None

            expect_type = MODE_TO_CLEAN_TYPE.get(mode)
            if (t0 is not None and cleaning and mode_obs is not None
                    and expect_type and mode_obs != expect_type
                    and not in_transition):
                if mode_bad_since is None:
                    mode_bad_since = now
                elif now - mode_bad_since > 30 and now - last_reassert > 30:
                    event("清扫模式偏离", "当前 %s，重发 %s"
                          % (mode_obs, expect_type))
                    apply_settings(only=("mode",))
                    mode_bad_since = None
            else:
                mode_bad_since = None

            if status != last_status:
                last_status = status
                event("状态变化", status)
                if cleaning and t0 is None:
                    t0 = now
                    start_fail_streak = 0
                    result["clean_start_ts"] = now
                    event("开始计时", "实际清扫开始")
                    # 续航监测从清扫开始这一刻起记录（用户 2026-11-24）
                    samples.append({"ts": now, "battery": battery,
                                    "battery_vol": _battery_vol(st),
                                    "status": status, "clean_seconds": 0,
                                    "fan_speed": st.values().get(
                                        "/MotorFan/Speed"),
                                    "fan_value": (mcu.values() or {}).get(
                                        "/Mcu/FanValue")
                                    if mcu is not None else None,
                                    "water_level": water_cur
                                    if water_cur is not None else water_user,
                                    "water_on": water_on})
                    _write_csv(csv_path, samples)
                    result["start_battery"] = battery if battery >= 0 else \
                        result.get("start_battery")
                    last_sample = now

            # ★ 吸力/水量必须等机器人「定位成功，继续清扫」后再下发；
            #   若设备日志里没有该事件(旧固件/无 MCU 流)，15 秒后兜底下发。
            if (t0 is not None and not initial_settings_sent and cleaning
                    and (location_ok or now - t0 > 15)):
                initial_settings_sent = True
                event("定位成功", "开始下发吸力/水量")
                apply_settings()

            if cleaning and segment_start is None:
                segment_start = now
            elif not cleaning and segment_start is not None:
                clean_seconds += now - segment_start
                segment_start = None
                event("暂停计时", "累计清扫 %s"
                      % _fmt_duration(clean_seconds))

            # 启动没生效 → 每 60 秒重发；连续 3 次启动失败则终止测试
            if t0 is None and now - last_start_ts > 60:
                start_fail_streak += 1
                if start_fail_streak >= 3:
                    result["failure_reason"] = "start_failed"
                    raise _EnduranceAbort(
                        "连续 3 次启动清扫失败（故障事件：%s）"
                        % fault_summary())
                starts_sent += 1
                last_start_ts = now
                event("重发启动清扫", "第 %d 次（连续失败 %d）"
                      % (starts_sent, start_fail_streak))
                svc.set_clean_mode(mode)
                start_clean()

            accum = clean_seconds + ((now - segment_start)
                                     if segment_start else 0)

            # 采样（只在清扫开始后记录，直到低电结束）；推送未就绪(电量 -1)
            # 时不落盘无效采样，避免报告里出现 -1% 的数据点
            if (t0 is not None and battery >= 0
                    and now - last_sample >= sample_interval):
                last_sample = now
                fan_speed = st.values().get("/MotorFan/Speed")
                fan_value = (mcu.values() or {}).get("/Mcu/FanValue") \
                    if mcu is not None else None
                samples.append({"ts": now, "battery": battery,
                                "battery_vol": _battery_vol(st),
                                "status": status, "clean_seconds": accum,
                                "fan_speed": fan_speed,
                                "fan_value": fan_value,
                                "water_level": water_cur
                                if water_cur is not None else water_user,
                                "water_on": water_on})
                _write_csv(csv_path, samples)
                if len(samples) % 8 == 1:      # 每 2 分钟输出一次进度
                    log_fn("[进度] %s 当前电量：%s，状态：%s，累计清扫：%s"
                           % (_now(), _pct_text(battery), status or "未知",
                              _fmt_duration(accum)))

            # 清扫中周期性兜底：只有观测值偏离才重发对应通道（未偏离不发）
            if cleaning and now - last_reassert > 120:
                last_reassert = now
                if not in_transition:
                    if fan_obs is not None and fan_obs != fan_level:
                        apply_settings(only=("fan",))
                    if (water_cur is not None and water_level > 0
                            and water_cur != water_level):
                        apply_settings(only=("water",))

            # 低电回充结束：电量到阈值且已离开清扫状态
            if t0 is not None and not cleaning and battery >= 0 \
                    and battery <= low_battery:
                return_battery = battery
                end_reason = "低电回充（%s%%）" % battery
                result["return_status"] = status
                result["return_time"] = now
                samples.append({"ts": now, "battery": battery,
                                "battery_vol": _battery_vol(st),
                                "status": status, "clean_seconds": accum,
                                "fan_speed": st.values().get(
                                    "/MotorFan/Speed"),
                                "fan_value": (mcu.values() or {}).get(
                                    "/Mcu/FanValue")
                                if mcu is not None else None,
                                "water_level": water_cur
                                if water_cur is not None else water_user,
                                "water_on": water_on})
                _write_csv(csv_path, samples)
                break

            # 提前完成/故障 → 电量充足时继续清扫（不能回基站充电）
            # 拖地回站洗拖布期间除外：等它洗完自动继续，别把任务重启掉
            if (t0 is not None and not cleaning
                    and (status in RESTART_STATUSES
                         or status == "work_status_error")
                    and battery > low_battery + 2
                    and now - restart_guard > 10
                    and not (wash_since is not None
                             and not wash_timeout_noted)):
                restart_guard = now
                result["restarts"] += 1
                if status == "work_status_error":
                    # 不清故障：清故障状态等于把当前任务也清掉（用户 2026-11-26）
                    # 直接重新下发清扫，让机器人自己恢复后继续跑
                    event("清扫故障恢复", "%s%% 状态 %s，直接继续清扫"
                          % (battery, status))
                else:
                    if wash_continue:
                        event("回洗完成继续清扫", "%s%% %s" % (battery, status))
                    else:
                        event("提前完成重启清扫", "%s%% %s" % (battery, status))
                wash_continue = False
                svc.set_clean_mode(mode)
                start_clean()
                ok = False
                end = time.time() + 15
                while time.time() < end:
                    if _status(st) in CLEAN_STATUSES:
                        ok = True
                        break
                    time.sleep(0.5)
                if ok:
                    start_fail_streak = 0
                    location_ok = False
                    initial_settings_sent = False
                    event("重启清扫成功", "%s%%" % _battery(st))
                else:
                    start_fail_streak += 1
                    event("重启清扫失败", "连续 %d 次，状态 %s"
                          % (start_fail_streak, _status(st)))
                    if start_fail_streak >= 3:
                        result["failure_reason"] = "start_failed"
                        raise _EnduranceAbort(
                            "连续 3 次启动清扫失败（故障事件：%s）"
                            % fault_summary())
                now = time.time()

            # 单组合超时：从实际开始清扫计时（等满电的时间不算，另有 4 小时上限）
            if t0 is not None and now - t0 > max_duration:
                end_reason = "测试超时（%d 小时）" % (max_duration // 3600)
                result["return_status"] = status
                result["return_time"] = now
                result["return_battery"] = battery
                break

            time.sleep(1.0)

        if segment_start is not None:
            clean_seconds += time.time() - segment_start
            segment_start = None
        result["clean_seconds"] = clean_seconds
        task_seconds = time.time() - t0 if t0 else 0
        result["wall_seconds"] = task_seconds
        result["task_seconds"] = task_seconds
        result["return_battery"] = (return_battery if return_battery is not None
                                    else result.get("return_battery"))
        result["completed"] = end_reason.startswith("低电回充")
        result["end_reason"] = end_reason
        result["end_time"] = _now()
        result["ten_percent"] = compute_ten_percent(
            samples, result.get("start_battery", 100), low_battery)
        event("测试结束", end_reason)
    except _EnduranceAbort as e:
        result["completed"] = False
        result["end_reason"] = str(e)
        result["end_time"] = _now()
        try:
            event("测试失败", e)
        except Exception:
            pass
        log_fn("[失败] %s" % e)
    except Exception as e:
        result["completed"] = False
        result["end_reason"] = "异常: %s" % e
        result["end_time"] = _now()
        try:
            event("异常", e)
        except Exception:
            pass
        log_fn("[异常] %s" % e)
    finally:
        # 任务结束清理：停止清扫 + 清掉最后清扫点，避免充电后触发断点续扫，
        # 影响下一次续航测试（用户 2026-11-25）。
        if clean_started:
            try:
                svc.stop_clean()
                time.sleep(1)
                # 只杀还没退出的清扫节点；不要 kill_all_nodes，
                # 否则会把低电回充/充电节点一起停掉
                if _status(st) in CLEAN_STATUSES:
                    svc.post_event("/event/task_manager", "kill_all_nodes")
                svc.post_event("/event/task_node", "clear_last_clean_id")
                log_fn("[清理] 已停止并清除当前清扫任务（防断点续扫）")
            except Exception as e:
                log_fn("[清理] 清除任务失败: %s" % e)
        if zone_used:
            # 划区写的是 areaValue(手动区域)，结束后清掉，避免残留在 APP 地图上
            try:
                if svc.clear_area_info():
                    log_fn("[清理] 已清除临时划区")
            except Exception as e:
                log_fn("[清理] 清除临时划区失败: %s" % e)
        if own_stream:
            st.stop()          # 共享流不归本函数管, 交给设备页
        if mcu is not None:
            mcu.stop()

    result.setdefault("clean_seconds", 0)
    result.setdefault("wall_seconds", 0)
    result.setdefault("task_seconds", result.get("wall_seconds", 0))
    result.setdefault("return_battery", None)
    result.setdefault("return_status", "")
    result.setdefault("end_time", _now())
    result.setdefault("ten_percent", compute_ten_percent(
        samples, result.get("start_battery", 100), low_battery))

    with open(os.path.join(base, "result.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    with open(os.path.join(base, "report.md"), "w", encoding="utf-8") as f:
        f.write(render_report(result))
    log_fn("[完成] 报告: %s" % os.path.join(base, "report.md"))
    prune_runs(protect=[base], log_fn=log_fn)
    return result, base
