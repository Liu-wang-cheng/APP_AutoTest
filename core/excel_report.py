# -*- coding: utf-8 -*-
"""Excel 报告: 一个「汇总」sheet + 每个用例一个 sheet。

用例 sheet 列: A=步骤 B=结果 C=错误信息 D=截图
默认路径 reports/test_report_YYYYMMDD_HHMMSS.xlsx(时间戳命名防覆盖)。
"""
import os
import re
from datetime import datetime

from openpyxl import Workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.worksheet.hyperlink import Hyperlink
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

from core import timefmt as _tf
from core.driver import BASE_DIR


def case_sheet_name(case_name, run_no=None):
    """一张表(一次执行)的名字: **用例名 + 第几次执行**(用户要求 2026-10-11)。

    ★ 为什么按"次"分表: 同一用例重复跑 N 轮时, 原来所有轮的结果堆在同一张表里, 看不出
      哪几行属于第几轮; 顺带也解决了"同一个用例文件里有多个用例时共用一张表"。
    run_no: 从 1 开始的执行序号; 不给就只有用例名(单次调用/旧接口)。
    """
    name = str(case_name or "用例")
    return f"{name}-{run_no}" if run_no else name


class ExcelReport:
    TITLE_FONT = Font(name="Microsoft YaHei", size=16, bold=True, color="1F4E79")
    HEADER_FONT = Font(name="Microsoft YaHei", size=11, bold=True, color="FFFFFF")
    HEADER_FILL = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    NORMAL_FONT = Font(name="Microsoft YaHei", size=10)
    PASS_FONT = Font(name="Microsoft YaHei", size=10, bold=True, color="006100")
    PASS_FILL = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
    FAIL_FONT = Font(name="Microsoft YaHei", size=10, bold=True, color="9C0006")
    #: 汇总页里"用例名称"做成跳转链接(点一下直达那一次的详情页), 用 Excel 惯用的蓝色下划线
    LINK_FONT = Font(name="Microsoft YaHei", size=10, color="0563C1", underline="single")
    FAIL_FILL = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
    BORDER = Border(*[Side(style="thin", color="B0B0B0")] * 4)

    #: 截图单元格的行高预算(磅)与 D 列默认宽度(字符) —— 与旧版报告保持一致,
    #: **不改表格布局**, 只把图片按这个格子的大小放大。
    #: ★ 2026-11-05 用户反馈: 截图只占单元格左上角一小块。旧版把图片固定压到
    #:   120x210px, 而 D 列宽 20 字符(≈145px)、行高 214 磅(≈285px) → 图和格子都留空。
    #:   现在按单元格实际尺寸(像素)等比放大, 行高按 像素×0.75 换算(原来误把像素当磅)。
    SHOT_ROW_HEIGHT_PT = 214
    #: 图片四周留的内边距(px) —— 图片在 Excel 里画在单元格边框**之上**, 贴边会把
    #: 下边框/左右边框盖住(用户 2026-11-05 实测反馈)。12px 即使导出/打印时表格被
    #: 缩放到 96%, 图片仍不会压到边框(图片尺寸不随打印缩放, 行高会)。
    SHOT_PAD_PX = 12
    PX_PER_CHAR = 7.0
    PX_PER_POINT = 4.0 / 3.0     # 1 磅 = 4/3 px

    def __init__(self, path=None):
        # 默认按时间戳命名,历史报告不互相覆盖(不建子目录)
        if path is None:
            from core import timefmt
            ts = datetime.now().strftime(timefmt.RUN_TAG)
            reports_dir = os.path.join(BASE_DIR, "reports")
            os.makedirs(reports_dir, exist_ok=True)
            path = os.path.join(reports_dir, f"test_report_{ts}.xlsx")
        self.path = path
        self.wb = Workbook()
        self.summary_ws = self.wb.active
        self.summary_ws.title = "汇总"
        self._cases = {}
        # ★ 电量监测页只建一次(代码审查 2026-11-04): 原来按 sheet 名判重, 用例名恰好叫
        #   「电量监测」时会被误认为已建好 → 报告里根本没有电量数据。改用本对象的标记。
        self._battery_sheet = None
        # 设备信息(会话开始时由 conftest 填入真实值)
        self._device_info = {"sn": "待获取", "app_version": "待获取",
                             "固件版本": "待获取", "基站版本": "待获取"}

    def set_device_info(self, sn="", app_version=""):
        """填入真实设备信息,失败时保留占位符"""
        if sn:
            self._device_info["sn"] = sn
        if app_version:
            self._device_info["app_version"] = app_version

    def set_case_priority(self, case_name, priority):
        """记录用例优先级(汇总页"优先级"列, 用户 2026-10-15: 界面设置的优先级
        要写进报告) —— 用例还没入册时先建页(与 add_result 同一入口语义)"""
        if case_name not in self._cases:
            self._cases[case_name] = self._create_case_sheet(case_name)
        self._cases[case_name]["priority"] = str(priority or "")

    def add_result(self, case_name, passed, error="", screenshot=""):
        if case_name not in self._cases:
            self._cases[case_name] = self._create_case_sheet(case_name)

        case = self._cases[case_name]
        ws = case["sheet"]
        # ★ 记下这次执行的**结束时间**(汇总页要显示它, 用户要求 2026-10-12):
        #   最后一条结果的写入时刻 —— 报告写入点是每步跑完时, 秒级够用。
        case["ended"] = datetime.now()
        case["total"] += 1
        r = case["row"]
        status = "PASS" if passed else "FAIL"

        # 步骤、结果、错误(A-C)
        for col_idx, val in enumerate([case.get("_last_desc", ""), status, error], 1):
            cell = ws.cell(row=r, column=col_idx)
            cell.value = val
            cell.font = self.NORMAL_FONT
            cell.alignment = Alignment(vertical="center", wrap_text=(col_idx != 2))
            cell.border = self.BORDER

        # 结果着色
        sc = ws.cell(row=r, column=2)
        if passed:
            sc.font = self.PASS_FONT
            sc.fill = self.PASS_FILL
            case["passed"] += 1
        else:
            sc.font = self.FAIL_FONT
            sc.fill = self.FAIL_FILL
            case["failed"] += 1
            for c in (1, 3):
                ws.cell(row=r, column=c).fill = PatternFill(
                    start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")

        # 截图列边框
        ws.cell(row=r, column=4).border = self.BORDER
        ws.cell(row=r, column=2).alignment = Alignment(horizontal="center", vertical="center")

        # 嵌入截图(用户 2026-11-05: 要放进单元格里、别缩成左上角一小块)
        ws.row_dimensions[r].height = 30
        if screenshot and os.path.exists(screenshot):
            self._add_screenshot(ws, r, screenshot)

        case["row"] += 1

    def _add_screenshot(self, ws, row, path):
        """把截图等比缩放到**单元格内**并四周留统一内边距(不改列宽/表格布局)。

        ★ 用 TwoCellAnchor(从本行跨到下一行)而不是 OneCellAnchor:
          Excel 打印/导出时会缩放表格, 但**不缩放** OneCellAnchor 的图片 —— 行高缩了、
          图片没缩, 上下留白就不一致, 甚至压住下边框(2026-11-05 实测)。TwoCellAnchor
          的矩形由单元格坐标推导, 会跟着一起缩放, 所以屏幕和打印里上下留白都一致。
        ★ 锚点盒子按图片原始比例计算, 图片不会被拉伸变形。
        ★ 行高按 像素×0.75 换算成磅(旧版误把像素当磅, 行高多出 33%, 图显得更小)。
        """
        try:
            from PIL import Image as PImage
            from openpyxl.drawing.spreadsheet_drawing import (
                AnchorMarker, TwoCellAnchor)
            from openpyxl.utils.units import pixels_to_EMU

            img = XLImage(path)
            pi = PImage.open(path)
            # 单元格可用尺寸(px): 列宽(字符)→像素 ≈ 7×字符+5; 行高(磅)→像素 ×4/3
            col_w = ws.column_dimensions["D"].width or 20
            cell_w = int(self.PX_PER_CHAR * col_w + 5)
            cell_h = int(self.SHOT_ROW_HEIGHT_PT * self.PX_PER_POINT)
            pad = self.SHOT_PAD_PX
            ratio = min((cell_w - 2 * pad) / float(pi.width),
                        (cell_h - 2 * pad) / float(pi.height))
            box_w = max(1.0, pi.width * ratio)
            box_h = max(1.0, pi.height * ratio)
            pad_x = max(0.0, (cell_w - box_w) / 2.0)      # 居中的左右留白
            pad_y = float(pad)                            # 上 = 下 = 12px
            img.width, img.height = int(box_w), int(box_h)

            # 行高(磅): 图片高度 + 上下留白; 横屏截图行会自动变矮, 不留大片空白
            ws.row_dimensions[row].height = max(
                30.0, round((box_h + 2 * pad_y) / self.PX_PER_POINT, 1))

            anchor = TwoCellAnchor(editAs="twoCell")
            # 左上角: D 列 + 本行
            anchor._from = AnchorMarker(
                col=3, colOff=pixels_to_EMU(pad_x), row=row - 1,
                rowOff=pixels_to_EMU(pad_y))
            # 右下角: 同一列, 底边落在下一行行首往上 pad_y 处(负偏移 = 行首上方)
            anchor.to = AnchorMarker(
                col=3, colOff=pixels_to_EMU(pad_x + box_w), row=row,
                rowOff=pixels_to_EMU(-pad_y))
            ws.add_image(img, anchor)
        except Exception:
            pass

    def set_step_desc(self, case_name, desc):
        """两段式设计: 只把描述存到该用例的 _last_desc,不写单元格;
        由下一次 add_result 取出写入 A 列 —— 即「先声明步骤名,再报结果」。
        """
        if case_name not in self._cases:
            self._cases[case_name] = self._create_case_sheet(case_name)
        self._cases[case_name]["_last_desc"] = desc

    def _style_header(self, cell):
        cell.font = self.HEADER_FONT
        cell.fill = self.HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = self.BORDER

    def _unique_sheet_title(self, base):
        """openpyxl 的 sheet 名限制: 非法字符替换 + 31 字截断 + 撞名追加 -2/-3…"""
        base = re.sub(r'[\\*?:/\[\]]', "_", str(base))[:31] or "用例"
        title, n = base, 2
        while title in self.wb.sheetnames:
            suffix = f"-{n}"
            title = base[:31 - len(suffix)] + suffix
            n += 1
        return title

    def _create_case_sheet(self, case_name):
        # ★ openpyxl 的 sheet 名不允许 [ ] : * ? / \ 这几个字符(实测: 传「温度: 设置」
        #   直接抛 ValueError: Invalid character : found in sheet title)。原实现只替换了
        #   / 和 \, 于是组名带「:」「[」「?」时 create_sheet 抛错 → 冒到 RunWorker.run
        #   的兜底 except → **整轮**以"执行异常"中断, 设备侧已跑的部分白跑。
        #   组名(module)是 GUI 里可自由编辑的文本, 很容易踩到。
        title = self._unique_sheet_title(case_name)
        ws = self.wb.create_sheet(title=title)
        for c, w in zip("ABCD", [28, 8, 28, 20]):
            ws.column_dimensions[c].width = w

        for ci, h in enumerate(["步骤", "结果", "错误信息", "截图"], 1):
            self._style_header(ws.cell(row=1, column=ci, value=h))
        ws.row_dimensions[1].height = 26
        return {"sheet": ws, "row": 2, "total": 0, "passed": 0, "failed": 0, "_last_desc": "",
                "ended": None}

    def _border_range(self, ws, r1, c1, r2, c2):
        for r in range(r1, r2 + 1):
            for c in range(c1, c2 + 1):
                ws.cell(row=r, column=c).border = self.BORDER

    # ── 电量监测(用户 2026-11-03) ─────────────────────────────────────────
    BATTERY_TITLE = "电量监测"

    def add_battery_report(self, samples, step_pct=5, interval=None):
        """新增一页「电量监测」: 上半汇总、下半详细。

        ★ 幂等: 一页只建一次(正常保存前调一次; 异常路径的部分报告也会调, 不能重复)。
        ★ 没有有效样本时写清原因, 不伪造数据。
        """
        from core.battery_monitor import build_points, build_summary, format_mmss

        if self._battery_sheet is not None:
            return self._battery_sheet
        ws = self.wb.create_sheet(self._unique_sheet_title(self.BATTERY_TITLE))
        self._battery_sheet = ws
        for col, w in zip("ABCDEFGH", [20, 12, 12, 10, 12, 14, 12, 14]):
            ws.column_dimensions[col].width = w
        summary = build_summary(samples, step_pct)
        points = build_points(samples, step_pct)

        def put(row, col, value, *, bold=False, fill=None, font_color=None,
                align="left", size=10):
            cell = ws.cell(row=row, column=col, value=value)
            cell.font = Font(name="Microsoft YaHei", size=size, bold=bold,
                             color=font_color or "000000")
            if fill:
                cell.fill = PatternFill(start_color=fill, end_color=fill,
                                        fill_type="solid")
            cell.alignment = Alignment(horizontal=align, vertical="center",
                                       wrap_text=False)
            cell.border = self.BORDER
            return cell

        def border_row(row, c1=1, c2=8):
            # ★ 合并单元格只有左上角带边框(用户 2026-11-04: "单元格边框加上") ——
            #   合并区与整行都补一遍, 表看起来才是完整网格。
            self._border_range(ws, row, c1, row, c2)

        def section(row, text):
            ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=8)
            put(row, 1, text, bold=True, font_color="1F4E79", size=12)
            border_row(row)
            return row + 1

        def header(row, labels):
            for ci, text in enumerate(labels, 1):
                put(row, ci, text, bold=True, fill="4472C4",
                    font_color="FFFFFF", align="center")
            border_row(row)               # 列数不足 8 时右侧也补齐边框
            return row + 1

        # ── 标题 ──
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=8)
        put(1, 1, "电量监测", bold=True, font_color="1F4E79", align="center", size=16)
        border_row(1)
        ws.row_dimensions[1].height = 34
        ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=8)
        sub = (f"步长 {int(step_pct or 1)}%    "
               f"采样间隔 {int(interval) if interval else '-'} 秒    "
               f"生成时间: {datetime.now().strftime(_tf.DATETIME)}")
        put(2, 1, sub, font_color="808080", align="center", size=9)
        border_row(2)

        r = 4
        r = section(r, "一、汇总")

        def kv(row, k, v):
            ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=8)
            put(row, 1, k, bold=True)
            put(row, 2, v)
            border_row(row)
            return row + 1

        if not summary["valid"]:
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=8)
            put(r, 1, "未采集到有效电量数据 —— 页面始终未展示电量, 或设备中途断开。",
                font_color="9C0006", align="center")
            border_row(r)
            r += 2
        else:
            r = kv(r, "总运行时长(分:秒)",
                   format_mmss(summary["total_seconds"]))
            r = kv(r, "起始电量", f"{summary['start_battery']}%")
            r = kv(r, "结束电量", f"{summary['end_battery']}%")
            # ★ 充电时总耗电可能为负 —— 如实写「电量上升 N%」, 不硬凑成 0
            drop = summary["total_drop"]
            r = kv(r, "总耗电", f"{drop}%" if drop >= 0 else f"电量上升 {-drop}%")
            r = kv(r, "平均每 1% 用时(分:秒)",
                   format_mmss(summary["average_per_1pct"])
                   if summary["average_per_1pct"] is not None else "无法计算(未下降)")
            r = kv(r, f"每 {int(step_pct or 1)}% 段平均用时(分:秒)",
                   format_mmss(summary["milestone_average"])
                   if summary["milestone_average"] is not None else "暂无完整里程碑")
            # ★ 最长/最短段(用户 2026-11-04): 带区间, 便于定位哪一档掉电最快/最慢
            longest = summary.get("milestone_longest")
            shortest = summary.get("milestone_shortest")
            if longest and shortest:
                r = kv(r, f"每 {int(step_pct or 1)}% 段最长用时(分:秒)",
                       f"{format_mmss(longest['dt'])}   "
                       f"({longest['from']}% → {longest['to']}%)")
                r = kv(r, f"每 {int(step_pct or 1)}% 段最短用时(分:秒)",
                       f"{format_mmss(shortest['dt'])}   "
                       f"({shortest['from']}% → {shortest['to']}%)")
            r = kv(r, "有效采样 / 读取失败",
                   f"{summary['valid']} / {summary['invalid']}")
            r += 1
            r = section(r, "按用例分段")
            r = header(r, ["用例/轮次", "起始电量", "结束电量", "耗电",
                           "用时(分:秒)", "平均每 1%(分:秒)"])
            for c in summary["cases"]:
                put(r, 1, c["label"] or "(未标注)")
                put(r, 2, f"{c['start_battery']}%", align="center")
                put(r, 3, f"{c['end_battery']}%", align="center")
                put(r, 4, f"{c['drop']}%", align="center")
                put(r, 5, format_mmss(c["seconds"]), align="center")
                put(r, 6, format_mmss(c["average_per_1pct"])
                    if c["average_per_1pct"] is not None else "-", align="center")
                border_row(r)
                r += 1
            r += 1
            # ★ 每 N% 分段明细放**汇总**里(用户 2026-11-04: 目标电量不要单独占一行,
            #   体现在汇总记录就行) —— 详细表只保留每次 1% 的实际变化。
            milestones = points["milestones"]
            if milestones:
                r = section(r, f"每 {int(step_pct or 1)}% 分段用时")
                r = header(r, ["分段", "起始电量", "结束电量", "用时(分:秒)",
                               "平均每 1%(分:秒)", "到达时刻(分:秒)"])
                for m in milestones:
                    # 段起点取 milestone 自带值: 充电回升后 build_points 会重新锚定
                    seg_start = m.get("from_battery", summary["start_battery"])
                    seg_end = m["battery"]
                    seg_drop = max(1, seg_start - seg_end)
                    put(r, 1, f"{seg_start}% → {seg_end}%")
                    put(r, 2, f"{seg_start}%", align="center")
                    put(r, 3, f"{seg_end}%", align="center")
                    put(r, 4, format_mmss(m["dt"]), align="center")
                    put(r, 5, format_mmss(m["dt"] / seg_drop), align="center")
                    put(r, 6, format_mmss(m["t"]), align="center")
                    border_row(r)
                    r += 1
                r += 1

        r = section(r, "二、详细数据")
        r = header(r, ["序号", "电量的当前时间", "用例/轮次", "电量", "变化",
                       "距上次变化(分:秒)", "累计用时(分:秒)"])
        # ★ 只列**每次 1% 的实际变化**(用户 2026-11-04): 里程碑不在这里单独占行,
        #   它的分段用时在「汇总」的每 N% 分段表里。
        raw = points["raw"]
        if not raw:
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=8)
            put(r, 1, "无电量变化记录。", align="center", font_color="666666")
            border_row(r)
        for i, p in enumerate(raw, 1):
            put(r, 1, i, align="center")
            # ★ 「电量的当前时间」= 采样时的本地(北京时间)时刻, 不是用时
            #   (用户 2026-11-04); 老数据没有 wall 时退回相对时间。
            put(r, 2, p.get("wall") or format_mmss(p["t"]), align="center")
            put(r, 3, p.get("label") or "")
            put(r, 4, f"{p['battery']}%", align="center")
            put(r, 5, f"{p['delta']:+d}%", align="center")
            put(r, 6, format_mmss(p.get("dt")), align="center")
            put(r, 7, format_mmss(p["t"]), align="center")
            border_row(r)
            r += 1

        # ── 右侧电量-时间曲线(用户 2026-11-04) ──
        last_table_row = r - 1               # 曲线数据写在隐藏列, 列宽只看正文表
        self._add_battery_chart(ws, samples, points["milestones"])
        # ★ 列宽自适应内容(用户 2026-11-04: 避免显示不全) —— openpyxl 不会自动调,
        #   这里按最长的非合并单元格文本估算(中文按 2 个字符宽)。
        self._autofit_columns(ws, max_row=last_table_row)
        return ws

    #: 曲线最多画这么多点(代码审查 2026-11-04): 长测(几小时/几秒一采)会写出上万行
    #: 隐藏数据, 文件膨胀且 Excel 打开变慢; 曲线形状在 600 点上已经看不出区别。
    CHART_MAX_POINTS = 600

    @classmethod
    def _downsample_for_chart(cls, pts, limit=None):
        """均匀抽稀, 但**首末点必须保留**(曲线的起点/终点不能丢)。"""
        limit = int(limit or cls.CHART_MAX_POINTS)
        if len(pts) <= limit:
            return list(pts)
        step = (len(pts) - 1) / float(limit - 1)
        return [pts[int(round(i * step))] for i in range(limit)]

    @staticmethod
    def _autofit_columns(ws, letters="ABCDEFGH", min_w=8, max_w=50, max_row=None):
        """按内容估算列宽; 跳过合并区(合并区文本横跨多列, 按它算会把列撑爆)。

        max_row: 只看正文表的前 N 行 —— 图表数据写在隐藏列里会把 ws.max_row 撑大,
        不设上限时 `ws[letter]` 会为空行**创建单元格**, 白白拖慢保存(代码审查 2026-11-04)。
        """
        from openpyxl.utils import column_index_from_string
        merged_tl = {(r.min_row, r.min_col) for r in ws.merged_cells.ranges}
        limit = max_row or ws.max_row
        for letter in letters:
            best = 0
            ci = column_index_from_string(letter)
            for (cell,) in ws.iter_rows(min_col=ci, max_col=ci, max_row=limit):
                if (cell.row, cell.column) in merged_tl or cell.value is None:
                    continue
                text = str(cell.value)
                width = sum(2 if ord(ch) > 127 else 1 for ch in text)
                best = max(best, width)
            ws.column_dimensions[letter].width = max(min_w, min(max_w, best + 3))

    @staticmethod
    def _add_battery_chart(ws, samples, milestones=()):
        """在右侧(J 列起)画「电量-时间」曲线, 并标注目标电量点(时间 + 电量)。

        ★ 数据放隐藏列 M/N/P/Q(时间分钟 / 电量), 图表锚在 J4 —— 不占用正文表格区域。
        ★ 用 ScatterChart 而不是 LineChart: 采样间隔可能不均(补采/漏采), 折线图会把
          时间轴当等距类别, 曲线形状会失真。
        ★ 目标点(每 N% 里程碑)单独一条系列 + 数据标签(X 值 / Y 值), 让看图的人
          直接读到"几分钟时到了多少电", 不必回头翻表。
        """
        from openpyxl.chart import Reference, ScatterChart, Series
        from openpyxl.chart.label import DataLabelList
        from openpyxl.chart.marker import Marker
        from openpyxl.chart.shapes import GraphicalProperties
        from openpyxl.chart.text import RichText
        from openpyxl.drawing.line import LineProperties
        from openpyxl.drawing.text import (CharacterProperties, Paragraph,
                                           ParagraphProperties)

        pts = [(s.t / 60.0, s.battery) for s in samples
               if getattr(s, "battery", -1) >= 0]
        if len(pts) < 2:
            return
        pts = ExcelReport._downsample_for_chart(pts)   # 长测抽稀, 首末点保留
        start = 4

        def write_col(col, letter, values, fmt=None):
            """写一列数值到隐藏列, 返回 Reference。fmt 决定**数据标签**的显示格式。"""
            for i, v in enumerate(values, start):
                cell = ws.cell(row=i, column=col, value=v)
                if fmt:
                    cell.number_format = fmt
            ws.column_dimensions[letter].hidden = True
            return Reference(ws, min_col=col, min_row=start,
                             max_row=start + len(values) - 1)

        # M/N = 全量采样(时间分钟 / 电量); 目标点用 P/R 与 T/V 两组(见下)
        # ★ 数字格式给数据标签用: 时间显示 "12.5分", 电量显示 "85%"
        xs = write_col(13, "M", [round(m, 2) for m, _b in pts], '0.0"分"')
        ys = write_col(14, "N", [b for _m, b in pts], '0"%"')

        # ── 主线: 电量随时间 ──
        series = Series(ys, xs, title="电量")
        series.marker = Marker(symbol="circle", size=5)
        series.marker.graphicalProperties = GraphicalProperties(solidFill="FFFFFF")
        series.marker.graphicalProperties.line = LineProperties(solidFill="2563EB")
        series.graphicalProperties.line = LineProperties(solidFill="2563EB")
        series.graphicalProperties.line.width = 22000      # EMU ≈ 1.7pt, 线更清晰
        series.smooth = False

        chart = ScatterChart()
        # ★ 单位放坐标轴标题(用户 2026-11-04: 应该标在 X/Y 轴那里) ——
        #   必须先关掉标题的 overlay, 否则 openpyxl 生成的轴标题会压在刻度文字上
        #   (导出图表图片实测过)。
        chart.title = "电量变化曲线（◆ 目标电量点）"
        chart.style = 2
        chart.x_axis.title = "时间(分钟)"
        chart.y_axis.title = "电量(%)"
        chart.x_axis.title.overlay = False
        chart.y_axis.title.overlay = False
        chart.x_axis.delete = False          # 不隐藏坐标轴(默认会藏)
        chart.y_axis.delete = False
        chart.x_axis.majorUnit = 10
        chart.y_axis.majorUnit = 10
        # ★ 去掉网格线(用户 2026-11-04: 方格影响看数据)
        chart.x_axis.majorGridlines = None
        chart.y_axis.majorGridlines = None
        chart.y_axis.scaling.min = 0
        chart.y_axis.scaling.max = 100
        chart.legend = None                  # 单条主线, 图例占地方
        # ★ 数据放在**隐藏列**里; Excel 默认不绘制隐藏单元格(visible_cells_only/
        #   plotVisOnly=True) → 曲线图整片空白(2026-11-04 实测)。必须显式关掉。
        chart.visible_cells_only = False
        chart.height = 12
        chart.width = 22
        chart.series.append(series)

        # ── 目标点: 菱形 + 数据标签(X 值=分钟, Y 值=电量) ──
        # ★ 标签防遮挡(用户 2026-11-04 多轮反馈): 曲线整体下降, 标签**统一放点下方**
        #   最干净 —— 放在上方/左右都会压到相邻的曲线与点; 再加引导线 + 7pt 小字号。
        ms = list(milestones)
        if ms:
            xs_m = write_col(16, "P", [round(m["t"] / 60.0, 2) for m in ms], '0.0"分"')
            ys_m = write_col(18, "R", [m["battery"] for m in ms], '0"%"')
            mser = Series(ys_m, xs_m, title="目标电量点")
            mser.marker = Marker(symbol="diamond", size=7)
            mser.marker.graphicalProperties = GraphicalProperties(solidFill="F59E0B")
            mser.marker.graphicalProperties.line = LineProperties(solidFill="B45309")
            mser.graphicalProperties.line = LineProperties(noFill=True)
            mser.smooth = False
            labels = DataLabelList()
            labels.showVal = True        # Y: 电量 %
            labels.showCatName = True    # 散点图里 catName = X 值(分钟)
            labels.showSerName = False
            labels.showLegendKey = False
            labels.separator = "\n"      # 两行: 时间 / 电量, 横向更窄
            labels.showLeaderLines = True
            labels.position = "b"
            labels.txPr = RichText(p=[Paragraph(
                pPr=ParagraphProperties(defRPr=CharacterProperties(sz=700)),
                endParaRPr=CharacterProperties(sz=700))])
            mser.dLbls = labels
            chart.series.append(mser)

        ws.add_chart(chart, "J4")

    def save(self):
        ws = self.summary_ws
        ncols = 8

        # ── 标题 ──
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncols)
        ws.cell(row=1, column=1, value="自动化测试报告").font = self.TITLE_FONT
        ws.cell(row=1, column=1).alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[1].height = 40

        # ── 生成时间 ──
        ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=ncols)
        ws.cell(row=2, column=1,
                value=f"生成时间: {datetime.now().strftime(_tf.DATETIME)}").font = Font(
            name="Microsoft YaHei", size=9, color="808080")
        ws.cell(row=2, column=1).alignment = Alignment(horizontal="center")

        # ── 设备信息 ──
        info = self._device_info
        ws.merge_cells(start_row=3, start_column=1, end_row=3, end_column=ncols)
        ws.cell(row=3, column=1,
                value=(f"设备SN: {info['sn']}     App版本: {info['app_version']}     "
                       f"固件版本: {info['固件版本']}     基站版本: {info['基站版本']}")).font = Font(
            name="Microsoft YaHei", size=10, color="1F4E79")
        ws.cell(row=3, column=1).alignment = Alignment(horizontal="center")

        # ── 汇总表头 ──
        headers = ["序号", "用例名称", "优先级", "结果", "步骤数", "通过", "失败", "结束时间"]
        ws.column_dimensions["A"].width = 6
        ws.column_dimensions["B"].width = 24
        for c in "CDEFG":
            ws.column_dimensions[c].width = 10
        ws.column_dimensions["H"].width = 18       # 结束时间(带日期)
        for ci, h in enumerate(headers, 1):
            self._style_header(ws.cell(row=5, column=ci, value=h))
        ws.row_dimensions[5].height = 26

        # ── 用例数据 ──
        r, seq = 6, 1
        for case_name, case in self._cases.items():
            total, passed, failed = case["total"], case["passed"], case["failed"]
            # ★ 带日期: 长跑(比如连续几十小时、跨零点)时只写 HH:MM:SS 会分不清是哪天
            end_txt = (case["ended"].strftime(_tf.REPORT_TIME)
                       if case.get("ended") else "")
            for ci, val in enumerate([seq, case_name,
                                      case.get("priority") or "", "",
                                      total, passed, failed, end_txt], 1):
                cell = ws.cell(row=r, column=ci, value=val)
                cell.font = self.NORMAL_FONT
                cell.alignment = Alignment(horizontal="center", vertical="center")
                cell.border = self.BORDER
            # ★ 用例名称做成**内部超链接**, 点一下直接跳到那一次执行的详情页(用户要求 2026-10-12)。
            #   用工作表**实际标题**(已做过非法字符替换与撞名去重), 不是原始用例名 ——
            #   否则重名/含非法字符时链接会指错地方甚至报错。名字里的单引号要写成两个。
            nc = ws.cell(row=r, column=2)
            sheet_title = str(case["sheet"].title)
            # ★ 必须用 Hyperlink(location=...): 直接赋 `"#'表'!A1"` 这种字符串, openpyxl 会
            #   当成**外部**链接建一个 rId 关系, location 为空 → Excel 里点不动(实测)。
            nc.hyperlink = Hyperlink(ref=nc.coordinate,
                                     location="'%s'!A1" % sheet_title.replace("'", "''"))
            nc.font = self.LINK_FONT
            # 结果
            rc = ws.cell(row=r, column=4)
            if failed == 0:
                rc.value = "PASS"
                rc.font = self.PASS_FONT
                rc.fill = self.PASS_FILL
            else:
                rc.value = "FAIL"
                rc.font = self.FAIL_FONT
                rc.fill = self.FAIL_FILL
            seq += 1
            r += 1

        # ── 全局汇总 ──
        ws.merge_cells(start_row=r + 1, start_column=1, end_row=r + 1, end_column=ncols)
        all_t = sum(c["total"] for c in self._cases.values())
        all_p = sum(c["passed"] for c in self._cases.values())
        all_f = sum(c["failed"] for c in self._cases.values())
        ws.cell(row=r + 1, column=1,
                value=(f"全部用例: {len(self._cases)} 个   步骤: {all_t}   通过: {all_p}   "
                       f"失败: {all_f}   通过率: {all_p / max(all_t, 1) * 100:.1f}%")).font = Font(
            name="Microsoft YaHei", size=11, bold=True)
        ws.cell(row=r + 1, column=1).alignment = Alignment(horizontal="center")
        ws.cell(row=r + 1, column=1).border = self.BORDER
        ws.row_dimensions[r + 1].height = 28

        # ── 边框补全(汇总表头到数据区) ──
        self._border_range(ws, 5, 1, 5, ncols)

        # ── 详情 sheet 末尾汇总 ──
        for case_name, case in self._cases.items():
            ws2 = case["sheet"]
            cr = case["row"] + 1
            ws2.cell(row=cr, column=1,
                     value=(f"通过: {case['passed']}   失败: {case['failed']}   "
                            f"通过率: {case['passed'] / max(case['total'], 1) * 100:.1f}%")).font = Font(
                name="Microsoft YaHei", size=10, bold=True)

        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.wb.save(self.path)
