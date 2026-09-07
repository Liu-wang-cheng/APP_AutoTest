from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.drawing.image import Image as XLImage
from datetime import datetime
import os


class ExcelReport:

    HEADER_FONT = Font(name="Microsoft YaHei", size=11, bold=True, color="FFFFFF")
    HEADER_FILL = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    PASS_FONT = Font(name="Microsoft YaHei", size=10, color="006100")
    PASS_FILL = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
    FAIL_FONT = Font(name="Microsoft YaHei", size=10, color="9C0006")
    FAIL_FILL = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
    NORMAL_FONT = Font(name="Microsoft YaHei", size=10)
    TITLE_FONT = Font(name="Microsoft YaHei", size=16, bold=True, color="1F4E79")
    BORDER = Border(
        left=Side(style="thin", color="B0B0B0"),
        right=Side(style="thin", color="B0B0B0"),
        top=Side(style="thin", color="B0B0B0"),
        bottom=Side(style="thin", color="B0B0B0"),
    )

    def __init__(self, path="reports/test_report.xlsx"):
        self.path = path
        self.wb = Workbook()
        self.summary_ws = self.wb.active
        self.summary_ws.title = "汇总"
        self._cases = {}
        self._summary_row = 7

    def add_result(self, case_name, passed, error="", screenshot=""):
        if case_name not in self._cases:
            self._cases[case_name] = self._create_case_sheet(case_name)

        case = self._cases[case_name]
        ws = case["sheet"]
        case["total"] += 1
        r = case["row"]
        status = "PASS" if passed else "FAIL"

        # 步骤、结果、错误（A-C）
        for col_idx, val in enumerate([case.get("_last_desc", ""), status, error], 1):
            cell = ws.cell(row=r, column=col_idx)
            cell.value = val
            cell.font = self.NORMAL_FONT
            cell.alignment = Alignment(vertical="center", wrap_text=(col_idx != 2))
            cell.border = self.BORDER

        # 结果着色
        sc = ws.cell(row=r, column=2)
        if passed:
            sc.font = self.PASS_FONT; sc.fill = self.PASS_FILL
            case["passed"] += 1
        else:
            sc.font = self.FAIL_FONT; sc.fill = self.FAIL_FILL
            case["failed"] += 1
            for c in (1, 3):
                ws.cell(row=r, column=c).fill = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")

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
        if case_name not in self._cases:
            self._cases[case_name] = self._create_case_sheet(case_name)
        self._cases[case_name]["_last_desc"] = desc

    def _create_case_sheet(self, case_name):
        safe_name = case_name.replace("/", "_").replace("\\", "_")
        ws = self.wb.create_sheet(title=safe_name)
        for c, w in zip("ABCD", [28, 8, 28, 20]):
            ws.column_dimensions[c].width = w

        for ci, h in enumerate(["步骤", "结果", "错误信息", "截图"], 1):
            cell = ws.cell(row=1, column=ci, value=h)
            self._style_header(cell)
        ws.row_dimensions[1].height = 26
        return {"sheet": ws, "row": 2, "total": 0, "passed": 0, "failed": 0, "_last_desc": ""}

    def _style_header(self, cell):
        cell.font = self.HEADER_FONT; cell.fill = self.HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center"); cell.border = self.BORDER

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
        ws.cell(row=2, column=1, value=f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}").font = Font(
            name="Microsoft YaHei", size=9, color="808080")
        ws.cell(row=2, column=1).alignment = Alignment(horizontal="center")

        # ── 设备信息 ──
        ws.merge_cells(start_row=3, start_column=1, end_row=3, end_column=ncols)
        ws.cell(row=3, column=1,
                value="项目名称: 待获取     设备SN: 待获取     固件版本: 待获取     基站版本: 待获取").font = Font(
            name="Microsoft YaHei", size=10, color="1F4E79")
        ws.cell(row=3, column=1).alignment = Alignment(horizontal="center")

        # ── 汇总表头 ──
        headers = ["序号", "用例名称", "优先级", "结果", "步骤数", "通过", "失败"]
        ws.column_dimensions["A"].width = 6; ws.column_dimensions["B"].width = 24
        for c in "CDEFG": ws.column_dimensions[c].width = 10
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
                rc.value = "PASS"; rc.font = self.PASS_FONT; rc.fill = self.PASS_FILL
            else:
                rc.value = "FAIL"; rc.font = self.FAIL_FONT; rc.fill = self.FAIL_FILL
            seq += 1; r += 1

        # ── 全局汇总 ──
        ws.merge_cells(start_row=r + 1, start_column=1, end_row=r + 1, end_column=ncols)
        all_t = sum(c["total"] for c in self._cases.values())
        all_p = sum(c["passed"] for c in self._cases.values())
        all_f = sum(c["failed"] for c in self._cases.values())
        ws.cell(row=r + 1, column=1,
                value=f"全部用例: {len(self._cases)} 个  步骤: {all_t}  通过: {all_p}  失败: {all_f}  通过率: {all_p / max(all_t, 1) * 100:.1f}%").font = Font(
            name="Microsoft YaHei", size=11, bold=True)
        ws.cell(row=r + 1, column=1).alignment = Alignment(horizontal="center")
        ws.cell(row=r + 1, column=1).border = self.BORDER
        ws.row_dimensions[r + 1].height = 28

        # ── 边框补全（汇总表头到数据区） ──
        self._border_range(ws, 5, 1, 5, ncols)

        # ── 详情 sheet 末尾汇总 ──
        for case_name, case in self._cases.items():
            ws2 = case["sheet"]
            cr = case["row"] + 1
            ws2.cell(row=cr, column=1,
                     value=f"通过: {case['passed']}  失败: {case['failed']}  通过率: {case['passed'] / max(case['total'], 1) * 100:.1f}%").font = Font(
                name="Microsoft YaHei", size=10, bold=True)

        self.wb.save(self.path)
