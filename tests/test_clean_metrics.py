# -*- coding: utf-8 -*-
"""「抓本次清扫面积/时间」的守护(2026-10-10 加入)。

★ 为什么单独一套: 真机「打扫报告」页上**同时**有
      总面积 1,300 m², 总时间 29 小时 57 分钟, 总次数 47 次    ← 累计
      真空吸尘器 20m² / 拖布 18m² / 时间 30 分                 ← 本次
  而原来的 grab 是"关键字子串匹配 + 标签正上方找数字": `面积` 会命中「总面积」,
  文档顺序里累计在前就抓到累计值(合成版式实测 128 vs 25、36 vs 12), 数字看着正常;
  单位只认 ㎡/min(真机是 m²/分); 版式只认"数值在上、标签在下"(报告页是反的)。
  这里的夹具就是**真机页面的层级快照**, 改动这块务必让它继续通过。
"""
import pytest

from core.actions.data_ops import (_grab_plan, extract_metric, metric_kind_of)

#: 真机「打扫报告」页的层级快照(2026-10-10, MuMu + SmartThings, 只保留非空文本)
REAL_REPORT = [
    ('扫地机器人 0050', (0, 72, 1080, 1920)),
    ('向上浏览', (48, 96, 168, 264)),
    ('打扫报告', (168, 96, 1032, 264)),
    ('总面积 1,300 m², 总时间 29 小时 57 分钟, 总次数 47 次', (30, 72, 1050, 222)),
    ('今天 19:44 清洁所有房间', (72, 303, 1008, 444)),
    ('折叠', (900, 312, 996, 408)),
    ('旋转 90°', (912, 1425, 1008, 1521)),
    ('已清洁 2，共3', (84, 1617, 996, 1686)),
    ('真空吸尘器 20m²,拖布 18m², 清洁时间 30 分', (72, 1728, 1008, 1920)),
    ('真空吸尘器', (114, 1770, 390, 1836)),
    ('20m²', (183, 1830, 321, 1911)),
    ('拖布', (402, 1770, 678, 1836)),
    ('18m²', (471, 1830, 609, 1911)),
    ('时间', (690, 1770, 966, 1836)),
    ('30 分', (762, 1830, 894, 1911)),
    ('20:36', (12, 11, 131, 60)),
]





def _nodes(*rows):
    """按 (文本, bounds) 造节点; bounds 用 (x1,y1,x2,y2)"""
    return list(rows)


# ── 真机快照 ──

def test_real_report_area_needs_keyword_because_two_candidates():
    """真机上本次面积有两个(真空吸尘器 20 / 拖布 18) —— 必须报错让人指明, 不能猜"""
    with pytest.raises(ValueError) as ei:
        extract_metric(REAL_REPORT, "area")
    msg = str(ei.value)
    assert "20" in msg and "18" in msg and "多个" in msg, msg


def test_real_report_time_is_this_run_not_total():
    """时间是本次的 30 分, **不是**累计的 29 小时 57 分钟"""
    assert extract_metric(REAL_REPORT, "time") == "30"


@pytest.mark.parametrize("want,expect", [("真空吸尘器", "20"), ("拖布", "18")])
def test_real_report_area_with_keyword(want, expect):
    assert extract_metric(REAL_REPORT, "area", want) == expect


# ── 累计 vs 本次(合成版式: 数值在上、标签在下, 累计在前) ──

TOTAL_FIRST = _nodes(
    ("128 ㎡", (100, 100, 260, 140)),          # 累计面板(文档顺序在前)
    ("累计清扫面积", (100, 150, 300, 180)),
    ("25 ㎡", (400, 100, 560, 140)),           # 本次
    ("清扫面积", (400, 150, 540, 180)),
)
TOTAL_TIME_FIRST = _nodes(
    ("36 min", (100, 100, 260, 140)),
    ("累计清扫时间", (100, 150, 300, 180)),
    ("12 min", (400, 100, 560, 140)),
    ("清扫时间", (400, 150, 540, 180)),
)
ONLY_TOTAL = _nodes(
    ("128 ㎡", (100, 100, 260, 140)),
    ("累计清扫面积", (100, 150, 300, 180)),
)


def test_total_is_excluded_even_when_it_comes_first():
    """★ 老实现会抓到 128(累计); 现在必须拿到本次的 25"""
    assert extract_metric(TOTAL_FIRST, "area") == "25"
    assert extract_metric(TOTAL_TIME_FIRST, "time") == "12"


def test_only_total_available_raises_instead_of_guessing():
    """★ 页面上只有累计值时**报错**, 不拿累计冒充本次"""
    with pytest.raises(ValueError, match="没找到"):
        extract_metric(ONLY_TOTAL, "area")


# ── 单位与换算 ──

@pytest.mark.parametrize("text,unit_kind,expect", [
    ("20m²", "area", "20"),
    ("20 ㎡", "area", "20"),
    ("1,300m²", "area", "1300"),
    ("30 分", "time", "30"),
    ("30min", "time", "30"),
    ("29 小时 57 分钟", "time", "1797"),      # 复合时长换算(29*60+57)
    ("1 小时 5 分钟", "time", "65"),
])
def test_units_and_conversion(text, unit_kind, expect):
    nodes = _nodes((text, (100, 100, 300, 140)), ("清扫指标", (100, 150, 300, 180)))
    assert extract_metric(nodes, unit_kind) == expect


def test_percent_and_clock_are_not_metrics():
    """99% / 19:44 不是面积也不是时间(状态卡与记录条目上方就有它们)"""
    nodes = _nodes(("99%", (100, 100, 200, 140)), ("电量", (100, 150, 260, 180)),
                   ("19:44", (400, 100, 520, 140)), ("时间", (400, 150, 540, 180)))
    with pytest.raises(ValueError, match="没找到"):
        extract_metric(nodes, "time")


# ── 字段摊开: 新老写法都认 ──

def test_grab_plan_accepts_all_forms():
    assert _grab_plan("面积,时间", None) == [
        ("面积", "area", "", None), ("时间", "time", "", None)]
    assert _grab_plan(True, None) == [                # GUI 保存后的形态
        ("面积", "area", "", None), ("时间", "time", "", None)]
    assert _grab_plan("真空吸尘器", "时间") == [        # 新界面: 两个框各填各的
        ("面积", "area", "真空吸尘器", None), ("时间", "time", "时间", None)]
    assert _grab_plan("预约时间", None) == [("预约时间", None, "", "预约时间")]


@pytest.mark.parametrize("kw,kind", [
    ("面积", "area"), ("清扫面积", "area"), ("时间", "time"),
    ("清扫时间", "time"), ("用时", "time"), ("时长", "time"),
    ("预约时间", None), ("预约日期", None), ("电量", None),
])
def test_metric_kind_of(kw, kind):
    assert metric_kind_of(kw) == kind
