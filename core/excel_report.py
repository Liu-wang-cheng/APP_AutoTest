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
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

from core.driver import BASE_DIR


class ExcelReport:
    TITLE_FONT = Font(name="Microsoft YaHei", size=16, bold=True, color="1F4E79")
    HEADER_FONT = Font(name="Microsoft YaHei", size=11, bold=True, color="FFFFFF")
    HEADER_FILL = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    NORMAL_FONT = Font(name="Microsoft YaHei", size=10)
    PASS_FONT = Font(name="Microsoft YaHei", size=10, bold=True, color="006100")
    PASS_FILL = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
    FAIL_FONT = Font(name="Microsoft YaHei", size=10, bold=True, color="9C0006")
    FAIL_FILL = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
    BORDER = Border(*[Side(style="thin", color="B0B0B0")] * 4)

    def __init__(self, path=None):
        # 默认按时间戳命名,历史报告不互相覆盖(不建子目录)
        if path is None:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            reports_dir = os.path.join(BASE_DIR, "reports")
            os.makedirs(reports_dir, exist_ok=True)
            path = os.path.join(reports_dir, f"test_report_{ts}.xlsx")
        self.path = path
        self.wb = Workbook()
        self.summary_ws = self.wb.active
        self.summary_ws.title = "汇总"
        self._cases = {}
        # 设备信息(会话开始时由 conftest 填入真实值)
        self._device_info = {"sn": "待获取", "app_version": "待获取",
                             "固件版本": "待获取", "基站版本": "待获取"}

    def set_device_info(self, sn="", app_version=""):
        """填入真实设备信息,失败时保留占位符"""
        if sn:
            self._device_info["sn"] = sn
        if app_version:
            self._device_info["app_version"] = app_version

    def add_result(self, case_name, passed, error="", screenshot=""):
        if case_name not in self._cases:
            self._cases[case_name] = self._create_case_sheet(case_name)

        case = self._cases[case_name]
        ws = case["sheet"]
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

        # 嵌入截图
        ws.row_dimensions[r].height = 30
        if screenshot and os.path.exists(screenshot):
            try:
                from PIL import Image as PImage
                img = XLImage(screenshot)
                pi = PImage.open(screenshot)
                ratio = min(120 / pi.width, 210 / pi.height)
                img.width, img.height = int(pi.width * ratio), int(pi.height * ratio)
                ws.row_dimensions[r].height = max(30, img.height + 4)
                ws.add_image(img, f"D{r}")
            except Exception:
                pass

        case["row"] += 1

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

    def _create_case_sheet(self, case_name):
        # ★ openpyxl 的 sheet 名不允许 [ ] : * ? / \ 这几个字符(实测: 传「温度: 设置」
        #   直接抛 ValueError: Invalid character : found in sheet title)。原实现只替换了
        #   / 和 \, 于是组名带「:」「[」「?」时 create_sheet 抛错 → 冒到 RunWorker.run
        #   的兜底 except → **整轮**以"执行异常"中断, 设备侧已跑的部分白跑。
        #   组名(module)是 GUI 里可自由编辑的文本, 很容易踩到。
        safe_name = re.sub(r'[\\*?:/\[\]]', "_", str(case_name))[:31] or "用例"
        ws = self.wb.create_sheet(title=safe_name)
        for c, w in zip("ABCD", [28, 8, 28, 20]):
            ws.column_dimensions[c].width = w

        for ci, h in enumerate(["步骤", "结果", "错误信息", "截图"], 1):
            self._style_header(ws.cell(row=1, column=ci, value=h))
        ws.row_dimensions[1].height = 26
        return {"sheet": ws, "row": 2, "total": 0, "passed": 0, "failed": 0, "_last_desc": ""}

    def _border_range(self, ws, r1, c1, r2, c2):
        for r in range(r1, r2 + 1):
            for c in range(c1, c2 + 1):
                ws.cell(row=r, column=c).border = self.BORDER

    def save(self):
        ws = self.summary_ws
        ncols = 7

        # ── 标题 ──
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncols)
        ws.cell(row=1, column=1, value="自动化测试报告").font = self.TITLE_FONT
        ws.cell(row=1, column=1).alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[1].height = 40

        # ── 生成时间 ──
        ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=ncols)
        ws.cell(row=2, column=1,
                value=f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}").font = Font(
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
        headers = ["序号", "用例名称", "优先级", "结果", "步骤数", "通过", "失败"]
        ws.column_dimensions["A"].width = 6
        ws.column_dimensions["B"].width = 24
        for c in "CDEFG":
            ws.column_dimensions[c].width = 10
        for ci, h in enumerate(headers, 1):
            self._style_header(ws.cell(row=5, column=ci, value=h))
        ws.row_dimensions[5].height = 26

        # ── 用例数据 ──
        r, seq = 6, 1
        for case_name, case in self._cases.items():
            total, passed, failed = case["total"], case["passed"], case["failed"]
            for ci, val in enumerate([seq, case_name, "", "", total, passed, failed], 1):
                cell = ws.cell(row=r, column=ci, value=val)
                cell.font = self.NORMAL_FONT
                cell.alignment = Alignment(horizontal="center", vertical="center")
                cell.border = self.BORDER
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
