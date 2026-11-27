# -*- coding: utf-8 -*-
"""续航测试 Excel 报告生成器（支持多模式/多挡位）。

用法:
  python tools/endurance_report.py <run_dir1> [run_dir2 ...] [--out x.xlsx]

每个 run_dir 是一次「满电→低电回充」的结果（result.json）。生成:
  · 汇总: 每个组合一行（测试项目=续航测试，测试条件=模式·挡位）；
  · 电量监测-<测试条件>: 每个组合单独一页，含汇总/每10%分段/详细数据。
样式与 core/excel_report.py 的自动化报告一致；含电量-时间曲线；时间整数分钟。
"""
import argparse
import json
import os
import re
import sys
import time
import types

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.hyperlink import Hyperlink

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from core.excel_report import ExcelReport as ER  # noqa: E402

MODE = {"sweepOnly": "仅扫", "sweepMop": "扫拖", "mopOnly": "单拖",
        "sweepThenMop": "先扫后拖"}


def put(ws, r, c, v, *, bold=False, fill=None, color=None, align="left",
        size=10, numfmt=None, border=True, merge_to=None, wrap=False):
    if merge_to:
        ws.merge_cells(start_row=r, start_column=c, end_row=r,
                       end_column=merge_to)
    cell = ws.cell(row=r, column=c, value=v)
    cell.font = Font(name="Microsoft YaHei", size=size, bold=bold,
                     color=color or "000000")
    if fill:
        cell.fill = PatternFill(start_color=fill, end_color=fill,
                                fill_type="solid")
    cell.alignment = Alignment(horizontal=align, vertical="center",
                               wrap_text=wrap)
    if numfmt:
        if numfmt == '0" 分钟"' and isinstance(v, (int, float)):
            v = int(float(v) + 0.5)
            cell.value = v
        cell.number_format = numfmt
    if border:
        for cc in range(c, (merge_to or c) + 1):
            ws.cell(row=r, column=cc).border = ER.BORDER
    return cell


def border_row(ws, r, ncols):
    for c in range(1, ncols + 1):
        ws.cell(row=r, column=c).border = ER.BORDER


def header(ws, r, labels):
    for i, h in enumerate(labels, 1):
        put(ws, r, i, h, bold=True, fill="4472C4", color="FFFFFF",
            align="center", size=11)
    border_row(ws, r, len(labels))
    return r + 1


def section(ws, r, text, ncols):
    put(ws, r, 1, text, bold=True, color="1F4E79", size=12, merge_to=ncols)
    border_row(ws, r, ncols)
    return r + 1


def kv(ws, r, k, v, ncols, numfmt=None):
    put(ws, r, 1, k, bold=True)
    put(ws, r, 2, v, merge_to=ncols, numfmt=numfmt)
    border_row(ws, r, ncols)
    return r + 1


def cond_label(d):
    label = "%s · 吸力%s" % (MODE.get(d.get("mode"), d.get("mode")),
                            d.get("fan_level"))
    if d.get("mode") in ("sweepMop", "sweepThenMop"):
        label += "+水量%s" % d.get("water_level")
    return label


def safe_sheet(name, used):
    name = re.sub(r'[\\*?:/\[\]]', "_", name)[:31] or "电量监测"
    base, n = name, 2
    while name in used:
        suffix = "-%d" % n
        name = base[:31 - len(suffix)] + suffix
        n += 1
    used.add(name)
    return name


def add_battery_chart(ws, samples, milestones=()):
    """复用自动化报告的「电量-时间」散点图（隐藏数据列 + 锚 J4）。"""
    if not samples:
        return
    start = samples[0]["ts"]
    objs = [types.SimpleNamespace(
        t=s["ts"] - start, battery=s.get("battery", -1))
        for s in samples if isinstance(s.get("battery"), int)]
    ms = [{"t": m["ts"] - start,
           "battery": m.get("battery", m.get("level"))}
          for m in milestones if m.get("ts") is not None]
    ER._add_battery_chart(ws, objs, ms)


def add_battery_sheet(wb, d, title_suffix):
    used = set(wb.sheetnames)
    sheet_part = title_suffix.replace(" · ", "-").replace(" ", "")
    ws = wb.create_sheet(safe_sheet("电量监测-" + sheet_part, used))
    for c, w in zip("ABCDEFGH", [10, 20, 12, 12, 16, 16, 20, 12]):
        ws.column_dimensions[c].width = w
    ws.merge_cells("A1:H1")
    put(ws, 1, 1, "电量监测", align="center", size=16, border=False)
    ws.cell(row=1, column=1).font = ER.TITLE_FONT
    ws.row_dimensions[1].height = 34
    ws.merge_cells("A2:H2")
    put(ws, 2, 1, "%s    步长 10%%    采样间隔 %s 秒    生成时间: %s"
        % (sheet_part, d.get("sample_interval"),
           time.strftime("%Y-%m-%d %H:%M:%S")), color="808080",
        align="center", size=9, border=False)
    border_row(ws, 2, 8)

    samples = d.get("samples") or []
    start = samples[0]["ts"] if samples else 0
    total = float(d.get("wall_seconds") or 0) / 60.0
    ret = int(d.get("return_battery") or 0)
    low = int(d.get("low_battery") or 20)
    ten = d.get("ten_percent") or []
    drops = []
    for i in range(1, len(ten)):
        prev, cur = ten[i - 1], ten[i]
        drops.append({"from": prev["level"], "to": cur["level"],
                      "dt": (cur["ts"] - prev["ts"]) / 60.0,
                      "arrive": (cur["ts"] - start) / 60.0})
    r = 4
    r = section(ws, r, "一、汇总", 8)
    r = kv(ws, r, "续航时间(开始→低电, 分钟)", total, 8, '0" 分钟"')
    r = kv(ws, r, "起始电量", "100%", 8)
    r = kv(ws, r, "结束电量", "%s%%" % ret, 8)
    r = kv(ws, r, "总耗电", "%d%%" % (100 - ret), 8)
    if drops:
        avg = sum(x["dt"] for x in drops) / len(drops)
        lo = min(drops, key=lambda x: x["dt"])
        hi = max(drops, key=lambda x: x["dt"])
        r = kv(ws, r, "每 10% 段平均用时(分钟)", avg, 8, '0" 分钟"')
        r = kv(ws, r, "最长段", "%s%% → %s%%    %d 分钟"
               % (hi["from"], hi["to"], int(hi["dt"] + 0.5)), 8)
        r = kv(ws, r, "最短段", "%s%% → %s%%    %d 分钟"
               % (lo["from"], lo["to"], int(lo["dt"] + 0.5)), 8)
    r = kv(ws, r, "有效采样", len(samples), 8)
    r += 1
    r = section(ws, r, "每 10% 分段用时", 8)
    r = header(ws, r, ["分段", "起始电量", "结束电量", "用时(分钟)",
                       "到达时刻(分钟)"])
    for x in drops:
        put(ws, r, 1, "%s%% → %s%%" % (x["from"], x["to"]))
        put(ws, r, 2, "%s%%" % x["from"], align="center")
        put(ws, r, 3, "%s%%" % x["to"], align="center")
        put(ws, r, 4, x["dt"], align="center", numfmt='0" 分钟"')
        put(ws, r, 5, x["arrive"], align="center", numfmt='0" 分钟"')
        border_row(ws, r, 5)
        r += 1
    r += 1
    r = section(ws, r, "二、详细数据", 8)
    r = header(ws, r, ["序号", "时间", "电量", "变化", "距上次变化(分钟)",
                       "累计用时(分钟)", "状态", "电池电压(V)"])
    prev = None
    idx = 0
    for s in samples:
        b = s.get("battery")
        if prev is None or b != prev.get("battery"):
            idx += 1
            delta = 0 if prev is None else b - prev["battery"]
            gap = 0 if prev is None else (s["ts"] - prev["ts"]) / 60.0
            put(ws, r, 1, idx, align="center")
            put(ws, r, 2, time.strftime(
                "%H:%M:%S", time.localtime(s["ts"])), align="center")
            put(ws, r, 3, "%s%%" % b, align="center")
            put(ws, r, 4, "%+d%%" % delta if prev else "—", align="center")
            put(ws, r, 5, gap if prev else "—", align="center",
                numfmt='0" 分钟"' if prev else None)
            put(ws, r, 6, (s["ts"] - start) / 60.0, align="center",
                numfmt='0" 分钟"')
            put(ws, r, 7, "低电回充" if isinstance(b, int) and b <= low
                and "back" in str(s.get("status")) else "清扫中",
                align="center")
            v = s.get("battery_vol")
            put(ws, r, 8, "%.2f" % v if isinstance(v, (int, float)) else "—",
                align="center")
            border_row(ws, r, 8)
            prev = s
            r += 1

    faults = d.get("faults") or []
    if faults:
        r += 1
        r = section(ws, r, "四、故障事件", 8)
        r = header(ws, r, ["序号", "时间", "故障事件", "中文含义", "当时状态"])
        for i, x in enumerate(faults, 1):
            put(ws, r, 1, i, align="center")
            put(ws, r, 2, time.strftime(
                "%H:%M:%S", time.localtime(x["ts"])), align="center")
            put(ws, r, 3, x.get("event", ""))
            name = x.get("name")
            if not name:
                try:
                    from core.apos.faults import event_name
                    name = event_name(x.get("event", ""))
                except Exception:
                    name = x.get("event", "")
            put(ws, r, 4, name)
            put(ws, r, 5, x.get("status", ""), align="center")
            border_row(ws, r, 5)
            r += 1
    ER._autofit_columns(ws, letters="ABCDEFGH", max_row=r - 1)
    add_battery_chart(ws, samples, d.get("ten_percent") or [])
    return ws


def add_fan_water_sheet(wb, d, title_suffix):
    """风机吸力 / 水量出水详情页（每个组合一页）。"""
    used = set(wb.sheetnames)
    sheet_part = title_suffix.replace(" · ", "-").replace(" ", "")
    ws = wb.create_sheet(safe_sheet("风机水量-" + sheet_part, used))
    for c, w in zip("ABCDEFGH", [10, 20, 14, 16, 16, 16, 14, 14]):
        ws.column_dimensions[c].width = w
    ws.merge_cells("A1:H1")
    put(ws, 1, 1, "风机吸力与出水量", align="center", size=16, border=False)
    ws.cell(row=1, column=1).font = ER.TITLE_FONT
    ws.row_dimensions[1].height = 34
    ws.merge_cells("A2:H2")
    put(ws, 2, 1, "%s    采样间隔 %s 秒    生成时间: %s"
        % (sheet_part, d.get("sample_interval"),
           time.strftime("%Y-%m-%d %H:%M:%S")),
        color="808080", align="center", size=9, border=False)
    border_row(ws, 2, 8)

    samples = d.get("samples") or []
    events = d.get("events") or []
    r = 4
    r = section(ws, r, "一、风机吸力", 8)
    r = kv(ws, r, "设置吸力档位", d.get("fan_level"), 8)
    r = kv(ws, r, "实际工作挡位(FanLevelCurrentWork)",
           d.get("fan_level_observed", "—"), 8)
    r = kv(ws, r, "用户挡位观察(FanLevelUserSet)",
           d.get("fan_user_observed", "—"), 8)
    speeds = [s.get("fan_speed") for s in samples
              if isinstance(s.get("fan_speed"), (int, float))]
    if speeds:
        r = kv(ws, r, "风机转速(最后/最低~最高)",
               "%d / %d~%d" % (int(speeds[-1]), int(min(speeds)),
                               int(max(speeds))), 8)
    fan_dev = sum(1 for e in events if e.get("name") == "挡位偏离")
    r = kv(ws, r, "吸力偏离重发次数", fan_dev, 8)
    r += 1
    fan_changes = d.get("fan_changes") or []
    if fan_changes:
        r = section(ws, r, "吸力挡位变化记录", 8)
        r = header(ws, r, ["时间", "类型", "挡位"])
        for c in fan_changes[-1000:]:
            put(ws, r, 1, time.strftime(
                "%H:%M:%S", time.localtime(c["ts"])), align="center")
            put(ws, r, 2, c.get("kind", ""))
            put(ws, r, 3, c.get("value"), align="center")
            border_row(ws, r, 3)
            r += 1
        r += 1

    r = section(ws, r, "二、水量与出水", 8)
    r = kv(ws, r, "设置水量档位", d.get("water_level"), 8)
    r = kv(ws, r, "实际工作挡位(WaterLevelCurrentWork)",
           d.get("water_level_observed", "—"), 8)
    r = kv(ws, r, "总出水量(mL)",
           float(d.get("water_total_ml") or 0), 8, numfmt='0.00')
    outflows = d.get("water_outflows") or []
    r = kv(ws, r, "出水次数（水泵脉冲）", len(outflows), 8)
    sessions = d.get("water_sessions") or []
    if sessions:
        r = kv(ws, r, "连续出水段数", len(sessions), 8)
    gaps = [x["interval_s"] for x in outflows
            if isinstance(x.get("interval_s"), (int, float))]
    if gaps:
        r = kv(ws, r, "出水间隔(平均/最短~最长 秒)",
               "%.1f / %.1f~%.1f" % (sum(gaps) / len(gaps),
                                     min(gaps), max(gaps)), 8)
    water_dev = sum(1 for e in events if e.get("name") == "水量偏离")
    r = kv(ws, r, "水量偏离重发次数", water_dev, 8)
    sets = sorted({(x.get("level"), x.get("kind") or "设置",
                    x.get("open_ms"), x.get("period_ms"))
                   for x in d.get("water_sets") or []})
    if sets:
        r = kv(ws, r, "水泵参数",
               "；".join("挡位 %s（%s）：%sms / %sms" % s for s in sets), 8)
    r += 1

    water_changes = d.get("water_changes") or []
    if water_changes:
        r = section(ws, r, "水量挡位变化记录", 8)
        r = header(ws, r, ["时间", "类型", "挡位"])
        for c in water_changes:
            put(ws, r, 1, time.strftime(
                "%H:%M:%S", time.localtime(c["ts"])), align="center")
            put(ws, r, 2, c.get("kind", ""))
            put(ws, r, 3, c.get("value"), align="center")
            border_row(ws, r, 3)
            r += 1
        r += 1

    if outflows:
        r = section(ws, r, "出水记录（水泵脉冲，间隔=固定周期）", 8)
        r = header(ws, r, ["序号", "开始时间", "出水时长(秒)",
                           "距上次出水(秒)", "出水量(mL)", "累计(mL)",
                           "挡位"])
        for i, x in enumerate(outflows, 1):
            put(ws, r, 1, i, align="center")
            put(ws, r, 2, time.strftime(
                "%H:%M:%S", time.localtime(x["start_ts"])), align="center")
            dur = x.get("duration_s")
            put(ws, r, 3, round(dur, 1) if isinstance(dur, (int, float))
                else "—", align="center",
                numfmt='0.0' if isinstance(dur, (int, float)) else None)
            gap = x.get("interval_s")
            put(ws, r, 4, round(gap, 1) if isinstance(gap, (int, float))
                else "—", align="center",
                numfmt='0.0' if isinstance(gap, (int, float)) else None)
            ml = x.get("water_ml")
            put(ws, r, 5, round(ml, 1) if isinstance(ml, (int, float))
                else "—", align="center",
                numfmt='0.0' if isinstance(ml, (int, float)) else None)
            tot = x.get("total_ml")
            if isinstance(tot, (int, float)):
                put(ws, r, 6, float(tot), align="center", numfmt='0.00')
            else:
                put(ws, r, 6, "—", align="center")
            lv = x.get("level")
            put(ws, r, 7, lv if lv is not None else "—", align="center")
            border_row(ws, r, 7)
            r += 1

    if sessions:
        r += 1
        r = section(ws, r, "出水汇总（每次连续出水段，出水量为设备上报）", 8)
        r = header(ws, r, ["时间", "持续(秒)", "出水量(mL)",
                           "累计(mL)", "挡位"])
        for x in sessions:
            put(ws, r, 1, time.strftime(
                "%H:%M:%S", time.localtime(x["ts"])), align="center")
            el = x.get("elapsed_s")
            put(ws, r, 2, round(el, 1) if isinstance(el, (int, float))
                else "—", align="center",
                numfmt='0.0' if isinstance(el, (int, float)) else None)
            ml = x.get("water_ml")
            if isinstance(ml, (int, float)):
                put(ws, r, 3, float(ml), align="center", numfmt='0.00')
            else:
                put(ws, r, 3, "—", align="center")
            tot = x.get("total_ml")
            if isinstance(tot, (int, float)):
                put(ws, r, 4, float(tot), align="center", numfmt='0.00')
            else:
                put(ws, r, 4, "—", align="center")
            lv = x.get("level")
            put(ws, r, 5, lv if lv is not None else "—", align="center")
            border_row(ws, r, 5)
            r += 1
    ER._autofit_columns(ws, letters="ABCDEFG", max_row=r - 1)
    return ws


def add_shutdown_sheet(wb, d, title_suffix):
    """低电回充→关机：单独一页（汇总 / 每 1% 分段 / 详细数据）。"""
    used = set(wb.sheetnames)
    sheet_part = title_suffix.replace(" · ", "-").replace(" ", "")
    ws = wb.create_sheet(safe_sheet("低电关机-" + sheet_part, used))
    for c, w in zip("ABCDEFGH", [10, 20, 12, 12, 16, 16, 20, 12]):
        ws.column_dimensions[c].width = w
    ws.merge_cells("A1:H1")
    put(ws, 1, 1, "低电至关机续航", align="center", size=16, border=False)
    ws.cell(row=1, column=1).font = ER.TITLE_FONT
    ws.row_dimensions[1].height = 34
    ws.merge_cells("A2:H2")
    put(ws, 2, 1, "%s    起始 ≤%s%%    关机阈值 %s%%    采样间隔 %s 秒"
        "    生成时间: %s"
        % (sheet_part, d.get("low_battery"), d.get("shutdown_battery"),
           d.get("sample_interval"), time.strftime("%Y-%m-%d %H:%M:%S")),
        color="808080", align="center", size=9, border=False)
    border_row(ws, 2, 8)

    samples = d.get("samples") or []
    start = samples[0]["ts"] if samples else 0
    total = float(d.get("shutdown_seconds") or 0) / 60.0
    start_b = d.get("start_battery")
    end_b = d.get("end_battery")
    r = 4
    r = section(ws, r, "一、汇总", 8)
    r = kv(ws, r, "续航时间(低电→关机, 分钟)", total, 8, '0" 分钟"')
    r = kv(ws, r, "开始时间", d.get("start_time", ""), 8)
    r = kv(ws, r, "关机时间", d.get("end_time", ""), 8)
    r = kv(ws, r, "起始电量", "%s%%" % start_b if start_b is not None
           else "—", 8)
    r = kv(ws, r, "关机电量", "%s%%" % end_b if end_b is not None else "—", 8)
    if isinstance(start_b, int) and isinstance(end_b, int):
        r = kv(ws, r, "总耗电", "%d%%" % (start_b - end_b), 8)
    r = kv(ws, r, "有效采样", len(samples), 8)
    r += 1

    steps = d.get("percent_steps") or []
    if steps:
        r = section(ws, r, "每 1% 分段用时", 8)
        r = header(ws, r, ["电量", "到达时刻", "距开始(分钟)"])
        for x in steps:
            put(ws, r, 1, "%s%%" % x.get("level"), align="center")
            put(ws, r, 2, time.strftime(
                "%H:%M:%S", time.localtime(x.get("ts", 0))), align="center")
            put(ws, r, 3, x.get("elapsed", 0) / 60.0, align="center",
                numfmt='0" 分钟"')
            border_row(ws, r, 3)
            r += 1
        r += 1

    r = section(ws, r, "二、详细数据", 8)
    r = header(ws, r, ["序号", "时间", "电量", "变化",
                       "距上次变化(分钟)", "累计用时(分钟)", "状态",
                       "电池电压(V)"])
    prev = None
    idx = 0
    for s in samples:
        b = s.get("battery")
        if prev is None or b != prev.get("battery"):
            idx += 1
            delta = 0 if prev is None else b - prev["battery"]
            gap = 0 if prev is None else (s["ts"] - prev["ts"]) / 60.0
            put(ws, r, 1, idx, align="center")
            put(ws, r, 2, time.strftime(
                "%H:%M:%S", time.localtime(s["ts"])), align="center")
            put(ws, r, 3, "%s%%" % b, align="center")
            put(ws, r, 4, "%+d%%" % delta if prev else "—", align="center")
            put(ws, r, 5, gap if prev else "—", align="center",
                numfmt='0" 分钟"' if prev else None)
            put(ws, r, 6, (s["ts"] - start) / 60.0, align="center",
                numfmt='0" 分钟"')
            put(ws, r, 7, "低电回充" if "charge" in str(s.get("status"))
                or "back" in str(s.get("status")) else "运行", align="center")
            v = s.get("battery_vol")
            put(ws, r, 8, "%.2f" % v if isinstance(v, (int, float)) else "—",
                align="center")
            border_row(ws, r, 8)
            prev = s
            r += 1
    ER._autofit_columns(ws, letters="ABCDEFGH", max_row=r - 1)
    add_battery_chart(
        ws, samples,
        [x for x in (d.get("percent_steps") or [])
         if x.get("level") is not None and int(x["level"]) % 5 == 0])
    return ws


def build_report(run_dirs, out=None):
    """把多个 run 目录生成一份 Excel，返回实际保存路径。

    GUI / CLI 共用；run_dirs 里每个目录都要有 result.json。
    """
    runs = []
    for d in run_dirs:
        p = os.path.join(d, "result.json")
        runs.append((d, json.load(open(p, encoding="utf-8"))))
    first = runs[0][1]
    out = out or os.path.join(
        os.path.dirname(os.path.abspath(runs[0][0])),
        "续航测试_%s.xlsx" % time.strftime("%Y%m%d_%H%M%S"))

    wb = Workbook()
    ws = wb.active
    ws.title = "汇总"
    for c, w in zip("ABCDEFGHI", [6, 14, 26, 16, 10, 13, 11, 15, 22]):
        ws.column_dimensions[c].width = w
    ws.merge_cells("A1:I1")
    put(ws, 1, 1, "续航测试报告", align="center", size=16, border=False)
    ws.cell(row=1, column=1).font = ER.TITLE_FONT
    ws.row_dimensions[1].height = 40
    ws.merge_cells("A2:I2")
    put(ws, 2, 1, "生成时间: %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
        color="808080", align="center", size=9, border=False)
    ws.merge_cells("A3:I3")
    put(ws, 3, 1, "设备SN: %s    固件版本: %s    测试项目: 续航测试"
        % (first.get("sn") or "—", first.get("firmware") or "—"),
        color="1F4E79", align="center", size=10, border=False, wrap=True)
    ws.row_dimensions[3].height = 28
    r = 5
    r = header(ws, r, ["序号", "测试项目", "测试条件", "执行挡位", "结果",
                       "续航(分钟)", "结束电量", "中途自动清扫", "结束时间"])
    ws.row_dimensions[5].height = 26
    sheets = []
    for i, (_d, d) in enumerate(runs, 1):
        is_sd = d.get("kind") == "shutdown"
        ws_fw = None
        if is_sd:
            cond = "低电回充至关机（≤%s%%）" % d.get("shutdown_battery")
            ws_batt = add_shutdown_sheet(wb, d, cond)
        else:
            cond = cond_label(d)
            ws_batt = add_battery_sheet(wb, d, cond)
            ws_fw = add_fan_water_sheet(wb, d, cond)
        sheets.append((cond, ws_batt.title))
        ok = bool(d.get("completed"))
        put(ws, r, 1, i, align="center")
        put(ws, r, 2, "低电关机续航" if is_sd else "续航测试",
            align="center")
        cell = put(ws, r, 3, cond, align="center")
        cell.hyperlink = Hyperlink(
            ref=cell.coordinate,
            location="'%s'!A1" % ws_batt.title.replace("'", "''"))
        cell.font = ER.LINK_FONT
        if is_sd:
            exec_label = "—"
        else:
            water = d.get("water_level")
            exec_label = "吸力%s / 水量%s" % (
                d.get("fan_level"), "关" if not water else water)
        cell = put(ws, r, 4, exec_label, align="center")
        if ws_fw is not None:
            cell.hyperlink = Hyperlink(
                ref=cell.coordinate,
                location="'%s'!A1" % ws_fw.title.replace("'", "''"))
            cell.font = ER.LINK_FONT
        put(ws, r, 5, "PASS" if ok else "FAIL", bold=True, align="center",
            fill="C6EFCE" if ok else "FFC7CE",
            color="006100" if ok else "9C0006")
        seconds = (d.get("shutdown_seconds") if is_sd
                   else d.get("wall_seconds"))
        put(ws, r, 6, float(seconds or 0) / 60.0,
            align="center", numfmt='0" 分钟"')
        end_battery = (d.get("end_battery") if is_sd
                       else d.get("return_battery"))
        put(ws, r, 7, "%s%%" % end_battery if end_battery is not None
            else "—", align="center")
        put(ws, r, 8, "—" if is_sd else "%s 次" % d.get("restarts", 0),
            align="center")
        put(ws, r, 9, d.get("end_time", ""), align="center")
        border_row(ws, r, 9)
        r += 1
    done = sum(1 for _d, d in runs if d.get("completed"))
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=9)
    put(ws, r, 1, "测试结论: 完成 %d/%d；共 %d 项"
        % (done, len(runs), len(runs)), bold=True, align="center")
    border_row(ws, r, 9)
    ws.row_dimensions[r].height = 28
    failed = [d for _d, d in runs if not d.get("completed")]
    if failed:
        r += 1
        reasons = []
        for d in failed:
            label = ("低电关机" if d.get("kind") == "shutdown"
                     else cond_label(d))
            reasons.append("%s：%s" % (label, d.get("end_reason") or "未完成"))
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=9)
        put(ws, r, 1, "失败原因: " + "；".join(reasons), bold=True,
            color="9C0006", wrap=True)
        border_row(ws, r, 9)
        ws.row_dimensions[r].height = 32
    ER._autofit_columns(ws, letters="ABCDEFGHI", max_row=r)
    for rr in (5,):
        ws.row_dimensions[rr].height = 26

    base, i = out[:-5], 1
    while True:
        try:
            wb.save(out)
            break
        except PermissionError:
            i += 1
            out = "%s_%d.xlsx" % (base, i)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    print(build_report(args.runs, args.out))


if __name__ == "__main__":
    main()
