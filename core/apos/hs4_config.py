# -*- coding: utf-8 -*-
"""HS4 项目配置清单（灯显/语音/故障码 xlsx）解析与故障模拟对照。

清单文件（桌面 `HS4_LED_Voice_Config.xlsx`）有三个 sheet：

* 主机语音定义   语音编号 Qxxx / 触发条件 / 中文 / 英文 / 停机状态
* 主机灯显定义   场景化的中文灯效描述（白色快闪、红色慢闪…）
* SS故障消息上报  Push ID（DA_RC_E_xxx）/ ErrorCode_NNN / 中文场景 / 级别

设备日志里的语音序号 `beep:066` 与 `Q066` 是同一个编号（去前导零），
故障码 `code=156` 与 `ErrorCode_156` 同理。清单本身没有事件 identifier，
所以「事件 → 期望语音/灯显」仍以事件配置 JSON 为准，这里负责：

1. 提供清单里的语音文案、灯显词表、故障码/推送文案；
2. 判断实测值是否在清单里（缺项标「清单未覆盖」，不算失败）；
3. 生成 Excel 测试结果报告（用户 2026-11-12）。
"""

import os
import re
import time

#: 缺项状态（不算失败）
STATUS_UNCOVERED = "清单未覆盖"
STATUS_OK = "通过"
STATUS_MISMATCH = "不一致"
STATUS_MISSING = "未捕获"
STATUS_UNCONFIGURED = "AP平台未配置"
STATUS_VOICE_CANCELLED = "语音取消"
#: PRD 要求该语音, 但机器人 /tmp/music 里缺文件
STATUS_VOICE_PACKAGE_MISSING = "语音包缺失"
#: PRD 有该语音/场景, 但 AP 平台配置里没有对应事件(不参与通过率)
STATUS_NO_EVENT = "无对应事件"
STATUS_NONE = "无"


def voice_no(value):
    """Q066 / 066 / 66 / beep: 066 → 66。"""
    m = re.search(r"\d+", str(value or ""))
    return int(m.group(0)) if m else None


def fault_no(value):
    """ErrorCode_156 / DA_RC_E_156 / 156 → 156。"""
    m = re.search(r"(\d+)\s*$", str(value or ""))
    if not m:
        m = re.search(r"\d+", str(value or ""))
    return int(m.group(0)) if m else None


def norm_text(text):
    """灯显文案归一化：去掉空白/标点，白灯≈白色、红灯≈红色。"""
    s = re.sub(r"[\s,，。.;；:：、()（）/\-]+", "", str(text or ""))
    return s.replace("白灯", "白色").replace("红灯", "红色")


def _scene_led(text):
    """PRD 灯显表格里的灯效文本；「恢复当前状态」这类动态描述不算期望。"""
    s = str(text or "").strip()
    if not s or any(k in s for k in ("当前状态", "恢复", "→", "->")):
        return ""
    return s.replace("双灯", "").strip()


def _led_norm(text):
    """灯效文案进一步归一化: 常亮≈长亮、熄灭≈关闭, 忽略序号。"""
    s = re.sub(r"\d+", "", norm_text(text))
    return s.replace("常亮", "长亮").replace("熄灭", "关闭")


def led_effect_id(cn, vocab):
    """PRD 中文灯效 → 灯效 identifier（只用 AP 词表做翻译, 不作期望来源）。"""
    want = _led_norm(cn)
    if not want:
        return ""
    for eid, name in (vocab or {}).items():
        if _led_norm(name) == want:
            return str(eid)
    for eid, name in (vocab or {}).items():
        have = _led_norm(name)
        if have and (want in have or have in want):
            return str(eid)
    return ""


def _scene_score(a, b):
    """灯显场景文案的二字片段重合度（保留给需要泛化匹配的场景用）。"""
    if not a or not b:
        return 0.0
    ga = {a[i:i + 2] for i in range(len(a) - 1)}
    gb = {b[i:i + 2] for i in range(len(b) - 1)}
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / min(len(ga), len(gb))


def led_scene_for(project, text):
    """PRD 文案 → PRD 灯显清单里的场景（power/home 为中文灯效）。

    ★ 灯显期望来自 PRD 清单, 不用 AP 事件配置(用户 2026-11-17):
      否则等于拿被测配置当期望, 测不出 AP 与 PRD 的差异。
    ★ 只认明确的场景词, 不做泛化相似度匹配 —— 猜错会制造假失败。
    """
    scenes = (project or {}).get("led_scenes") or []
    want = norm_text(text)
    if not want or not scenes:
        return None
    hints = (
        ("唤醒", ("待机", "开机完成")),
        ("密码", ("密码错误", "配网失败")),
        ("配网失败", ("配网失败",)),
        ("联网失败", ("配网失败",)),
        ("配网超时", ("配网失败",)),
        ("重置wifi", ("wifi重置",)),
        ("休眠", ("休眠",)),
    )
    for kw, targets in hints:
        if kw not in want:
            continue
        for scene in scenes:
            scen = norm_text(scene.get("scenario"))
            if any(t in scen for t in targets):
                return scene
    return None


def load_project(path):
    """解析项目清单 xlsx → dict；失败抛异常，调用方提示。"""
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True)
    out = {"path": str(path), "voices": {}, "leds": [], "led_scenes": [],
           "faults": {}, "fault_by_push": {}}
    ws = wb["主机语音定义"] if "主机语音定义" in wb.sheetnames else None
    if ws is not None:
        for row in ws.iter_rows(min_row=2, values_only=True):
            qid = str(row[0] or "").strip()
            no = voice_no(qid)
            if not qid.upper().startswith("Q") or no is None:
                continue
            out["voices"].setdefault(no, {
                "qid": qid.upper(),
                "condition": str(row[1] or "").strip(),
                "name": str(row[3] or "").strip() or str(row[2] or "").strip(),
                "text": str(row[2] or "").strip(),
                "stop": str(row[6] or "").strip(),
            })
    ws = wb["主机灯显定义"] if "主机灯显定义" in wb.sheetnames else None
    if ws is not None:
        seen = set()
        scenes = []
        table = ""
        for row in ws.iter_rows(values_only=True):
            cells = [str(c or "").strip() for c in row]
            for cell in cells[1:4]:
                text = str(cell or "").strip()
                if (text and len(text) <= 24 and text not in seen
                        and (("灯" in text) or ("灭" in text)
                             or ("闪" in text) or ("呼吸" in text))):
                    seen.add(text)
                    out["leds"].append(text)
            flat = " ".join(cells)
            if cells[1] == "分类" and "过程" in cells[:5]:
                table = "flow"
                continue
            if cells[1] == "序号" and any("状态" in c for c in cells[2:4]):
                table = "state"
                continue
            if not any(cells) or "基站灯显方案" in flat:
                table = ""
                continue
            if table == "flow" and cells[2] and cells[3]:
                scenes.append({"scenario": cells[2],
                               "power": _scene_led(cells[3]),
                               "home": _scene_led(cells[3])})
            elif table == "state" and cells[2] and (cells[3] or cells[4]):
                scenes.append({"scenario": cells[2],
                               "power": _scene_led(cells[3]),
                               "home": _scene_led(cells[4])})
        out["led_scenes"] = [s for s in scenes
                             if s["power"] or s["home"]]
    ws = (wb["SS故障消息上报260904"]
          if "SS故障消息上报260904" in wb.sheetnames else None)
    if ws is not None:
        for row in ws.iter_rows(min_row=2, values_only=True):
            push = str(row[0] or "").strip()
            err = str(row[1] or "").strip()
            no = fault_no(err) if err else fault_no(push)
            if no is None:
                continue
            info = {
                "push": push,
                "scene": str(row[4] or "").strip() or str(row[3] or "").strip(),
                "level": str(row[6] or "").strip(),
            }
            out["faults"].setdefault(no, info)
            out["fault_by_push"].setdefault(push, info)
    return out


def voice_entry(project, beep):
    no = voice_no(beep)
    return (project or {}).get("voices", {}).get(no)


def fault_entry(project, code):
    no = fault_no(code)
    return (project or {}).get("faults", {}).get(no)


def led_known(project, text):
    """清单灯显词表里是否有这个中文效果（归一化后包含匹配）。"""
    want = norm_text(text)
    if not want:
        return False
    for item in (project or {}).get("leds", []):
        have = norm_text(item)
        if want and (want in have or have in want):
            return True
    return False


def _dimension(status, expected="", actual="", note=""):
    return {"status": status, "expected": expected, "actual": actual,
            "note": note}


def _voice_values(exp):
    """可接受的语音号: PRD 语音 + 该 AP 事件复用/语义覆盖后的语音。"""
    vals = []
    for value in ([exp.get("sound")]
                  + list(exp.get("acceptable_sounds") or [])):
        n = voice_no(value)
        if n is not None and n not in vals:
            vals.append(n)
    return vals


def _voice_label(vals, fallback=""):
    if not vals:
        return str(fallback or "")
    return "/".join(f"{v:03d}" if v < 1000 else str(v) for v in vals)


def verify(actual, expected, event, project=None):
    """对照事件配置 + 项目清单验证一次触发。返回分维度结果与总判定。

    actual: parse_verification() 的结果；expected: 事件配置 dict；
    project: load_project() 的结果（可为 None）。
    清单缺项记「清单未覆盖」，不算失败。
    """
    exp = (expected or {}).get(event) or {}
    beep = str(actual.get("beep") or "")
    led = actual.get("led") or {}
    code = str(actual.get("code") or "")
    out = {"voice": None, "led": None, "code": None, "fail": False,
           "configured": bool(exp)}

    # ── 语音 ──
    want = str(exp.get("sound") or "")
    want_vals = _voice_values(exp)
    want_label = _voice_label(want_vals, want)
    got_no = voice_no(beep)
    if exp.get("voice_cancelled"):
        v = _dimension(STATUS_VOICE_CANCELLED, "", beep)
        v["note"] = "PRD 标注不提供语音"
    elif not exp or not want_vals:
        v = _dimension(STATUS_UNCONFIGURED, want_label, beep)
    elif not beep:
        v = _dimension(STATUS_MISSING, want_label, "")
        out["fail"] = True
    elif got_no not in want_vals:
        v = _dimension(STATUS_MISMATCH, want_label, beep)
        out["fail"] = True
    else:
        v = _dimension(STATUS_OK, want_label, beep)
    vi = voice_entry(project, beep or want)
    if vi:
        v["note"] = f"{vi['qid']} {vi['name']}".strip()
    elif project is not None and (beep or want):
        v["note"] = STATUS_UNCOVERED
    out["voice"] = v

    # ── 灯显 ──
    want_led = exp.get("led") or {}
    if not exp or not want_led:
        l = _dimension(STATUS_UNCONFIGURED)
        l["expected"] = _led_text(want_led, exp)
        l["actual"] = _led_text(led, exp)
    else:
        diffs = [dev for dev, eff in want_led.items()
                 if str(led.get(dev) or "") != str(eff)]
        if not diffs:
            l = _dimension(STATUS_OK)
        elif all(not led.get(dev) for dev in diffs):
            l = _dimension(STATUS_MISSING)
            out["fail"] = True
        else:
            l = _dimension(STATUS_MISMATCH)
            out["fail"] = True
        l["expected"] = _led_text(want_led, exp)
        l["actual"] = _led_text(led, exp)
    if project is not None and (want_led or led):
        # 词表核对：期望/实际灯效的中文名都在清单里才标通过
        names = list(want_led.values()) + list(led.values())
        if names and all(led_known(project, _effect_cn(expected, x))
                         for x in names):
            l["note"] = "清单词条 ✓"
        else:
            l["note"] = STATUS_UNCOVERED
    out["led"] = l

    # ── 故障码 ──
    # 用户 2026-11-16: 故障码不做一致性判断, 只检查设备有没有上报。
    # 设备上报了任意故障码就算通过; 只有 PRD/AP 期望有码而设备没上报才算失败。
    want_code = str(exp.get("code") or "")
    if code:
        c = _dimension(STATUS_OK, want_code, code)
        if project is not None:
            fi = fault_entry(project, code)
            if fi:
                c["note"] = f'{fi["push"]} {fi["scene"]}'.strip()
    elif want_code:
        c = _dimension(STATUS_MISSING, want_code, "")
        out["fail"] = True
    else:
        c = _dimension(STATUS_NONE)
    out["code"] = c

    if out["fail"]:
        out["overall"] = STATUS_MISMATCH
    elif not exp:
        out["overall"] = STATUS_UNCONFIGURED
    elif exp.get("voice_cancelled"):
        out["overall"] = STATUS_VOICE_CANCELLED
    elif (out["voice"]["status"] == STATUS_OK
          or out["led"]["status"] == STATUS_OK
          or out["code"]["status"] == STATUS_OK):
        out["overall"] = STATUS_OK
    elif "清单未覆盖" in (out["voice"].get("note"),
                       out["led"].get("note")):
        out["overall"] = STATUS_UNCOVERED
    else:
        out["overall"] = STATUS_UNCONFIGURED
    return out


def _effect_cn(expected, effect):
    """灯效 identifier → 配置里的中文名（红色慢闪等）。"""
    for exp in (expected or {}).values():
        for dev, meta in (exp.get("led_names") or {}).items():
            if meta.get("effect_id") == effect:
                return meta.get("effect") or effect
    return effect


def _led_text(led, exp=None):
    from core.apos import faults
    if not led:
        return ""
    names = dict((exp or {}).get("led_names") or {})
    for dev, label in (("rgb_led__home", "回充灯"),
                       ("rgb_led__start", "电源灯")):
        names.setdefault(dev, {"device": label, "effect_id": "",
                               "effect": ""})
    for eid, cn in ((exp or {}).get("led_vocab") or {}).items():
        names.setdefault(eid, {"effect_id": eid, "effect": cn})
    return faults.led_display(led, {"_": {"led_names": names}})


def status_display(status):
    """报告结论列用短标签, 详细原因写在问题说明里。"""
    return "未配置" if status == STATUS_UNCONFIGURED else str(status or "")


def _problem_text(row):
    """把一个事件的问题压缩成一句话(只写失败/缺项维度)。"""

    def dim(label, key):
        status = str(row.get(f"{key}_status") or "")
        exp = str(row.get(f"{key}_expected") or "")
        act = str(row.get(f"{key}_actual") or "")
        if status in ("", STATUS_OK, STATUS_NONE, STATUS_UNCONFIGURED):
            return ""
        if status == STATUS_MISSING:
            return f"{label}未捕获（期望 {exp or '-'}）"
        if status == STATUS_MISMATCH:
            return f"{label}不一致（期望 {exp or '-'}，实际 {act or '-'}）"
        if status == STATUS_VOICE_CANCELLED:
            return "语音取消"
        if status == STATUS_UNCOVERED:
            return f"{label}清单未覆盖（实际 {act or '-'}）"
        return f"{label}{status}（期望 {exp or '-'}，实际 {act or '-'}）"

    parts = [dim("语音", "voice"), dim("灯显", "led"),
             dim("故障码", "code")]
    miss_file = str(row.get("voice_package_missing") or "")
    if miss_file:
        parts.insert(0, f"语音包缺失（{miss_file}）")
        # 语音号虽然对得上, 但设备没有这个文件 —— 配了等于没配
        if (str(row.get("voice_status") or "") == STATUS_OK
                and str(row.get("prd_voice") or "").strip()):
            parts.append(
                f"语音配置指向 {str(row.get('prd_voice')).strip()}，"
                "但机器人无该文件")
    overall = str(row.get("overall") or "")
    if overall == STATUS_UNCONFIGURED:
        bare = [name for name, key in (("语音", "voice"), ("灯显", "led"))
                if str(row.get(f"{key}_status") or "") == STATUS_UNCONFIGURED]
        parts.insert(0, "AP平台未配置" + ("/".join(bare) if bare
                                       else "语音/灯显"))
    elif overall == STATUS_UNCOVERED:
        parts.insert(0, "PRD 清单未覆盖")
    text_parts = [p for p in parts if p]
    # 问题说明按「1. xxx / 2. xxx」分条换行, 更好读(用户 2026-11-17)
    if text_parts:
        return "\n".join(
            f"{i}. {p}" for i, p in enumerate(text_parts, 1))
    return str(row.get("note") or "") or "详见明细"


def _fit_height(values, widths, base=15, pad=6, cap=180):
    """按内容长度估算行高(cap 上限), 让多行内容/两行事件名不被截断。

    中文字符按 2 个宽度单位、ASCII 按 1 个; 每个单元格按列宽折行后取最大行数。
    """
    import math
    lines = 1
    for text, width in zip(values, widths):
        if text in (None, ""):
            continue
        count = 0
        for seg in str(text).split("\n"):
            units = sum(2 if ord(ch) > 127 else 1 for ch in seg)
            count += max(1, math.ceil(units / max(float(width) - 1, 1)))
        lines = max(lines, count)
    return min(cap, max(20, lines * base + pad))


def make_report(path, rows, project_path="", device_ip="", started=None,
                device_info=None, voice_check=None, unmatched=None):
    """生成故障模拟测试报告（汇总 + 明细），风格与自动化测试报告一致。

    汇总页突出总体结果和问题事件；明细页只保留关键列，问题说明压缩成一句话。
    unmatched: PRD 里有、但 AP 平台配置中没有对应事件的行 —— 不参与通过率,
    但必须在报告里列出来(用户 2026-11-17)。
    """
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    title_font = Font(name="Microsoft YaHei", size=16, bold=True,
                      color="1F4E79")
    head_font = Font(name="Microsoft YaHei", size=11, bold=True,
                     color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="4472C4")
    normal = Font(name="Microsoft YaHei", size=10)
    small = Font(name="Microsoft YaHei", size=9, color="808080")
    info_font = Font(name="Microsoft YaHei", size=10, color="1F4E79")
    pass_font = Font(name="Microsoft YaHei", size=10, bold=True,
                     color="006100")
    pass_fill = PatternFill("solid", fgColor="C6EFCE")
    fail_font = Font(name="Microsoft YaHei", size=10, bold=True,
                     color="9C0006")
    fail_fill = PatternFill("solid", fgColor="FFC7CE")
    warn_fill = PatternFill("solid", fgColor="FFF2CC")
    border = Border(*[Side(style="thin", color="B0B0B0")] * 4)
    center = Alignment(horizontal="center", vertical="center")
    center_wrap = Alignment(horizontal="center", vertical="center",
                            wrap_text=True)

    # 语音包缺失的 PRD 语音: 对应事件即使框架报了语音号, 设备也没有文件可播,
    # 必须算问题事件(用户 2026-11-17: 缺 8 个文件只列出 6 条问题)。
    missing_map = {}
    for item in (voice_check or {}).get("missing") or []:
        for qid in item.get("qids") or []:
            if qid:
                missing_map[str(qid)] = str(item.get("file") or "")
    if missing_map:
        rows = [dict(r) for r in rows]
        for r in rows:
            fname = missing_map.get(str(r.get("qid") or ""))
            if not fname:
                continue
            r["voice_package_missing"] = fname
            if r.get("overall") == STATUS_OK:
                r["overall"] = STATUS_VOICE_PACKAGE_MISSING
    voice_missing = sum(1 for r in rows
                        if r.get("overall") == STATUS_VOICE_PACKAGE_MISSING)

    total = len(rows)
    passed = sum(1 for r in rows if r.get("overall") == STATUS_OK)
    failed = sum(1 for r in rows if r.get("overall") == STATUS_MISMATCH)
    uncovered = sum(1 for r in rows if r.get("overall") == STATUS_UNCOVERED)
    cancelled = sum(1 for r in rows
                    if r.get("overall") == STATUS_VOICE_CANCELLED)
    unconfigured = (total - passed - failed - uncovered - cancelled
                    - voice_missing)
    hit = passed / max(total, 1) * 100
    problems = [r for r in rows if r.get("overall") != STATUS_OK]
    unmatched = [dict(r) for r in (unmatched or []) if isinstance(r, dict)]
    for r in unmatched:
        r.setdefault("overall", STATUS_NO_EVENT)
        r.setdefault("note", "AP平台配置中没有对应事件，未触发")
        r.setdefault("name", "未找到对应事件")
    all_problems = problems + unmatched
    # PRD 本来有 Q 号(HS4)才写 Q 号; 只有语音序号的 PRD 显示实际编号(0030)
    id_head = ("Q号" if any(str(r.get("qid") or "")[:1].upper() == "Q"
                            for r in rows) else "语音序号")

    def style_head(ws, row, ncols):
        for col in range(1, ncols + 1):
            cell = ws.cell(row=row, column=col)
            cell.font = head_font
            cell.fill = head_fill
            cell.alignment = center
            cell.border = border
        ws.row_dimensions[row].height = 24

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "汇总"
    ncols = 7
    # ── 标题 / 生成时间 / 设备信息 ──
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncols)
    ws.cell(row=1, column=1, value="故障模拟批量测试报告").font = title_font
    ws.cell(row=1, column=1).alignment = center
    ws.row_dimensions[1].height = 40
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=ncols)
    ws.cell(row=2, column=1,
            value="生成时间: " + time.strftime("%Y-%m-%d %H:%M:%S")
            ).font = small
    ws.cell(row=2, column=1).alignment = center
    info = device_info or {}
    # 报告不写设备 IP(用户 2026-11-13), 只保留型号/固件/基站
    bits = []
    for key, label in (("model", "型号"), ("firmware_version", "固件"),
                       ("base_station_version", "基站")):
        if info.get(key):
            bits.append(f"{label}: {info[key]}")
    ws.merge_cells(start_row=3, start_column=1, end_row=3, end_column=ncols)
    ws.cell(row=3, column=1,
            value="    ".join(bits) or "设备信息: —").font = info_font
    ws.cell(row=3, column=1).alignment = center
    ws.merge_cells(start_row=4, start_column=1, end_row=4, end_column=ncols)
    ws.cell(row=4, column=1,
            value=(f"PRD清单: {os.path.basename(project_path) or '未加载'}"
                   f"    开始: {started or '-'}"
                   f"    结束: {time.strftime('%Y-%m-%d %H:%M:%S')}")
            ).font = small
    ws.cell(row=4, column=1).alignment = center

    # ── 语音包检查(批量前 SSH 检查 /tmp/music) ──
    if voice_check is not None:
        missing = voice_check.get("missing") or []
        if voice_check.get("checked"):
            if missing:
                qids = []
                for item in missing:
                    for qid in item.get("qids") or []:
                        if qid and qid not in qids:
                            qids.append(qid)
                text = (f"缺失 {len(missing)} 个文件"
                        + (f"（{'、'.join(qids)}）" if qids else ""))
            else:
                text = "PRD 所需语音齐全"
        else:
            text = (f"未执行："
                    f"{str(voice_check.get('error') or '未知错误')[:160]}")
        # 标签只占 A 列会被截断; A:B 合并给标签, C:G 放结果
        ws.merge_cells(start_row=5, start_column=1, end_row=5,
                       end_column=2)
        label = ws.cell(row=5, column=1, value="语音包检查")
        label.font = head_font
        label.fill = head_fill
        label.alignment = center
        label.border = border
        ws.merge_cells(start_row=5, start_column=3, end_row=5,
                       end_column=ncols)
        cell = ws.cell(row=5, column=3, value=text)
        cell.font = fail_font if missing else normal
        cell.fill = warn_fill if (missing or not voice_check.get("checked")) \
            else pass_fill
        cell.alignment = center
        cell.border = border
        ws.row_dimensions[5].height = 24

    # ── 总体结果 ──
    for col, text in enumerate(
            ["总数", "通过", "不一致", "清单未覆盖", "AP平台未配置",
             "语音取消", "通过率"], 1):
        ws.cell(row=6, column=col, value=text)
    style_head(ws, 6, ncols)
    values = [total, passed, failed, uncovered, unconfigured, cancelled,
              f"{hit:.1f}%"]
    for col, val in enumerate(values, 1):
        cell = ws.cell(row=7, column=col, value=val)
        cell.font = normal
        cell.alignment = center
        cell.border = border
    for col in (2, 3, 4, 5, 6):
        cell = ws.cell(row=7, column=col)
        if col == 2 and passed == total and total:
            cell.font, cell.fill = pass_font, pass_fill
        elif col == 3 and failed:
            cell.font, cell.fill = fail_font, fail_fill
        elif col in (4, 5, 6) and cell.value:
            cell.fill = warn_fill
        elif col == 2:
            cell.font = pass_font
    rate = ws.cell(row=7, column=7)
    rate.font = Font(name="Microsoft YaHei", size=11, bold=True,
                     color="006100" if failed == 0 else "9C0006")

    # 未匹配事件 / 语音包缺失单独说明: 不参与通过率, 但报告里必须看得到
    notes = []
    if unmatched:
        notes.append(f"PRD 有、AP 平台无对应事件: {len(unmatched)} 条")
    if voice_missing:
        notes.append(f"语音包缺失: {voice_missing} 条（对应事件设备无语音文件）")
    if notes:
        ws.merge_cells(start_row=8, start_column=1, end_row=8,
                       end_column=ncols)
        note_text = ("；".join(notes)
                     + "（不参与通过率，详见问题事件与明细）")
        cell = ws.cell(row=8, column=1, value=note_text)
        cell.font = info_font
        cell.fill = warn_fill
        cell.alignment = center
        cell.border = border
        ws.row_dimensions[8].height = _fit_height(
            [note_text], [sum((6, 12, 24, 22, 16, 9, 34))])

    # ── 问题事件 ──
    ws.merge_cells(start_row=9, start_column=1, end_row=9, end_column=ncols)
    title = ws.cell(row=9, column=1,
                    value=(f"问题事件（{len(all_problems)} 条）"
                           if all_problems else "测试结果"))
    title.font = Font(name="Microsoft YaHei", size=12, bold=True,
                      color="9C0006" if all_problems else "006100")
    title.alignment = Alignment(horizontal="left", vertical="center")
    if not all_problems:
        ws.merge_cells(start_row=10, start_column=1, end_row=10,
                       end_column=ncols)
        cell = ws.cell(row=10, column=1, value="全部事件验证通过")
        cell.font, cell.fill = pass_font, pass_fill
        cell.alignment = center
        cell.border = border
        last = 10
    else:
        heads = ["序号", id_head, "PRD事件内容", "事件", "中文名", "结论",
                 "问题说明"]
        for col, text in enumerate(heads, 1):
            ws.cell(row=10, column=col, value=text)
        style_head(ws, 10, ncols)
        last = 10
        for idx, row in enumerate(all_problems, 1):
            last = 10 + idx
            values = [idx, row.get("qid", ""), row.get("prd_content", ""),
                      row.get("event", ""), row.get("name", ""),
                      status_display(row.get("overall")), _problem_text(row)]
            for col, val in enumerate(values, 1):
                cell = ws.cell(row=last, column=col, value=val)
                cell.font = normal
                cell.alignment = center_wrap
                cell.border = border
            cc = ws.cell(row=last, column=6)
            cc.alignment = center
            if cc.value == STATUS_MISMATCH:
                cc.font, cc.fill = fail_font, fail_fill
            else:
                cc.fill = warn_fill
            # 问题说明左对齐更易读(用户 2026-11-17)
            ws.cell(row=last, column=7).alignment = Alignment(
                horizontal="left", vertical="center", wrap_text=True)
            ws.row_dimensions[last].height = _fit_height(
                values, (6, 12, 24, 22, 16, 9, 34))

    # ── 底部总计 ──
    footer_row = last + 2
    ws.merge_cells(start_row=footer_row, start_column=1,
                   end_row=footer_row, end_column=ncols)
    cell = ws.cell(
        row=footer_row, column=1,
        value=(f"全部事件: {total}    通过: {passed}    不一致: {failed}    "
               f"未覆盖/AP平台未配置: {uncovered + unconfigured}    "
               f"语音取消: {cancelled}    通过率: {hit:.1f}%"
               + (f"    无对应事件: {len(unmatched)}"
                  if unmatched else "")
               + (f"    语音包缺失: {voice_missing}"
                  if voice_missing else "")))
    cell.font = Font(name="Microsoft YaHei", size=11, bold=True)
    cell.alignment = center
    cell.border = border
    ws.row_dimensions[footer_row].height = _fit_height(
        [str(cell.value or "")], [sum((6, 12, 24, 22, 16, 9, 34))])
    for col, width in zip("ABCDEFG", (6, 12, 24, 22, 16, 9, 34)):
        ws.column_dimensions[col].width = width

    # ── 明细(精简为关键列, 期望/实际合并, 全部居中) ──
    ws2 = wb.create_sheet("明细")

    def pair(exp_value, act_value):
        exp_text = str(exp_value or "")
        act_text = str(act_value or "")
        if exp_text and act_text and exp_text != act_text:
            return f"{exp_text} → {act_text}"
        return exp_text or act_text

    cols = ["序号", "时间", id_head, "PRD事件内容", "AP事件",
            "语音(期望/实际)", "灯显(期望/实际)", "故障码", "结论",
            "问题说明"]
    ws2.append(cols)
    style_head(ws2, 1, len(cols))
    detail_rows = list(rows) + unmatched
    for idx, row in enumerate(detail_rows, 1):
        event = str(row.get("event") or "")
        name = str(row.get("name") or "")
        ap_event = (f"{event}\n{name}" if name and event
                    else name or event)
        ws2.append([
            idx, row.get("time", ""), row.get("qid", ""),
            row.get("prd_content", ""), ap_event,
            pair(row.get("voice_expected"), row.get("voice_actual")),
            pair(row.get("led_expected"), row.get("led_actual")),
            str(row.get("code_actual") or ""),
            status_display(row.get("overall")),
            "" if row.get("overall") == STATUS_OK else _problem_text(row),
        ])
        r = ws2.max_row
        for col in range(1, len(cols) + 1):
            cell = ws2.cell(row=r, column=col)
            cell.font = normal
            cell.alignment = center_wrap
            cell.border = border
        # 语音/灯显不一致(或未捕获)时, 对应实际值单元格标黄
        for col, status_key in ((6, "voice_status"), (7, "led_status")):
            status = str(row.get(status_key) or "")
            if status in (STATUS_MISMATCH, STATUS_MISSING):
                ws2.cell(row=r, column=col).fill = warn_fill
        cc = ws2.cell(row=r, column=9)
        cc.alignment = center
        if cc.value == STATUS_OK:
            cc.font, cc.fill = pass_font, pass_fill
        elif cc.value == STATUS_MISMATCH:
            cc.font, cc.fill = fail_font, fail_fill
        else:
            cc.fill = warn_fill
        ws2.row_dimensions[r].height = _fit_height(
            [idx, row.get("time", ""), row.get("qid", ""),
             row.get("prd_content", ""), ap_event,
             pair(row.get("voice_expected"), row.get("voice_actual")),
             pair(row.get("led_expected"), row.get("led_actual")),
             str(row.get("code_actual") or ""),
             status_display(row.get("overall")),
             "" if row.get("overall") == STATUS_OK else _problem_text(row)],
            (6, 18, 12, 34, 30, 16, 34, 10, 10, 50))
        # 问题说明左对齐(用户 2026-11-17)
        ws2.cell(row=r, column=10).alignment = Alignment(
            horizontal="left", vertical="center", wrap_text=True)
    widths = [6, 18, 12, 34, 30, 16, 34, 10, 10, 50]
    for i, width in enumerate(widths, 1):
        ws2.column_dimensions[
            openpyxl.utils.get_column_letter(i)].width = width
    ws2.freeze_panes = "A2"
    if rows:
        ws2.auto_filter.ref = f"A1:J{ws2.max_row}"

    if voice_check is not None:
        ws3 = wb.create_sheet("语音包检查")
        cols = [id_head, "所需文件", "PRD内容", "语音类型", "状态"]
        ws3.append(cols)
        style_head(ws3, 1, len(cols))
        missing = voice_check.get("missing") or []
        if missing:
            for item in missing:
                for src in item.get("sources") or [{}]:
                    ws3.append([src.get("qid", ""), item.get("file", ""),
                                src.get("condition", ""),
                                src.get("field", ""), "缺失"])
        elif voice_check.get("checked"):
            ws3.append(["", "", "PRD 所需语音齐全", "", "通过"])
        else:
            ws3.append(["", "", str(voice_check.get("error") or ""),
                        "", "未检查"])
        for r in range(2, ws3.max_row + 1):
            vals = [ws3.cell(row=r, column=c).value for c in
                    range(1, len(cols) + 1)]
            for c in range(1, len(cols) + 1):
                cell = ws3.cell(row=r, column=c)
                cell.font = normal
                cell.alignment = center_wrap
                cell.border = border
            ws3.row_dimensions[r].height = _fit_height(
                vals, (12, 12, 26, 10, 7))
        for i, width in enumerate((12, 12, 26, 10, 7), 1):
            ws3.column_dimensions[
                openpyxl.utils.get_column_letter(i)].width = width

    # 统一字体/居中/自动换行: 所有有内容的单元格
    from copy import copy
    for sheet in wb.worksheets:
        for row in sheet.iter_rows():
            for cell in row:
                if cell.value in (None, ""):
                    continue
                align = copy(cell.alignment)
                # 已显式设成左对齐的(问题说明)保留, 其余居中
                if align.horizontal not in ("left", "right", "justify"):
                    align.horizontal = "center"
                align.vertical = "center"
                align.wrap_text = True
                cell.alignment = align
                if str(cell.font.name or "") != "Microsoft YaHei":
                    font = copy(cell.font)
                    font.name = "Microsoft YaHei"
                    cell.font = font
                # 带数字的单元格全部按文本存/显示, 避免 0001 变 1、
                # 序号/计数被 Excel 当数值(用户 2026-11-17)
                if isinstance(cell.value, (int, float)) and not isinstance(
                        cell.value, bool):
                    cell.value = str(cell.value)
                cell.number_format = "@"

    wb.save(path)
    return path
