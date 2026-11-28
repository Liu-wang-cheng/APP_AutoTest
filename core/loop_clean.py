# -*- coding: utf-8 -*-
"""循环清扫：按组合轮换，清扫到下限电量回充，充到目标电量再继续。

用户需求（2026-11-27）：
  · 一轮完整的清扫 = 从开始清扫，直到进入充电/烘干/待机/休眠等结束状态；
  · 中途出现报错：记录事件名 + 中文含义，然后继续尝试启动清扫；
  · 连续 3 次启动后仍处于报错状态 → 结束循环清扫；
  · 可设置清扫次数，0 = 无限循环；
  · 每轮可使用不同的模式/吸力/水量（组合表轮换）。
"""
import json
import os
import re
import time
from datetime import datetime

from core.apos.faults import event_name, is_fault_event
from core.apos.mcu import McuStream
from core.apos.service import ParamService
from core.apos.stream import UploadStream
from core.driver import REPORT_SUBDIRS
from core.endurance import (CLEAN_STATUSES, TASK_ACTIVE_STATUSES,
                            _abs_out_dir, _battery,
                            _fmt_duration, _keepalive, _pct_text,
                            _status, _stream_fresh, is_wash_wait,
                            plan_settings)

#: 这些状态视为「一轮清扫结束」（用户 2026-11-27）
ROUND_DONE_STATUSES = frozenset({
    "work_status_charging", "work_status_full_charging",
    "work_status_dry_mop", "work_status_idle", "work_status_dormant",
    "work_status_base_station",
})

MAX_ERROR_RETRIES = 3

#: 只有回到基站/停稳后才允许开始下一轮（不能再在回充途中启动清扫）
START_READY_STATUSES = frozenset({
    "work_status_charging", "work_status_full_charging",
    "work_status_idle", "work_status_dormant",
})

#: 启动清扫后，进入这些状态也算“任务已接受”（扫拖会先回站洗拖布再清扫）
START_ACCEPT_STATUSES = frozenset({
    "work_status_back_charge", "work_status_go_charge",
    "work_status_base_station", "work_status_collect_dust",
    "work_status_wash_mop", "work_status_dry_mop",
})


def round_done(status):
    return status in ROUND_DONE_STATUSES


def should_return_charge(battery, recharge_battery):
    return 0 <= battery < recharge_battery


def ready_to_start(battery, status, clean_battery):
    """电量 ≥ 清扫电量，且机器人已经回到基站/停稳，才允许开始下一轮。

    回充途中(back_charge/go_charge)、基站任务(集尘/洗拖布/烘干)中都不启动，
    避免机器还没到站就被重新下发清扫。
    """
    return battery >= clean_battery and status in START_READY_STATUSES


def should_stop_existing(status):
    """开测前机器人是否还带着旧的清扫/暂停任务（需要先停掉再重开）。"""
    return status in TASK_ACTIVE_STATUSES


CLEAN_TYPE_TOTAL = "total"
CLEAN_TYPE_CURPOINT = "curpoint"      # 旧版「原地定点」(仅兼容已保存的配置)
CLEAN_TYPE_POINT = "point"            # 指定地点定点(用户 2026-11-28)
CLEAN_TYPE_ROOMS = "rooms"
CLEAN_TYPE_ZONES = "zones"

#: 定点清扫的默认方块边长(mm)与清扫遍数 —— 与 AP 上位机一致
POINT_SIDE = 1500
POINT_CLEAN_COUNT = 2


def parse_point(value):
    """定点坐标 mm: 支持 [x, y] / "x,y" / "定点(x, y)" / "(x, y)"。

    返回 [x, y] 或 []。
    """
    if isinstance(value, (list, tuple)):
        nums = list(value)
    else:
        text = re.sub(r"定点", "", str(value or ""))
        nums = [s for s in re.split(r"[,\s，()（）]+", text) if s]
    try:
        vals = [int(round(float(n))) for n in nums]
    except (TypeError, ValueError):
        return []
    return vals[:2] if len(vals) >= 2 else []


def point_area_entry(point, side=POINT_SIDE, count=POINT_CLEAN_COUNT,
                     start_id=9001):
    """定点 → 以该点为中心的方形区域条目(AP 的定点清扫就是 1.5m 见方扫 2 遍)。"""
    pt = parse_point(point)
    if not pt:
        return None
    half = max(100, int(side) // 2)
    x, y = pt
    rect = [x - half, y - half, x + half, y + half]
    entry = zone_area_entries([rect], start_id=start_id)[0]
    entry["cleanCount"] = int(count)
    return entry


def parse_area_ids(value):
    """房间 ID: 支持 "1,2" / "1、2;3" / "房间1 房间2" / 列表。

    界面里显示成「房间1 房间2」(用户 2026-11-28)，这里把前缀和空白一起处理。
    """
    if isinstance(value, (list, tuple)):
        items = value
    else:
        text = re.sub(r"房间\s*", " ", str(value or ""))
        items = re.split(r"[,，、;；\s]+", text)
    out = []
    for item in items:
        try:
            n = int(str(item).strip())
        except (TypeError, ValueError):
            continue
        if n > 0 and n not in out:
            out.append(n)
    return out


def parse_zones(value):
    """划区矩形: 支持 "x1,y1,x2,y2" 列表 / "x1,y1,x2,y2;x1,y1,x2,y2" 文本。

    返回 [[x1, y1, x2, y2], ...]（毫米，左上/右下顺序已在内部归一化）。
    """
    if isinstance(value, str):
        raw = [part for part in re.split(r"[;；]", value) if part.strip()]
    elif isinstance(value, (list, tuple)):
        raw = list(value)
    else:
        raw = []
    out = []
    for item in raw:
        if isinstance(item, str):
            nums = [s for s in re.split(r"[,\s，]+", item) if s]
        else:
            nums = list(item or ())
        try:
            vals = [int(round(float(n))) for n in nums]
        except (TypeError, ValueError):
            continue
        if len(vals) != 4:
            continue
        x1, y1, x2, y2 = vals
        rect = [min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)]
        if rect[2] - rect[0] < 100 or rect[3] - rect[1] < 100:
            continue        # 太小(小于 10cm)的区域机器人会忽略
        out.append(rect)
    return out


def combo_parts(combo):
    """(mode, fan, water, clean_type, area) —— 兼容旧三元组。

    area: 选区=房间 ID 列表; 划区=矩形列表; 定点=[x, y]; 其它=[]。
    """
    values = list(combo or ())
    values += [None] * (5 - len(values))
    mode, fan, water, ctype, area = values[:5]
    ctype = ctype or CLEAN_TYPE_TOTAL
    if ctype == CLEAN_TYPE_ZONES:
        return (mode, fan, water, ctype, parse_zones(area))
    if ctype in (CLEAN_TYPE_POINT, CLEAN_TYPE_CURPOINT):
        return (mode, fan, water, CLEAN_TYPE_POINT, parse_point(area))
    return (mode, fan, water, ctype, parse_area_ids(area))


def combo_label(combo):
    mode, fan, water, ctype, area = combo_parts(combo)
    text = "%s 吸力%s 水量%s" % (mode, fan, water)
    if ctype == CLEAN_TYPE_POINT:
        text += ("（定点 %d,%d）" % tuple(area) if area else "（未选定点）")
    elif ctype == CLEAN_TYPE_ROOMS:
        text += "（选区 %s）" % ",".join(str(x) for x in area)
    elif ctype == CLEAN_TYPE_ZONES:
        text += "（划区 %d 个）" % len(area)
    return text


def zone_area_entries(zones, start_id=9001):
    """矩形 → `/MapData/SetAreaInfo` 的 Areainfo 条目（AP 上位机同格式）。

    ★ id 用 9001 起（真机验证 2026-11-28）：AP 上位机临时区用 1，会和房间
      分段 id 撞；9001+ 机器人照样接受，`AreaIds` 下发也只命中 areaValue。
    """
    entries = []
    for i, zone in enumerate(parse_zones(zones)):
        x1, y1, x2, y2 = zone
        entries.append({
            "id": start_id + i,
            "cleanCount": 1,
            "vertexs": [[x1, y1], [x2, y1], [x2, y2], [x1, y2]],
        })
    return entries


def next_combo(combos, index):
    """按顺序取下一个组合（多组合轮换）。"""
    if not combos:
        return None
    return combos[index % len(combos)]


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _fault_cn(event):
    try:
        return event_name(event) or event
    except Exception:
        return event


def run_loop_clean(ip, combos, clean_battery=30, recharge_battery=20,
                   max_rounds=0, sample_interval=15.0,
                   out_dir=None, log_fn=None, stop_event=None,
                   on_state=None, stream=None):
    """执行循环清扫，返回结果 dict。

    stop_event: threading.Event；GUI 点停止时置位，正常收尾。
    on_state: callable(dict)，约每秒回调一次实时状态。
    """
    combos = [tuple(c) for c in (combos or []) if c]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = _abs_out_dir(out_dir or os.path.join(
        "reports", REPORT_SUBDIRS["loop"], "loop_%s" % stamp))
    os.makedirs(base, exist_ok=True)
    run_log = os.path.join(base, "run.log")
    on_log = log_fn

    def _log(msg):
        line = "[%s] %s" % (_now(), msg)
        try:
            with open(run_log, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
        if on_log is not None:
            on_log(msg)

    def _stop():
        return stop_event is not None and stop_event.is_set()

    result = {"kind": "loop", "ip": ip, "clean_battery": clean_battery,
              "recharge_battery": recharge_battery, "max_rounds": max_rounds,
              "combos": combos, "rounds": [], "errors": [], "events": [],
              "completed": False, "start_time": _now(), "end_reason": ""}

    # ★ 共用推送会话(用户 2026-11-27): 设备页传入 stream 时直接用, 不另开会话
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

    applied = {"mode": None, "fan": None, "water": None}
    ka = {"last_restart": 0.0, "since": time.time()}
    last_mcu_ts = time.time()
    last_state_ts = 0.0
    current = None
    combo_idx = 0
    error_streak = 0
    last_retry = 0.0
    last_charge_try = 0.0
    last_wait_note = 0.0
    last_status = ""
    location_ok = False      # 本轮是否收到「定位成功，继续清扫」事件
    zone_used = set()        # 本次是否下发过临时划区(结束时清掉)

    def _rebuild_if_stale():
        if _stream_fresh(st):
            return
        try:
            ok = bool(st.start(ip, paths, preserve=True))
        except Exception:
            ok = False
        ka["since"] = time.time()
        ka["next_try"] = 0.0
        _log("[推送] 数据流%s" % ("已重建" if ok else "不可用"))

    def apply(combo, force=False, only=None):
        mode, fan, water, _ctype, _areas = combo_parts(combo)
        plan = plan_settings(applied, mode, fan, water, only)
        if force and not plan:
            plan = {"mode": mode, "fan": fan, "water": water}
        if not plan:
            return True
        _rebuild_if_stale()
        oks = {}
        if "mode" in plan:
            oks["mode"] = bool(svc.set_clean_mode(mode))
        if "water" in plan:
            oks["water"] = bool(svc.set_water_level(water))
        if "fan" in plan:
            oks["fan"] = bool(svc.set_fan_level(fan))
        for ch, ok in oks.items():
            if ok:
                applied[ch] = plan[ch]
        _log("[设置] 模式=%s 吸力=%s 水量=%s%s"
             % (mode, fan, water, "" if all(oks.values()) else "，下发失败"))
        return all(oks.values())

    def record_event(kind, detail="", round_no=None):
        item = {"ts": time.time(), "time": _now(), "round": round_no or "",
                "kind": kind, "detail": str(detail)}
        result["events"].append(item)
        return item

    def record_error(round_no, event, status, detail=""):
        item = {"ts": time.time(), "time": _now(), "round": round_no,
                "event": event, "name": _fault_cn(event),
                "status": status, "detail": str(detail)[:200]}
        result["errors"].append(item)
        record_event("报错", "%s（%s），状态 %s"
                     % (event, item["name"], status), round_no)
        _log("[报错] 第 %s 轮 %s（%s）状态=%s"
             % (round_no or "-", event, item["name"], status))
        return item

    def capture_faults(round_no):
        nonlocal last_mcu_ts, location_ok
        if mcu is None:
            return
        now = time.time()
        for _t, line in mcu.log_since(last_mcu_ts):
            if "clean_location_success" in line:
                location_ok = True
            m = re.search(
                r"(?:Recv event_id\s+|tpc:/hardware/error id:"
                r"|event_id:\s*|event:)([a-z][a-z0-9_]+)", line)
            if m and is_fault_event(m.group(1)):
                ev = m.group(1)
                items = result["errors"]
                if not (items and items[-1]["event"] == ev
                        and now - items[-1]["ts"] < 5):
                    record_error(round_no, ev,
                                 _status(st), line.strip()[:200])
        last_mcu_ts = now

    def try_continue():
        """报错恢复：发继续清扫指令，等待重新进入清扫/回站状态。"""
        _log("[恢复] 发送继续清扫指令")
        try:
            svc.continue_clean()
        except Exception:
            pass
        end = time.time() + 15
        while time.time() < end:
            if _stop():
                return False
            s = _status(st)
            if s in CLEAN_STATUSES or s in START_ACCEPT_STATUSES:
                return True
            time.sleep(0.5)
        return False

    def ensure_idle(timeout=30.0):
        """下发新一轮前先回到空闲：当前还有清扫/暂停任务时先停止(用户 2026-11-28)。

        划区/选区清扫只有在空闲(或充电)状态下才会被任务管理器接受；带着旧任务
        直接下发要么被忽略，要么沿用旧任务，测试组合就不可控了。
        """
        s = _status(st)
        if not should_stop_existing(s):
            return True
        _log("[启动] 当前还有任务（%s），先停止转空闲再下发清扫" % s)
        record_event("停止已有任务", "下发清扫前检测到 %s，先停止转空闲" % s)
        try:
            svc.stop_clean()
            time.sleep(1)
            if _status(st) in TASK_ACTIVE_STATUSES:
                svc.post_event("/event/task_manager", "kill_all_nodes")
            svc.post_event("/event/task_node", "clear_last_clean_id")
        except Exception as e:
            _log("[启动] 停止已有任务失败: %s" % e)
        end = time.time() + timeout
        while time.time() < end:
            if _stop():
                return False
            s = _status(st)
            if not should_stop_existing(s):
                _log("[启动] 已回到空闲（%s）" % (s or "未知"))
                return True
            time.sleep(1)
        _log("[启动] 等待转空闲超时（%s），本轮放弃启动"
             % (_status(st) or "未知"))
        return False

    def start_round(combo):
        mode, fan, water, ctype, area = combo_parts(combo)
        if not ensure_idle():
            return False
        # ★ 启动前只下发模式(决定清扫类型)；吸力/水量必须等机器人定位成功、
        #   真正进入清扫后再下发（用户 2026-11-27：提前下发会被启动流程覆盖）。
        if svc.set_clean_mode(mode):
            applied["mode"] = mode
        _log("[清扫] 启动 %s" % combo_label(combo))
        if ctype == CLEAN_TYPE_POINT:
            # 指定地点定点: 以该点为中心 1.5m 见方、扫 2 遍(与 AP 一致),
            # 走已验证的临时区域链路
            entry = point_area_entry(area)
            if entry is None:
                _log("[清扫] 定点启动失败：没有选择定点位置")
                return False
            if not svc.set_area_info([entry]):
                _log("[清扫] 定点启动失败：写区域信息失败")
                return False
            zone_used.add("point")
            if not svc.start_area_clean([entry["id"]]):
                _log("[清扫] 定点启动失败（区域 ID: %s）" % entry["id"])
                return False
        elif ctype == CLEAN_TYPE_ROOMS:
            # 房间走机器人自带分段(autoAreaValue): StartSegmentClean
            if not svc.start_segment_clean(area):
                _log("[清扫] 选区启动失败（房间 ID: %s）"
                     % ",".join(str(x) for x in area))
                return False
        elif ctype == CLEAN_TYPE_ZONES:
            entries = zone_area_entries(area)
            if not entries:
                _log("[清扫] 划区启动失败：没有有效区域")
                return False
            # AP 上位机同款链路：先写区域(SetAreaInfo)，再按区域 id 清扫
            if not svc.set_area_info(entries):
                _log("[清扫] 划区启动失败：写区域信息失败")
                return False
            zone_used.add("zones")
            if not svc.start_area_clean([e["id"] for e in entries]):
                _log("[清扫] 划区启动失败（区域 ID: %s）"
                     % ",".join(str(e["id"]) for e in entries))
                return False
        else:
            svc.start_total_clean()
        end = time.time() + 30
        while time.time() < end:
            if _stop():
                break
            s = _status(st)
            if s in CLEAN_STATUSES or s in START_ACCEPT_STATUSES:
                return True
            time.sleep(0.5)
        return False

    def cleanup():
        try:
            svc.stop_clean()
            time.sleep(1)
            if _status(st) in TASK_ACTIVE_STATUSES:
                svc.post_event("/event/task_manager", "kill_all_nodes")
            svc.post_event("/event/task_node", "clear_last_clean_id")
            _log("[清理] 已停止并清除当前清扫任务（防断点续扫）")
        except Exception as e:
            _log("[清理] 失败: %s" % e)

    # 开测前若机器人已在清扫/暂停：不沿用旧任务，先停掉再按测试组合重开，
    # 保证每一轮的起始状态、模式与挡位都可控（用户 2026-11-27）。
    wait_end = time.time() + 10
    while not _status(st) and time.time() < wait_end and not _stop():
        time.sleep(0.5)
    initial_status = _status(st)
    if should_stop_existing(initial_status):
        _log("[启动] 检测到已有任务（%s），先停止再重新开始"
             % initial_status)
        record_event("停止旧任务",
                     "开测前检测到 %s，已停止并按测试组合重新开始"
                     % initial_status)
        cleanup()
        wait_end = time.time() + 15
        while (time.time() < wait_end and not _stop()
               and should_stop_existing(_status(st))):
            time.sleep(0.5)
        _log("[启动] 旧任务已停止（当前状态 %s）"
             % (_status(st) or "未知"))

    try:
        while True:
            if _stop():
                result["end_reason"] = "用户停止"
                break
            if max_rounds and len(result["rounds"]) >= max_rounds:
                result["end_reason"] = "达到设定清扫次数（%d）" % max_rounds
                break
            now = time.time()
            _keepalive(st, ka, _log, ip, paths)
            battery = _battery(st)
            status = _status(st)
            cleaning = status in CLEAN_STATUSES
            if status != last_status:
                kind = None
                if cleaning and last_status not in CLEAN_STATUSES:
                    kind = "开始清扫"
                elif status in ("work_status_back_charge",
                                "work_status_go_charge"):
                    kind = "开始回充"
                elif status in ("work_status_charging",
                                "work_status_full_charging"):
                    kind = "充电"
                elif status == "work_status_wash_mop":
                    kind = "洗拖布"
                elif status == "work_status_collect_dust":
                    kind = "集尘"
                elif status == "work_status_dry_mop":
                    kind = "烘干"
                elif status == "work_status_base_station":
                    kind = "基站任务"
                elif status in ("work_status_pause",
                                "work_status_base_station_pause",
                                "work_status_back_charge_pause"):
                    kind = "暂停"
                elif status in ("work_status_idle", "work_status_dormant"):
                    kind = "待机"
                if kind:
                    record_event(kind, status.replace("work_status_", ""),
                                 current["no"] if current else None)
                last_status = status
            capture_faults(current["no"] if current else None)

            if on_state is not None and now - last_state_ts >= 1.0:
                last_state_ts = now
                on_state({
                    "battery": battery, "status": status,
                    "round": current["no"] if current else 0,
                    "rounds": len(result["rounds"]),
                    "combo": current["combo"] if current else None,
                    "round_seconds": (now - current["start_ts"])
                    if current else 0,
                    "errors": len(result["errors"]),
                    "total_seconds": result.get("clean_seconds", 0),
                })

            # ── 清扫中 ──
            if current is not None and cleaning:
                error_streak = 0
                current["cleaned_once"] = True
                if (not current.get("settings_sent")
                        and (location_ok
                             or now - current["start_ts"] > 15)):
                    current["settings_sent"] = True
                    apply(current["combo"])
                    _log("[设置] 已定位成功并开始清扫，下发吸力/水量")
                current["clean_seconds"] = (current.get("clean_seconds", 0)
                                            + (now - current["last_ts"]))
                current["last_ts"] = now
                if should_return_charge(battery, recharge_battery):
                    if not current.get("return_sent"):
                        current["return_sent"] = True
                        _log("[回充] 电量 %s < 回充电量 %s%%，发送回充"
                             % (_pct_text(battery), recharge_battery))
                        svc.stop_clean()
                        svc.start_find_charger()
                time.sleep(0.5)
                continue

            # ── 有当前轮、但暂时不在清扫（回洗/结束/报错） ──
            if current is not None:
                mode = current["combo"][0]
                if status == "work_status_error":
                    if now - last_retry > 10:
                        last_retry = now
                        error_streak += 1
                        record_error(current["no"], "work_status_error",
                                     status)
                        if error_streak >= MAX_ERROR_RETRIES:
                            result["end_reason"] = (
                                "连续 %d 次清扫处于报错状态，结束循环"
                                % MAX_ERROR_RETRIES)
                            break
                        record_event("继续清扫",
                                     "第 %d 次尝试" % error_streak,
                                     current["no"])
                        _log("[恢复] 第 %d 次尝试继续清扫" % error_streak)
                        if try_continue():
                            current["last_ts"] = time.time()
                    time.sleep(1)
                    continue
                if is_wash_wait(mode, status):
                    _log("[回洗] 等待机器人自动继续清扫（%s）" % status)
                    current["last_ts"] = now
                    time.sleep(2)
                    continue
                if round_done(status):
                    # 点扫拖会先洗拖布/回站，未真正清扫过之前不能算一轮完成
                    if not current.get("cleaned_once"):
                        if now - current["start_ts"] < 180:
                            time.sleep(2)
                            continue
                        record_error(current["no"], "start_failed", status,
                                     "启动后 3 分钟未进入清扫")
                        current = None
                        error_streak += 1
                        if error_streak >= MAX_ERROR_RETRIES:
                            result["end_reason"] = (
                                "连续 %d 次启动清扫失败，结束循环"
                                % MAX_ERROR_RETRIES)
                            break
                        time.sleep(5)
                        continue
                    current["end_ts"] = now
                    current["end_time"] = _now()
                    current["end_battery"] = battery
                    current["end_status"] = status
                    current["seconds"] = now - current["start_ts"]
                    result["clean_seconds"] = (result.get("clean_seconds", 0)
                                               + current.get("clean_seconds",
                                                             0))
                    result["rounds"].append(current)
                    record_event("轮次完成",
                                 "%s 吸力%s 水量%s，%s，状态 %s，电量 %s"
                                 % (current["combo"][0], current["combo"][1],
                                    current["combo"][2],
                                    _fmt_duration(current["seconds"]),
                                    status, _pct_text(battery)),
                                 current["no"])
                    _log("[轮次 %d] 完成：%s 吸力%s 水量%s，%s，结束状态 %s，"
                         "电量 %s"
                         % (current["no"], current["combo"][0],
                            current["combo"][1], current["combo"][2],
                            _fmt_duration(current["seconds"]),
                            status, _pct_text(battery)))
                    current = None
                    error_streak = 0
                    combo_idx += 1
                    time.sleep(1)
                    continue
                # 其它中间状态：等
                time.sleep(1)
                continue

            # ── 无当前轮：等待回充/开始下一轮 ──
            if status in ("work_status_back_charge",
                          "work_status_go_charge"):
                if now - last_wait_note > 60:
                    last_wait_note = now
                    _log("[等待] 机器人回充中，等待到站后再开始下一轮")
                time.sleep(2)
                continue
            if status == "work_status_error":
                combo = next_combo(combos, combo_idx)
                if now - last_retry > 10:
                    last_retry = now
                    error_streak += 1
                    record_error(0, "work_status_error", status)
                    if error_streak >= MAX_ERROR_RETRIES:
                        result["end_reason"] = (
                            "连续 %d 次清扫处于报错状态，结束循环"
                            % MAX_ERROR_RETRIES)
                        break
                    if combo:
                        _log("[恢复] 第 %d 次尝试继续清扫" % error_streak)
                        if not try_continue():
                            # 没有可继续的任务时才新开一轮
                            start_round(combo)
                time.sleep(1)
                continue

            if battery >= 0 and battery < recharge_battery:
                if ("charging" not in status and status not in (
                        "work_status_back_charge", "work_status_go_charge")):
                    if now - last_charge_try > 60:
                        last_charge_try = now
                        _log("[等待] 电量 %s < 回充电量 %s%%，发送回充"
                             % (_pct_text(battery), recharge_battery))
                        svc.start_find_charger()
                time.sleep(2)
                continue

            combo = next_combo(combos, combo_idx)
            if combo is None:
                result["end_reason"] = "没有可用的清扫组合"
                break
            if not ready_to_start(battery, status, clean_battery):
                time.sleep(1)
                continue
            # 开始新的一轮
            location_ok = False
            current = {"no": len(result["rounds"]) + 1, "combo": combo,
                       "start_ts": time.time(), "start_time": _now(),
                       "last_ts": time.time(), "start_battery": battery,
                       "clean_seconds": 0.0, "return_sent": False,
                       "cleaned_once": False, "settings_sent": False}
            _log("[轮次 %d] 开始：电量 %s，组合 %s 吸力%s 水量%s"
                 % (current["no"], _pct_text(battery), combo[0], combo[1],
                    combo[2]))
            if not start_round(combo):
                record_error(current["no"], "start_failed", _status(st),
                             "启动清扫超时")
                error_streak += 1
                current = None
                if error_streak >= MAX_ERROR_RETRIES:
                    result["end_reason"] = ("连续 %d 次启动清扫失败，结束循环"
                                            % MAX_ERROR_RETRIES)
                    break
                time.sleep(5)
                continue
            error_streak = 0
            record_event("启动清扫任务",
                         "%s 吸力%s 水量%s，电量 %s"
                         % (combo[0], combo[1], combo[2],
                            _pct_text(battery)), current["no"])
            time.sleep(1)
    finally:
        cleanup()
        if zone_used:
            # 划区写的是 areaValue(手动区域，整体替换)；结束时清掉，
            # 避免测试用的矩形一直留在 APP 地图上（AP 上位机同款行为）。
            try:
                if svc.clear_area_info():
                    _log("[清理] 已清除临时划区")
            except Exception as e:
                _log("[清理] 清除临时划区失败: %s" % e)
        if own_stream:
            st.stop()          # 共享流不归本函数管, 交给设备页
        if mcu is not None:
            mcu.stop()

    record_event("结束", result.get("end_reason") or "")
    result["completed"] = bool(result["rounds"])
    result["end_time"] = _now()
    result.setdefault("clean_seconds", 0)
    try:
        from tools.loop_report import build_report
        result["report"] = build_report(
            result, os.path.join(base, "循环清扫_报告.xlsx"))
        _log("[报告] %s" % result["report"])
    except Exception as e:
        result["report"] = None
        _log("[报告] 生成异常: %s" % e)
    with open(os.path.join(base, "rounds.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    _log("[完成] 循环清扫结束：%d 轮，%s"
         % (len(result["rounds"]), result.get("end_reason", "")))
    try:
        from core.endurance import prune_runs
        prune_runs(root=os.path.dirname(base), protect=[base], log_fn=_log)
    except Exception:
        pass
    return result, base
