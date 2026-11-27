# -*- coding: utf-8 -*-
"""循环清扫 Excel 报告（样式与自动化/续航报告一致）。"""
import os
import time

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

from core.excel_report import ExcelReport as ER

MODE = {"sweepOnly": "仅扫", "sweepMop": "扫拖", "mopOnly": "单拖",
        "sweepThenMop": "先扫后拖"}


def _put(ws, r, c, v, *, bold=False, fill=None, color=None, align="left",
         size=10, numfmt=None, merge_to=None, wrap=False, border=True):
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
        cell.number_format = numfmt
    if border:
        for cc in range(c, (merge_to or c) + 1):
            ws.cell(row=r, column=cc).border = ER.BORDER
    return cell


def _header(ws, r, labels):
    for i, h in enumerate(labels, 1):
        _put(ws, r, i, h, bold=True, fill="4472C4", color="FFFFFF",
             align="center", size=11)
    return r + 1


def _section(ws, r, text, ncols):
    _put(ws, r, 1, text, bold=True, color="1F4E79", size=12,
         merge_to=ncols)
    return r + 1


def _kv(ws, r, k, v, ncols, numfmt=None):
    _put(ws, r, 1, k, bold=True)
    _put(ws, r, 2, v, merge_to=ncols, numfmt=numfmt)
    return r + 1


def _autofit(ws, letters, min_w=10, max_w=48):
    from openpyxl.utils import get_column_letter
    for idx, col in enumerate(ws.columns, 1):
        letter = get_column_letter(idx)
        if letter not in letters:
            continue
        longest = 0
        for cell in col:
            v = cell.value
            if v is None:
                continue
            text = str(v)
            ln = sum(2 if ord(ch) > 127 else 1 for ch in text)
            longest = max(longest, ln)
        ws.column_dimensions[letter].width = min(
            max(min_w, longest + 4), max_w)


def _combo_text(combo):
    if not combo:
        return "—"
    mode, fan, water = combo
    text = "%s · 吸力%s" % (MODE.get(mode, mode), fan)
    if mode in ("sweepMop", "mopOnly", "sweepThenMop"):
        text += " · 水量%s" % water
    return text


def build_report(result, out=None):
    """根据 run_loop_clean 的结果生成 Excel，返回路径。"""
    base_dir = os.path.dirname(out) if out else None
    if out is None:
        base_dir = os.path.join("reports", "循环清扫", "loop_report")
        out = os.path.join(base_dir, "循环清扫_报告.xlsx")
    else:
        base_dir = os.path.dirname(out)
    os.makedirs(base_dir, exist_ok=True)

    wb = Workbook()
    ws = wb.active
    ws.title = "汇总"
    for c, w in zip("ABCDEFGH", [10, 20, 18, 18, 16, 16, 14, 14]):
        ws.column_dimensions[c].width = w
    ws.merge_cells("A1:H1")
    _put(ws, 1, 1, "循环清扫报告", align="center", size=16, border=False)
    ws.cell(row=1, column=1).font = ER.TITLE_FONT
    ws.row_dimensions[1].height = 32
    ws.merge_cells("A2:H2")
    _put(ws, 2, 1, "生成时间：%s"
         % time.strftime("%Y-%m-%d %H:%M:%S"),
         color="808080", align="center", size=9, border=False)

    r = 4
    r = _section(ws, r, "一、测试条件", 8)
    r = _kv(ws, r, "设备 IP", result.get("ip", ""), 8)
    r = _kv(ws, r, "清扫电量", "%s%%" % result.get("clean_battery", "—"), 8)
    r = _kv(ws, r, "回充电量", "%s%%" % result.get("recharge_battery", "—"), 8)
    rounds_limit = result.get("max_rounds") or 0
    r = _kv(ws, r, "清扫次数", "无限循环" if not rounds_limit
            else rounds_limit, 8)
    combos = result.get("combos") or []
    r = _kv(ws, r, "组合",
            "；".join(_combo_text(c) for c in combos) or "—", 8)
    r += 1

    r = _section(ws, r, "二、测试结果", 8)
    rounds = result.get("rounds") or []
    total_seconds = sum(float(x.get("seconds") or 0) for x in rounds)
    r = _kv(ws, r, "完成轮次", len(rounds), 8)
    r = _kv(ws, r, "总清扫时长",
            round(total_seconds / 60.0, 1), 8, numfmt='0.0" 分钟"')
    r = _kv(ws, r, "报错次数", len(result.get("errors") or []), 8)
    r = _kv(ws, r, "结束原因", result.get("end_reason") or "—", 8)
    r = _kv(ws, r, "开始时间", result.get("start_time") or "—", 8)
    r = _kv(ws, r, "结束时间", result.get("end_time") or "—", 8)

    # 轮次明细
    ws2 = wb.create_sheet("轮次明细")
    for c, w in zip("ABCDEFGHI", [6, 26, 20, 20, 12, 12, 22, 14, 10]):
        ws2.column_dimensions[c].width = w
    r = 1
    r = _header(ws2, r, ["序号", "组合", "开始时间", "结束时间",
                         "开始电量", "结束电量", "结束状态",
                         "清扫时长(分钟)", "报错数"])
    errs = result.get("errors") or []
    for i, item in enumerate(rounds, 1):
        no = item.get("no") or i
        n_err = sum(1 for e in errs if e.get("round") == no)
        _put(ws2, r, 1, no, align="center")
        _put(ws2, r, 2, _combo_text(item.get("combo")))
        _put(ws2, r, 3, item.get("start_time") or "—", align="center")
        _put(ws2, r, 4, item.get("end_time") or "—", align="center")
        sb = item.get("start_battery")
        eb = item.get("end_battery")
        _put(ws2, r, 5, "%s%%" % sb if isinstance(sb, int) and sb >= 0
             else "—", align="center")
        _put(ws2, r, 6, "%s%%" % eb if isinstance(eb, int) and eb >= 0
             else "—", align="center")
        _put(ws2, r, 7, (item.get("end_status") or "—")
             .replace("work_status_", ""), align="center")
        _put(ws2, r, 8, round(float(item.get("seconds") or 0) / 60.0, 1),
             align="center", numfmt='0.0')
        _put(ws2, r, 9, n_err, align="center")
        r += 1
    _autofit(ws2, "ABCDEFGHI")

    # 事件明细（关键事件 + 报错，报错行红色文字）
    ws3 = wb.create_sheet("事件明细")
    for c, w in zip("ABCDE", [6, 20, 8, 16, 60]):
        ws3.column_dimensions[c].width = w
    r = 1
    r = _header(ws3, r, ["序号", "时间", "轮次", "事件", "说明"])
    events = result.get("events") or []
    if not events and errs:
        events = [{"time": e.get("time"), "round": e.get("round"),
                   "kind": "报错",
                   "detail": "%s（%s），状态 %s"
                   % (e.get("event", ""), e.get("name", ""),
                      e.get("status", ""))} for e in errs]
    for i, e in enumerate(sorted(events, key=lambda x: x.get("ts") or 0), 1):
        is_error = e.get("kind") == "报错"
        color = "C00000" if is_error else None
        _put(ws3, r, 1, i, align="center", color=color)
        _put(ws3, r, 2, e.get("time") or "—", align="center", color=color)
        _put(ws3, r, 3, e.get("round") or "—", align="center", color=color)
        _put(ws3, r, 4, e.get("kind") or "", align="center", color=color,
             bold=is_error)
        _put(ws3, r, 5, e.get("detail") or "", color=color)
        r += 1
    _autofit(ws3, "ABCDE", max_w=80)

    wb.save(out)
    return out
