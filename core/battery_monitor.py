# -*- coding: utf-8 -*-
"""电量监测: 运行期间按固定间隔采样电量, 统计「每下降 N% 用了多久」。

★ 设计(用户 2026-11-03 确认):
  · 运行条勾选后启动, 从本轮第一条用例开始采样, 整轮结束/手动停止时停止;
  · 独立线程, **共用执行线程的 uiautomator2 设备对象** —— 不新建连接, 避免与点击/
    截图抢资源; 设备端请求本就是串行处理, 采样时最多让一次操作多等几百毫秒;
  · 报告在原有 Excel 里新增一页「电量监测」, 同一页分**汇总**与**详细**两段。

★ 这个模块只负责「采样 + 纯统计」, 不 import PySide6 / openpyxl —— 纯函数可直接单测。
  报告排版在 core/excel_report.py。
"""
import threading
import time
from dataclasses import dataclass


@dataclass
class BatterySample:
    """一次采样。battery = -1 表示这一轮读不到(页面没展示电量/设备抖动)。"""
    t: float                       # 相对监测开始的时间(秒)
    battery: int                   # 机器人电量百分比; -1 = 读不到
    label: str = ""                # 当时的「用例名(第N次)」
    wall: str = ""                 # 采样时刻的本地时间 "HH:MM:SS"(用户 2026-11-04)
    voltage_v: float | None = None  # 电池电压(V); None = 本轮未取到
    temp_c: float | None = None     # 电池温度(℃); None = 本轮未取到
    source: str = ""                # "param"=扫地机参数, "app"=APP 兜底


def format_mmss(seconds):
    """秒 → "分:秒"(两位秒); 无效值给 "--:--"。"""
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return "--:--"
    if s < 0:
        return "--:--"
    s = int(round(s))
    return f"{s // 60:02d}:{s % 60:02d}"


def _valid(samples):
    return [s for s in samples if getattr(s, "battery", -1) >= 0]


def build_points(samples, step_pct=5):
    """原始变化点 + N% 里程碑。

    返回 dict:
      valid / invalid: 有效/无效采样数
      start_battery / end_battery: 首/末有效电量(没有有效样本时 None)
      raw:       [{"t","battery","label","delta","dt"}]  —— 每次电量实际变化一行
      milestones:[{"t","battery","label","target","dt","from_battery"}]
                 —— 每降 N% 一行; from_battery = 该段起点电量

    ★ 里程碑锚点 = **相对监测起点的累计下降量**(起点 87%、N=5 → 82/77/…)，
      起点不是整十也不影响。
    ★ 一次采样跨过多个阈值时只记一行(实际电量 + 跨过的目标)，下一锚点从实际电量
      重算 —— 否则会出现好几条 dt=0 的噪声行。
    ★ 电量回升(回充/充电)时**重新锚定**(代码审查 2026-11-04): 充电这一段时间不属于
      任何一个下降段, 否则"最长/最短 N% 段"会把充电的几十分钟算进某一段, 结论完全失真。
      所以回升时 next_thr / prev_t 一起重置, 后续从回升后的电量重新算。
    """
    step = max(1, int(step_pct or 1))
    vals = _valid(samples)
    out = {"valid": len(vals), "invalid": len(samples) - len(vals),
           "start_battery": None, "end_battery": None,
           "raw": [], "milestones": []}
    if not vals:
        return out

    out["start_battery"] = vals[0].battery
    out["end_battery"] = vals[-1].battery
    out["start_voltage"] = next(
        (s.voltage_v for s in vals if getattr(s, "voltage_v", None) is not None), None)
    out["end_voltage"] = next(
        (s.voltage_v for s in reversed(vals)
         if getattr(s, "voltage_v", None) is not None), None)
    out["start_temp"] = next(
        (s.temp_c for s in vals if getattr(s, "temp_c", None) is not None), None)
    out["end_temp"] = next(
        (s.temp_c for s in reversed(vals)
         if getattr(s, "temp_c", None) is not None), None)

    prev = vals[0]
    for s in vals[1:]:
        if s.battery != prev.battery:
            out["raw"].append({
                "t": s.t, "battery": s.battery, "label": s.label,
                "wall": s.wall,
                "voltage_v": getattr(s, "voltage_v", None),
                "temp_c": getattr(s, "temp_c", None),
                "delta": s.battery - prev.battery, "dt": max(0.0, s.t - prev.t),
            })
            prev = s

    start = vals[0]
    next_thr = start.battery - step
    prev_t = start.t
    anchor_b = start.battery           # 当前下降段的起点电量
    for s in vals[1:]:
        if s.battery > anchor_b:        # 回升 = 充电: 重置锚点, 不把充电时间算进任何一段
            anchor_b = s.battery
            prev_t = s.t
            next_thr = s.battery - step
            continue
        if s.battery <= next_thr:
            out["milestones"].append({
                "t": s.t, "battery": s.battery, "label": s.label,
                "wall": s.wall,
                "target": next_thr, "dt": max(0.0, s.t - prev_t),
                "from_battery": anchor_b,
            })
            prev_t = s.t
            anchor_b = s.battery
            next_thr = s.battery - step
    return out


def build_summary(samples, step_pct=5):
    """汇总: 全局指标 + 按「用例/轮次」分段。

    average_per_1pct: 只在**确实有下降**时给值, 充电(电量上升)时为 None。
    """
    pts = build_points(samples, step_pct)
    vals = _valid(samples)
    out = {
        "valid": pts["valid"], "invalid": pts["invalid"],
        "start_battery": pts["start_battery"], "end_battery": pts["end_battery"],
        "start_voltage": None, "end_voltage": None,
        "start_temp": None, "end_temp": None,
        "total_seconds": 0.0, "total_drop": 0,
        "average_per_1pct": None, "milestone_average": None,
        "milestone_longest": None, "milestone_shortest": None,
        "cases": [],
    }
    if not vals:
        return out
    out["total_seconds"] = max(0.0, vals[-1].t - vals[0].t)
    out["total_drop"] = (pts["start_battery"] or 0) - (pts["end_battery"] or 0)
    out["start_voltage"] = pts.get("start_voltage")
    out["end_voltage"] = pts.get("end_voltage")
    out["start_temp"] = pts.get("start_temp")
    out["end_temp"] = pts.get("end_temp")
    if out["total_drop"] > 0:
        out["average_per_1pct"] = out["total_seconds"] / out["total_drop"]
    dts = [m["dt"] for m in pts["milestones"]]
    if dts:
        out["milestone_average"] = sum(dts) / len(dts)
        # ★ 最长/最短段(用户 2026-11-04): 带分段区间, 一眼能定位是哪一档掉得慢/快。
        #   区间起点用 milestone.from_battery —— 充电回升后段起点会重置(见 build_points)。
        segs = [{"from": m.get("from_battery", 0), "to": m["battery"],
                 "dt": m["dt"]} for m in pts["milestones"]]
        out["milestone_longest"] = max(segs, key=lambda s: s["dt"])
        out["milestone_shortest"] = min(segs, key=lambda s: s["dt"])

    # 按 label 的连续出现分组(同一标签即一次执行; 重复轮次标签带"第N次", 天然唯一)
    groups = []
    for s in vals:
        if not groups or groups[-1][0] != s.label:
            groups.append((s.label, [s]))
        else:
            groups[-1][1].append(s)
    for label, items in groups:
        first, last = items[0], items[-1]
        drop = first.battery - last.battery
        secs = max(0.0, last.t - first.t)
        out["cases"].append({
            "label": label,
            "start_battery": first.battery,
            "end_battery": last.battery,
            "drop": drop,
            "seconds": secs,
            "average_per_1pct": (secs / drop) if drop > 0 else None,
        })
    return out


def _default_reader(device):
    """默认电量读取: 复用前置/断言同一套实现(树优先 + OCR 兜底)。"""
    from core import session
    return session.get_battery_level(device)


class BatteryMonitor(threading.Thread):
    """后台采样线程: **拿到第一个有效电量后才开始计时**, 之后每 interval 秒一次。

    ★ 共用执行线程的设备对象(用户确认): 不新建 uiautomator2 连接。
    ★ 为什么要"武装延迟"(用户 2026-11-04): 运行一开始还没进设备页, 此时读到的
      不是机器人电量(或读不到) —— 若从运行开始计时, 起始电量/总时长都会把
      前置导航算进去。改为: 第一次读到有效电量(=进入设备页、电量可见)时才把
      该点作为 t=0 的起点样本; 之前的无效读数直接丢弃, 不计入统计。
    ★ stop() 幂等且带 join 超时 —— 停止/异常路径都不能留下僵尸线程。
    """

    def __init__(self, device, interval=60.0, label_getter=None, reader=None):
        super().__init__(daemon=True, name="battery-monitor")
        self.device = device
        self.interval = max(1.0, float(interval or 60.0))
        self._label_getter = label_getter or (lambda: "")
        self._reader = reader or _default_reader
        self.samples = []
        self._stop_event = threading.Event()
        self._t0 = None                 # None = 尚未拿到有效电量(未开始计时)
        self._armed = False
        self._joined = False

    def _label(self):
        try:
            label = str(self._label_getter() or "")
        except Exception:
            label = ""
        # 计时开始后、第一条用例开始前 = 前置准备阶段(起始电量就在这时读到)
        return label or ("前置准备" if self._armed else "")

    def _sample_once(self):
        try:
            raw = self._reader(self.device)
        except Exception:
            raw = -1
        voltage_v = None
        temp_c = None
        source = "app"
        if isinstance(raw, dict):
            try:
                battery = int(raw.get("battery", -1))
            except (TypeError, ValueError):
                battery = -1
            voltage_v = raw.get("voltage_v")
            temp_c = raw.get("temp_c")
            source = str(raw.get("source") or "param")
        else:
            try:
                battery = int(raw)
            except (TypeError, ValueError):
                battery = -1
        now = time.monotonic()
        if not self._armed:
            if battery < 0:
                return                  # 还没进设备页/读不到: 丢弃, 不开始计时
            self._armed = True
            self._t0 = now              # 第一个有效点 = 计时起点(t=0)
        self.samples.append(BatterySample(
            t=max(0.0, now - self._t0),
            battery=battery, label=self._label(),
            wall=time.strftime("%H:%M:%S"),
            voltage_v=float(voltage_v) if voltage_v is not None else None,
            temp_c=float(temp_c) if temp_c is not None else None,
            source=source))

    def run(self):
        self._sample_once()
        while not self._stop_event.wait(self.interval):
            self._sample_once()

    def start(self):
        # ★ 不在这里设 _t0: 计时起点由**第一个有效电量**决定(见 _sample_once)
        super().start()

    def stop(self, timeout=5.0):
        """停止采样并等待线程退出; 可重复调用。"""
        self._stop_event.set()
        if self.is_alive() and not self._joined:
            try:
                self.join(timeout)
            except RuntimeError:
                pass
            self._joined = True
