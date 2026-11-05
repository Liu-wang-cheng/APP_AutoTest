# -*- coding: utf-8 -*-
"""Excel 报告: 懒建 sheet / 两段式步骤名 / 汇总统计 / 截图嵌入。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.excel_report import ExcelReport
from openpyxl import load_workbook


def test_two_phase_desc_and_stats(tmp_path):
    p = tmp_path / "r.xlsx"
    r = ExcelReport(path=p)
    r.set_step_desc("全局清扫", "点击开始清扫")
    r.add_result("全局清扫", True)
    r.set_step_desc("全局清扫", "检查清扫中")
    r.add_result("全局清扫", False, error="超时")
    r.save()
    assert p.exists()

    from openpyxl import load_workbook
    wb = load_workbook(p)
    ws = wb["全局清扫"]
    assert ws.cell(row=2, column=1).value == "点击开始清扫"
    assert ws.cell(row=2, column=2).value == "PASS"
    assert ws.cell(row=3, column=1).value == "检查清扫中"
    assert ws.cell(row=3, column=2).value == "FAIL"
    assert ws.cell(row=3, column=3).value == "超时"


def test_battery_report_page_summary_and_details(tmp_path):
    """「电量监测」页: 上汇总 + 下详细; 幂等(只建一页); 空样本给原因。

    ★ 2026-11-04: 里程碑不占详细行(改到汇总的「每 N% 分段」表); 表头标注 (分:秒);
      右侧有电量-时间曲线图。
    """
    from core.battery_monitor import BatterySample

    p = tmp_path / "r.xlsx"
    r = ExcelReport(path=p)
    r.add_result("用例A", True)
    samples = [BatterySample(t=t, battery=b, label="用例A(第1次)")
               for t, b in [(0, 80), (30, 80), (60, 79), (90, 78), (120, 75)]]
    r.add_battery_report(samples, step_pct=5, interval=30)
    r.add_battery_report(samples, step_pct=5, interval=30)   # 幂等
    r.save()

    wb = load_workbook(p)
    assert wb.sheetnames.count("电量监测") == 1
    ws = wb["电量监测"]
    texts = [str(c.value) for row in ws.iter_rows() for c in row if c.value]
    assert "一、汇总" in texts and "二、详细数据" in texts
    assert "总运行时长(分:秒)" in texts
    assert any("02:00" == t for t in texts), texts          # 总时长 120s
    assert any(t == "5%" for t in texts), texts             # 总耗电 5%
    assert any("每 5% 段平均用时(分:秒)" in t for t in texts), texts
    # 里程碑进汇总表, 不再出现在详细里
    assert any("每 5% 分段用时" in t for t in texts), texts
    assert not any(t == "5% 里程碑" for t in texts), texts
    # 详细区只剩每次 1% 的实际变化(79/78/75 各一行)
    assert not any(t == "1% 原始" for t in texts), "类型列已移除"
    assert texts.count("79%") >= 1 and texts.count("78%") >= 1 and texts.count("75%") >= 1
    # 表头带单位说明
    assert "距上次变化(分:秒)" in texts and "累计用时(分:秒)" in texts
    # 右侧曲线图存在, 数据列(M/N)隐藏
    assert ws._charts, "电量-时间曲线图没生成"
    assert ws.column_dimensions["M"].hidden and ws.column_dimensions["N"].hidden
    # ★ 关键: 数据在隐藏列里, 图表必须允许绘制隐藏单元格 —— 否则 Excel 里一片空白
    #   (2026-11-04 实测踩过)
    assert ws._charts[0].visible_cells_only is False


def test_battery_report_cells_have_borders(tmp_path):
    """★ 单元格边框: 合并区也要整行补边框(用户 2026-11-04)。"""
    from core.battery_monitor import BatterySample

    p = tmp_path / "r.xlsx"
    r = ExcelReport(path=p)
    samples = [BatterySample(t=t, battery=b, label="A")
               for t, b in [(0, 80), (60, 79)]]
    r.add_battery_report(samples, step_pct=5)
    r.save()
    ws = load_workbook(p)["电量监测"]
    # 合并区在保存后只有**外缘**保留边框: 左列看 left、右列看 right, 上下都要有
    for coord, side in (("A1", "left"), ("H1", "right"), ("A2", "left"),
                        ("H2", "right"), ("A4", "left"), ("H4", "right")):
        b = ws[coord].border
        assert getattr(b, side).style is not None, f"{coord} 缺少 {side} 边框"
        assert b.top.style is not None and b.bottom.style is not None, \
            f"{coord} 缺少上下边框"


def test_battery_report_page_empty_samples(tmp_path):
    from core.battery_monitor import BatterySample

    p = tmp_path / "r.xlsx"
    r = ExcelReport(path=p)
    r.add_battery_report([BatterySample(t=0, battery=-1)], step_pct=5)
    r.save()
    ws = load_workbook(p)["电量监测"]
    texts = [str(c.value) for row in ws.iter_rows() for c in row if c.value]
    assert any("未采集到有效电量数据" in t for t in texts), texts


def test_battery_report_columns_autofit(tmp_path):
    """★ 列宽按内容自适应(用户 2026-11-04: 避免显示不全)。"""
    from core.battery_monitor import BatterySample

    p = tmp_path / "r.xlsx"
    r = ExcelReport(path=p)
    samples = [BatterySample(t=t, battery=b, label="一个比较长的用例名(第1次)")
               for t, b in [(0, 80), (60, 79)]]
    r.add_battery_report(samples, step_pct=5)
    r.save()
    ws = load_workbook(p)["电量监测"]
    # 标签列(A) 与用例列(C) 都要被撑开; 过窄会导致"每 5% 段最长用时(分:秒)"被截断
    assert ws.column_dimensions["A"].width >= 20, ws.column_dimensions["A"].width
    assert ws.column_dimensions["C"].width >= 20, ws.column_dimensions["C"].width


def test_battery_report_not_confused_by_case_named_battery(tmp_path):
    """★ 代码审查 2026-11-04: 用例名恰好叫「电量监测」时, 旧实现按 sheet 名判重会
    直接把用例页当电量页返回 → 报告里没有电量数据。幂等应看对象标记而非名字。"""
    from core.battery_monitor import BatterySample

    r = ExcelReport(path=tmp_path / "r.xlsx")
    r.add_result("电量监测", True)               # 先建了一张同名的用例页
    samples = [BatterySample(t=t, battery=b) for t, b in [(0, 80), (60, 79)]]
    ws1 = r.add_battery_report(samples, step_pct=5)
    ws2 = r.add_battery_report(samples, step_pct=5)   # 幂等: 再调不建第二页
    assert ws1 is ws2
    assert "电量监测" in r.wb.sheetnames                 # 用例页还在
    assert ws1.title != "电量监测", "电量页自动改名, 不覆盖用例页"
    titles = [c.value for row in ws1.iter_rows() for c in row if c.value]
    assert any(v == "电量监测" for v in titles)          # 内容确实是电量页
    assert len([n for n in r.wb.sheetnames if n.startswith("电量监测")]) == 2


def test_battery_chart_downsamples_long_runs():
    """★ 代码审查 2026-11-04: 长测(几秒一采、几小时)不把上万行写进隐藏列。"""
    pts = [(i * 0.01, 90) for i in range(5000)]
    out = ExcelReport._downsample_for_chart(pts)
    assert len(out) == ExcelReport.CHART_MAX_POINTS
    assert out[0] == pts[0] and out[-1] == pts[-1]      # 首末点必须保留
    short = pts[:100]
    assert ExcelReport._downsample_for_chart(short) == short


def test_summary_sheet_has_pass_rate(tmp_path):
    p = tmp_path / "r.xlsx"
    r = ExcelReport(path=p)
    r.add_result("用例A", True)
    r.add_result("用例B", False)
    r.save()
    from openpyxl import load_workbook
    wb = load_workbook(p)
    ws = wb["汇总"]
    texts = [str(c.value) for row in ws.iter_rows() for c in row if c.value]
    assert any("通过率" in t for t in texts)
    assert any("设备SN" in t for t in texts)


def test_screenshot_embedded(tmp_path):
    from PIL import Image
    img = tmp_path / "shot.png"
    Image.new("RGB", (40, 30), "red").save(img)
    p = tmp_path / "r2.xlsx"
    r = ExcelReport(path=p)
    r.set_step_desc("用例A", "截图步骤")
    r.add_result("用例A", True, screenshot=str(img))
    r.save()
    from openpyxl import load_workbook
    wb = load_workbook(p)
    assert len(wb["用例A"]._images) == 1


def test_screenshot_fills_cell(tmp_path):
    """★ 2026-11-05 用户反馈: 截图只占单元格左上角一小块。
    现在**不改表格布局**(D 列仍 20 字符、行高仍 ≈214 磅), 只把图片等比放大到格子内,
    上下留白一致。用 TwoCellAnchor 跨到下一行: 打印时图片跟着表格一起缩放,
    上下留白不会一宽一窄。竖屏 1080x2400 截图 ≈117x261px(四周 12px)。"""
    from PIL import Image
    img = tmp_path / "shot_tall.png"
    Image.new("RGB", (540, 1200), "red").save(img)      # 竖屏手机比例
    p = tmp_path / "r2b.xlsx"
    r = ExcelReport(path=p)
    r.set_step_desc("用例A", "截图步骤")
    r.add_result("用例A", True, screenshot=str(img))
    r.save()
    from openpyxl import load_workbook
    ws = load_workbook(p)["用例A"]
    shot = ws._images[0]
    a = shot.anchor
    assert type(a).__name__ == "TwoCellAnchor"
    # 行高(磅)= (261+24)/1.333 ≈ 213.8 —— 与旧版一致, 表格没有变高
    assert 205 <= ws.row_dimensions[2].height <= 220, ws.row_dimensions[2].height
    # D 列宽保持旧值 20 字符, 没有被改宽
    assert ws.column_dimensions["D"].width == 20
    # 左上角在 D2 内、距上边 12px; 右下角落在下一行行首往上 12px(上下留白一致)
    assert a._from.col == 3 and a._from.row == 1
    assert round(a._from.rowOff / 9525) == 12
    assert a.to.row == 2 and round(a.to.rowOff / 9525) == -12
    box_w = (a.to.colOff - a._from.colOff) / 9525
    assert 116 <= box_w <= 118, box_w
    # 图片盒子完全在本行内: 上下各 12px, 盒子高 ≈261px
    row_px = ws.row_dimensions[2].height * 4 / 3
    box_h = row_px - (a._from.rowOff - a.to.rowOff) / 9525
    assert 260 <= box_h <= 262, box_h


def test_screenshots_do_not_overlap_adjacent_rows(tmp_path):
    """★ 用户 2026-11-05: 确认每个步骤的截图不会上下互相遮挡(竖屏/横屏/方图混合)。"""
    from PIL import Image
    from openpyxl import load_workbook
    sizes = [(540, 1200), (1200, 540), (600, 600), (540, 1200)]
    rep = tmp_path / "rows.xlsx"
    r = ExcelReport(path=rep)
    for i, size in enumerate(sizes, 1):
        p = tmp_path / f"shot{i}.png"
        Image.new("RGB", size, "white").save(p)
        r.set_step_desc("用例A", f"步骤{i}")
        r.add_result("用例A", True, screenshot=str(p))
    r.save()
    ws = load_workbook(rep)["用例A"]
    assert len(ws._images) == 4
    for im in ws._images:
        a = im.anchor
        row = a._from.row + 1
        # 跨行锚点: 右下角正好落在下一行行首上方(负 rowOff), 图片只属于本行
        assert a.to.row == a._from.row + 1, (a._from.row, a.to.row)
        assert a._from.rowOff >= 0 and a.to.rowOff <= 0
        cell_px = ws.row_dimensions[row].height * 4 / 3
        box_h = (cell_px - (a._from.rowOff - a.to.rowOff) / 9525)
        assert box_h <= cell_px, (
            f"第{row}行图片越界: 图片高 {box_h:.1f} > 行高 {cell_px:.1f}")


def test_device_info_filled(tmp_path):
    p = tmp_path / "r3.xlsx"
    r = ExcelReport(path=p)
    r.set_device_info(sn="SN12345", app_version="1.8.45.24")
    r.add_result("用例A", True)
    r.save()
    from openpyxl import load_workbook
    wb = load_workbook(p)
    texts = [str(c.value) for row in wb["汇总"].iter_rows() for c in row if c.value]
    assert any("SN12345" in t for t in texts)


def test_case_name_sanitized(tmp_path):
    """用例名含 / \\ 时必须清洗,否则 openpyxl 建 sheet 报错"""
    p = tmp_path / "r4.xlsx"
    r = ExcelReport(path=p)
    r.add_result("清扫/记录", True)
    r.save()
    from openpyxl import load_workbook
    wb = load_workbook(p)
    assert "清扫_记录" in wb.sheetnames


def test_sheet_name_sanitizes_all_openpyxl_forbidden_chars(tmp_path):
    """★ sheet 名要清掉 openpyxl 全部非法字符([ ] : * ? / \\), 不能让整轮挂掉。

    回归守护: 原实现只替换了 / 和 \\ —— 组名写「温度: 设置」时 create_sheet 直接抛
    `ValueError: Invalid character : found in sheet title`, 冒到 RunWorker.run 的
    兜底 except → **整轮**以"执行异常"中断, 设备侧已跑的部分白跑。
    组名(module)是 GUI 里可自由编辑的文本, 很容易踩到。
    """
    from openpyxl import load_workbook

    from core.excel_report import ExcelReport
    p = tmp_path / "r.xlsx"
    r = ExcelReport(path=p)
    names = ["温度: 设置", "模式[夜间]", "速度?快", "a*b", "a\\b", "a/b", "全局清扫"]
    for n in names:
        r.add_result(n, True)          # 关键: 这里不得抛异常
    r.save()
    wb = load_workbook(p)
    # 「汇总」是报告自带的汇总页; a\b 与 a/b 清洗后同名, openpyxl 会自己加后缀
    case_sheets = [s for s in wb.sheetnames if s != "汇总"]
    assert len(case_sheets) == len(names)
    for sn in wb.sheetnames:
        assert not (set(sn) & set('[]:*?/\\')), f"sheet 名仍含非法字符: {sn!r}"


def test_summary_case_name_links_to_its_detail_sheet(tmp_path):
    """★ 汇总页的用例名称要能**点进**那一次执行的详情页(用户要求 2026-10-12)。

    同一个用例跑多轮时, 汇总页每行是一个"第几次执行", 链接必须指向**那一行对应的**
    详情页(而不是笼统的用例名 —— 那样只会跳到第一轮)。
    """
    from openpyxl import load_workbook
    from core.excel_report import ExcelReport

    rep = ExcelReport(path=str(tmp_path / "r.xlsx"))
    rep.add_result("清扫-第1次", True, "", "")
    rep.add_result("清扫-第2次", False, "炸了", "")
    rep.save()

    wb = load_workbook(tmp_path / "r.xlsx")
    ws = wb["汇总"]
    # 汇总数据从第 6 行开始
    for row, sheet, result in ((6, "清扫-第1次", "PASS"), (7, "清扫-第2次", "FAIL")):
        cell = ws.cell(row=row, column=2)
        assert cell.value == sheet, cell.value
        assert cell.hyperlink is not None, f"第{row}行的用例名没有超链接"
        assert cell.hyperlink.location == f"'{sheet}'!A1", cell.hyperlink.location
        assert cell.hyperlink.target is None, "内部跳转不该有外部 target"
        assert cell.font.underline == "single" and cell.font.color.rgb.endswith("0563C1"), \
            "链接该是蓝色下划线"
        assert ws.cell(row=row, column=4).value == result
    # 两张详情页都在
    assert "清扫-第1次" in wb.sheetnames and "清扫-第2次" in wb.sheetnames


def test_summary_link_escapes_single_quote_in_sheet_name(tmp_path):
    """表名里有单引号时要按 Excel 规则写成两个, 否则链接会指错/报错"""
    from openpyxl import load_workbook
    from core.excel_report import ExcelReport

    rep = ExcelReport(path=str(tmp_path / "r.xlsx"))
    rep.add_result("张三's 用例", True, "", "")
    rep.save()
    wb = load_workbook(tmp_path / "r.xlsx")
    cell = wb["汇总"].cell(row=6, column=2)
    assert cell.hyperlink.location == "'张三''s 用例'!A1", cell.hyperlink.location


def test_summary_shows_case_end_time(tmp_path):
    """★ 汇总页要显示每个用例(每次执行)的**结束时间**(用户要求 2026-10-12)。

    时间取"该次执行最后一条结果写入的时刻" —— 报告是在每步跑完时写的, 秒级足够。
    """
    from openpyxl import load_workbook
    from core.excel_report import ExcelReport

    rep = ExcelReport(path=str(tmp_path / "r.xlsx"))
    rep.add_result("清扫-第1次", True, "", "")
    rep.add_result("清扫-第1次", True, "", "")
    rep.add_result("清扫-第2次", False, "炸了", "")
    rep.save()

    wb = load_workbook(tmp_path / "r.xlsx")
    ws = wb["汇总"]
    assert ws.cell(row=5, column=8).value == "结束时间", "表头缺「结束时间」"
    t1 = ws.cell(row=6, column=8).value
    t2 = ws.cell(row=7, column=8).value
    import re as _re
    assert _re.fullmatch(r"\d{2}-\d{2} \d{2}:\d{2}:\d{2}", str(t1)), t1
    assert _re.fullmatch(r"\d{2}-\d{2} \d{2}:\d{2}:\d{2}", str(t2)), t2
    # 超链接仍在(同一列的用例名)
    assert ws.cell(row=6, column=2).hyperlink is not None


def test_each_case_execution_has_its_own_end_time(tmp_path):
    """★ 每条用例(每次执行)的结束时间必须是**各自的**, 不能全变成一个值。

    用户 2026-10-12 特意提醒: "注意是记录每条用例的单独结束时间, 不要又变成同一个结束时间了"
    —— 报告里每个"第几次执行"是**独立的 sheet**(case_sheet_name 生成), 时间在建表时就分开
    存在各自的 case 记录里; 这条用"三次执行之间真的隔了时间"来钉住它, 而不是只看格式合法。
    """
    import re as _re
    import time as _t

    from openpyxl import load_workbook
    from core.excel_report import ExcelReport

    rep = ExcelReport(path=str(tmp_path / "r.xlsx"))
    rep.add_result("清扫-第1次", True, "", "")
    _t.sleep(1.05)                       # 让秒数真的跨过去
    rep.add_result("清扫-第2次", True, "", "")
    _t.sleep(1.05)
    rep.add_result("清扫-第3次", False, "炸了", "")
    rep.save()

    ws = load_workbook(tmp_path / "r.xlsx")["汇总"]
    times = [ws.cell(row=r, column=8).value for r in (6, 7, 8)]
    for t in times:
        assert _re.fullmatch(r"\d{2}-\d{2} \d{2}:\d{2}:\d{2}", str(t)), times
    assert len(set(times)) == 3, f"三次执行的时间不该相同: {times}"


def test_same_case_twice_shares_no_state(tmp_path):
    """同一个用例的多次执行**不共享状态**: 各自的步骤只进自己的详情页, 统计各自算"""
    from openpyxl import load_workbook
    from core.excel_report import ExcelReport

    rep = ExcelReport(path=str(tmp_path / "r.xlsx"))
    rep.set_step_desc("甲-第1次", "第一次的步骤")
    rep.add_result("甲-第1次", True, "", "")
    rep.set_step_desc("甲-第2次", "第二次的步骤")
    rep.add_result("甲-第2次", True, "", "")
    rep.add_result("甲-第2次", True, "", "")          # 第二次有两条
    rep.save()

    wb = load_workbook(tmp_path / "r.xlsx")
    first = [c.value for row in wb["甲-第1次"].iter_rows() for c in row if c.value]
    second = [c.value for row in wb["甲-第2次"].iter_rows() for c in row if c.value]
    assert "第一次的步骤" in first and "第二次的步骤" not in first, first
    assert "第二次的步骤" in second and "第一次的步骤" not in second, second
    assert first.count("PASS") == 1, first            # 步骤数各自独立
    assert second.count("PASS") == 2, second
    ws = wb["汇总"]                                    # 汇总页逐行对应一次执行
    assert (ws.cell(row=6, column=5).value, ws.cell(row=6, column=6).value) == (1, 1)
    assert (ws.cell(row=7, column=5).value, ws.cell(row=7, column=6).value) == (2, 2)


def test_case_priority_written_to_summary(tmp_path):
    """★ 汇总页"优先级"列写真实值(用户 2026-10-15: "这里设置的优先级也没有在
    测试报告的汇总页写出来") —— 界面/用例 YAML 里设置的 priority 此前恒为空。"""
    from core.excel_report import ExcelReport
    rep = ExcelReport(path=str(tmp_path / "p.xlsx"))
    rep.set_case_priority("冒烟", "P0")
    rep.add_result("冒烟", True, "", "")
    rep.add_result("别的用例", True, "", "")      # 没设优先级 → 留空
    rep.save()
    wb = load_workbook(tmp_path / "p.xlsx")
    ws = wb["汇总"]
    rows = {ws.cell(row=r, column=2).value: ws.cell(row=r, column=3).value
            for r in range(6, ws.max_row + 1) if ws.cell(row=r, column=2).value}
    assert rows.get("冒烟") == "P0", rows
    assert rows.get("别的用例") in ("", None), rows
