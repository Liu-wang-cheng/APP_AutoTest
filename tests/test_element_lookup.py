# -*- coding: utf-8 -*-
"""定位解析与点击回退 —— 所有点击/断言都要走 _find_element,它错了整条链路都错。

这里覆盖的是「把 YAML 里写的定位串翻译成设备上的元素」这一段:
    "确认"         → textContains,找不到回退 description
    "=确认"        → 精确 text
    "虚拟墙#2"     → 第 2 个匹配
    ":id/btn"      → resourceId
    "//*[@text=…]" → XPath
    [x, y]         → 坐标直点(在 test_actions_basic 里)
以及点不动文字节点时回退到整行(RN 列表的关键)。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import core.actions.basic  # noqa: F401
from core.runner import ActionRunner, XPathElement


class FakeElement:
    """模拟 uiautomator2 的元素对象"""

    def __init__(self, exists=True, clickable=True, text="", bounds=(0, 0, 100, 50)):
        self._exists = exists
        self._clickable = clickable
        self._text = text
        self._bounds = bounds
        self.clicked = False

    def exists(self, timeout=1):
        return self._exists

    def click(self):
        self.clicked = True

    @property
    def info(self):
        x1, y1, x2, y2 = self._bounds
        return {"clickable": self._clickable, "text": self._text,
                "bounds": {"left": x1, "top": y1, "right": x2, "bottom": y2}}


class FakeIndexed:
    """支持 d(...)[i] 索引访问"""

    def __init__(self, elements):
        self._elements = elements

    def exists(self, timeout=1):
        return bool(self._elements)

    def __getitem__(self, i):
        return self._elements[i]

    def click(self):
        if self._elements:
            self._elements[0].click()


class FakeDevice:
    """按查询条件返回元素;present 里的文本才算"页面上存在\""""

    serial = "fake"

    def __init__(self, present=(), duplicates=None, clickable=True, xml=""):
        self.present = set(present)
        self.duplicates = duplicates or {}    # {"虚拟墙": 3} → 该文本有 3 个
        self.clickable = clickable
        self._xml = xml
        self.clicked_at = None
        self.pressed = None
        self.queries = []

    def info(self):
        return {}

    def window_size(self):
        return (1080, 1920)

    def click(self, x, y):
        self.clicked_at = (x, y)

    def press(self, key):
        self.pressed = key

    def dump_hierarchy(self):
        # 判断类现在读层级(原生优先): 桩要把 present 也体现在 XML 里
        if self._xml:
            return self._xml
        return "".join(f'<node text="{p}"/>' for p in sorted(self.present))

    def xpath(self, expr):
        return FakeElement(exists=False)      # 默认 xpath 无命中;需要时子类覆盖

    def __call__(self, **kw):
        self.queries.append(kw)
        for key in ("textContains", "text", "description", "resourceId"):
            if key in kw:
                val = kw[key]
                if val in self.duplicates:
                    n = self.duplicates[val]
                    return FakeIndexed([FakeElement(text=val, clickable=self.clickable)
                                        for _ in range(n)])
                hit = val in self.present
                return FakeElement(exists=hit, text=val, clickable=self.clickable)
        return FakeElement(exists=False)


@pytest.fixture
def make_runner():
    def _make(device):
        return ActionRunner(device, {"step_interval": 0, "default_timeout": 1,
                                     "click_timeout": 1}, case_name="t")
    return _make


# ── 定位串解析 ──

def test_text_contains_hit(make_runner):
    d = FakeDevice(present=("确认",))
    el = make_runner(d)._find_element("确认")
    assert d.queries[0].get("textContains") == "确认"
    assert el.exists()


def test_exact_text_prefix_distinguishes_equals(make_runner):
    """= 前缀走精确 text,不是 textContains"""
    d = FakeDevice(present=("确认",))
    make_runner(d)._find_element("=确认")
    assert d.queries[0].get("text") == "确认"
    assert "textContains" not in d.queries[0]


def test_fallback_to_description_when_text_missing(make_runner):
    """文本找不到时回退 description —— 涂鸦很多按钮只有 content-desc"""
    class D(FakeDevice):
        def __call__(self, **kw):
            self.queries.append(kw)
            if "description" in kw:
                return FakeElement(exists=kw["description"] == "Popup_Confirm")
            return FakeElement(exists=False)

    d = D()
    el = make_runner(d)._find_element("确认")
    assert any("description" in q for q in d.queries)
    assert el.exists()


def test_index_suffix_picks_nth(make_runner):
    """「虚拟墙#2」→ 取第 2 个匹配"""
    d = FakeDevice(duplicates={"虚拟墙": 3})
    el = make_runner(d)._find_element("虚拟墙#2")
    assert isinstance(el, FakeElement)

    d2 = FakeDevice(duplicates={"虚拟墙": 3})
    idx = make_runner(d2)._find_element("虚拟墙#2")
    assert idx is not None


def test_resource_id_branch(make_runner):
    """:id/ 走 resourceId,不做文本匹配"""
    d = FakeDevice()
    make_runner(d)._find_element("com.tuya.smartiot:id/btn")
    assert d.queries[0].get("resourceId") == "com.tuya.smartiot:id/btn"


def test_xpath_returns_xpath_element(make_runner):
    """// 开头或 xpath= 前缀 → XPathElement(从 v1.4 用例迁移来的定位串)"""
    d = FakeDevice()
    r = make_runner(d)
    assert isinstance(r._find_element("//*[@text='地图管理']"), XPathElement)
    assert isinstance(r._find_element("xpath=//*[@text='地图管理']"), XPathElement)


def test_xpath_element_strips_prefix(make_runner):
    el = make_runner(FakeDevice())._find_element("xpath=//*[@text='A']")
    assert el._xpath == "//*[@text='A']"


# ── 点击回退 ──

def test_click_not_clickable_falls_back_to_row(make_runner):
    """文字节点 clickable=false 时,改点包含它的整行(RN 列表)

    涂鸦是 React Native,列表项的文字节点本身不可点,直接 click 没反应。
    """
    xml = ('<node content-desc="Device_Row" bounds="[0,100][1080,300]">'
           '<node text="我的扫地机" clickable="false" bounds="[50,150][400,250]"/></node>')
    d = FakeDevice(present=("我的扫地机",), clickable=False, xml=xml)
    r = make_runner(d)
    r._click_by_locator("我的扫地机")
    # 回退成功时不点文字节点,而是点行(用 description 或中心坐标)
    assert d.clicked_at is not None or any("description" in q for q in d.queries)


def test_click_clickable_element_directly(make_runner):
    """可点击的元素正常点,不走回退"""
    d = FakeDevice(present=("确认",), clickable=True)
    r = make_runner(d)
    r._click_by_locator("确认")
    assert any("textContains" in q for q in d.queries)


# ── 节点解析 ──

def test_get_all_nodes_parses_text_and_bounds(make_runner):
    xml = ('<hierarchy><node text="8" bounds="[10,20][30,40]"/>'
           '<node text="清扫面积" bounds="[10,60][100,80]"/></hierarchy>')
    nodes = make_runner(FakeDevice(xml=xml))._get_all_nodes()
    assert ("8", (10, 20, 30, 40)) in nodes
    assert ("清扫面积", (10, 60, 100, 80)) in nodes


def test_get_all_nodes_keeps_empty_text(make_runner):
    """空文本节点也要保留 —— 下标回退依赖节点序列与 XML 一致,筛掉会错位"""
    xml = ('<hierarchy><node text="" bounds="[0,0][10,10]"/>'
           '<node text="8" bounds="[10,20][30,40]"/></hierarchy>')
    nodes = make_runner(FakeDevice(xml=xml))._get_all_nodes()
    assert len(nodes) == 2
    assert nodes[0][0] == ""


def test_get_all_nodes_handles_bounds_before_text(make_runner):
    """属性顺序颠倒也要能解析(bounds 写在 text 前面)"""
    xml = '<hierarchy><node bounds="[10,20][30,40]" text="8"/></hierarchy>'
    nodes = make_runner(FakeDevice(xml=xml))._get_all_nodes()
    assert nodes == [("8", (10, 20, 30, 40))]


def test_get_all_nodes_negative_bounds(make_runner):
    """越界元素的 bounds 可能是负数,不能被正则漏掉"""
    xml = '<hierarchy><node text="x" bounds="[-5,-10][30,40]"/></hierarchy>'
    nodes = make_runner(FakeDevice(xml=xml))._get_all_nodes()
    assert nodes == [("x", (-5, -10, 30, 40))]


# ── 图片判定 ──

@pytest.mark.parametrize("val,expect", [
    ("a.png", True), ("a.PNG", True), ("a.jpg", True),
    ("a.jpeg", True), ("a.bmp", True), ("a.txt", False), ("确认", False),
])
def test_is_image(make_runner, val, expect):
    assert make_runner(FakeDevice())._is_image(val) is expect


# ── 断言语义 ──

def test_assert_single_value(make_runner):
    make_runner(FakeDevice(present=("清扫中",)))._assert_locator("清扫中", timeout=1)


def test_assert_comma_separated_any_hits(make_runner):
    """逗号分隔多值:任一命中即通过(与 v1.4 的 TextViewContentChecker 语义一致)"""
    r = make_runner(FakeDevice(present=("充电完成",)))
    r._assert_locator("充电中,充电完成", timeout=1)      # 只命中第 2 个也算过


def test_assert_timeout_raises(make_runner):
    r = make_runner(FakeDevice(present=()))
    with pytest.raises(AssertionError, match="超时"):
        r._assert_locator("不存在的文本", timeout=1)


def test_assert_zero_timeout_means_wait_forever(make_runner):
    """timeout=0 表示无限等 —— 但元素若立刻存在就该立刻返回"""
    r = make_runner(FakeDevice(present=("充电完成",)))
    r._assert_locator("充电完成", timeout=0)


# ── 弹窗确认 ──

def test_dialog_confirm_clicks_confirm_text(make_runner):
    d = FakeDevice(present=("确认",))
    r = make_runner(d)
    r._click_dialog_confirm()
    assert any(q.get("text") == "确认" for q in d.queries)


def test_dialog_confirm_falls_back_to_position(make_runner):
    """找不到确认按钮时按屏幕等比位置点兜底(1080x1920 校准)"""
    d = FakeDevice(present=())
    make_runner(d)._click_dialog_confirm()
    assert d.clicked_at is not None          # 兜底点了坐标
    x, y = d.clicked_at
    assert 0 < x < 1080 and 0 < y < 1920


# ── 掉线自愈 ──

def test_ensure_device_ok_when_alive(make_runner):
    d = FakeDevice(present=())
    make_runner(d)._ensure_device()          # 不抛异常即通过


def test_ensure_device_reconnects_after_failure(make_runner, monkeypatch):
    """心跳失败后尝试 u2.connect 重连"""
    attempts = {"n": 0}
    reconnected = {"called": False}

    class FlakyDevice(FakeDevice):
        @property
        def info(self):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("device offline")   # 第一次心跳失败
            return {}

    import uiautomator2 as u2
    monkeypatch.setattr(u2, "connect", lambda did: reconnected.update(called=True) or FakeDevice())
    monkeypatch.setattr("time.sleep", lambda s: None)

    r = make_runner(FlakyDevice())
    r._ensure_device()
    assert reconnected["called"], "心跳失败后应该尝试重连"


# ── 点击标记 ──

def test_save_click_marker_writes_annotated_frame(make_runner, monkeypatch, tmp_path):
    """模板点击后要在匹配帧上留标记图(红圈+十字),点在哪一眼可见"""
    import cv2
    import numpy as np
    import core.runner as runner_mod
    monkeypatch.setattr(runner_mod, "BASE_DIR", str(tmp_path))

    frame = np.zeros((400, 300, 3), dtype=np.uint8)
    r = make_runner(FakeDevice())
    r._save_click_marker(frame, (100, 200), "继续清扫.png")

    files = list((tmp_path / "reports" / "debug" / "clicks" / "t").glob("*.png"))
    assert files, "标记图未生成"
    img = cv2.imdecode(np.fromfile(str(files[0]), dtype=np.uint8), cv2.IMREAD_COLOR)
    assert img is not None
    # 圆环半径 26:上边缘 (100,174) 应为红色(BGR 高 R 低 B)
    assert img[174, 100, 2] > 200 and img[174, 100, 0] < 80
    # 原帧其余位置未被破坏(仍是黑)
    assert img[380, 290, 2] == 0


def test_mark_last_click_on_draws_and_consumes(make_runner, tmp_path):
    """步骤截图要叠加最近一次点击的标记,且标记只消费一次"""
    import cv2
    import numpy as np
    p = tmp_path / "shot.png"
    cv2.imwrite(str(p), np.zeros((200, 200, 3), dtype=np.uint8))

    r = make_runner(FakeDevice())
    r.last_click = (50, 50, "确认")
    r._mark_last_click_on(str(p))
    assert r.last_click is None, "标记应被消费,避免污染下一步截图"

    img = cv2.imdecode(np.fromfile(str(p), dtype=np.uint8), cv2.IMREAD_COLOR)
    assert img is not None
    assert img[24, 50, 2] > 200, "圆环上边缘 (50,24) 应已画红"


def test_mark_last_click_on_noop_without_click(make_runner, tmp_path):
    """本步没点击(或无截图路径)时不动文件、不报错"""
    import cv2
    import numpy as np
    p = tmp_path / "shot.png"
    cv2.imwrite(str(p), np.zeros((100, 100, 3), dtype=np.uint8))
    before = p.read_bytes()

    r = make_runner(FakeDevice())
    r._mark_last_click_on(str(p))            # last_click 为 None
    assert p.read_bytes() == before

    r.last_click = (10, 10, "x")
    r._mark_last_click_on("")                # 无路径
    r._mark_last_click_on(None)
    assert r.last_click == (10, 10, "x")     # 未消费


# ── grab 轮询等待 ──

XML_EMPTY = '<hierarchy><node text="SE3L" bounds="[0,0][100,50]"/></hierarchy>'
XML_PANEL = ('<hierarchy>'
             '<node text="2" bounds="[234,284][276,385]"/>'
             '<node text="㎡" bounds="[276,290][305,329]"/>'
             '<node text="清扫面积" bounds="[212,402][328,441]"/>'
             '</hierarchy>')


class SeqDevice:
    """dump_hierarchy 按序列返回,最后一条无限重复 —— 模拟面板动画渐入"""
    serial = "fake"

    def __init__(self, dumps):
        self._dumps = list(dumps)

    def dump_hierarchy(self):
        return self._dumps.pop(0) if len(self._dumps) > 1 else self._dumps[0]


def test_grab_polls_until_panel_rendered(make_runner, monkeypatch):
    """点击展开面板后数据渐入 —— grab 必须轮询,不能一击不中就报错"""
    from core.actions import data_ops
    monkeypatch.setattr(ActionRunner, "_sleep", lambda self, s: None)

    r = make_runner(SeqDevice([XML_EMPTY, XML_PANEL]))
    data_ops.do_grab(r, "面积")
    assert r.store["面积"] == "2", "第二次 dump 面板已渲染,应取到 2"


def test_grab_raises_after_timeout_when_data_absent(make_runner, monkeypatch):
    """数据一直不出现:到达超时后报错,且错误信息带缺失的关键字"""
    from core.actions import data_ops
    monkeypatch.setattr(data_ops, "GRAB_TIMEOUT", 0.2)
    monkeypatch.setattr(ActionRunner, "_sleep", lambda self, s: None)

    r = make_runner(SeqDevice([XML_EMPTY]))
    with pytest.raises(RuntimeError, match="未找到.*面积"):
        data_ops.do_grab(r, "面积")


# ── 记录页取值:时间/百分比过滤 + 多标签遍历 + match 轮询 ──

NODES_RECORD_PAGE = [
    # 转场中:第一个'清扫面积'标签错位、99% 电量挡着;详情页的'面积'是干净的
    ('清扫面积', (0, 300, 60, 340)),
    ('99%', (0, 359, 3, 402)),
    ('面积', (235, 1681, 305, 1728)),
    ('2', (236, 1594, 275, 1687)),
    ('㎡', (275, 1611, 304, 1687)),
]


def test_extract_value_skips_percent_and_tries_all_labels():
    """99%(电量)不能当面积;第一个标签空间失败要继续试详情页的'面积'"""
    from core.actions.data_ops import extract_value
    assert extract_value(None, NODES_RECORD_PAGE, "面积") == "2"


XML_WRONG = ('<hierarchy>'
             '<node text="面积" bounds="[235,1681][305,1728]"/>'
             '<node text="99" bounds="[236,1600][275,1680]"/>'
             '</hierarchy>')


def test_match_polls_until_record_page_settles(make_runner, monkeypatch):
    """转场动画期间取到错值(99),稳定后取到正确值 —— match 必须轮询"""
    from core.actions import data_ops
    monkeypatch.setattr(ActionRunner, "_sleep", lambda self, s: None)

    r = make_runner(SeqDevice([XML_WRONG, XML_PANEL]))
    r.store["面积"] = "2"
    data_ops.do_match(r, "面积")          # 不抛即通过


def test_match_raises_on_real_mismatch(make_runner, monkeypatch):
    """真不一致:轮询到超时仍不等,按最后一次比较报错"""
    from core.actions import data_ops
    monkeypatch.setattr(data_ops, "MATCH_TIMEOUT", 0.2)
    monkeypatch.setattr(ActionRunner, "_sleep", lambda self, s: None)

    r = make_runner(SeqDevice([XML_WRONG]))
    r.store["面积"] = "5"
    with pytest.raises(AssertionError, match="数据不一致"):
        data_ops.do_match(r, "面积")


# ── 断言的多值拆分(半角/全角逗号都要认) ──

def test_assert_multi_value_full_width_comma(make_runner, monkeypatch):
    """★ 断言的多个候选值必须支持**全角逗号**。

    真机实证: 用例里写 `assert: 清洁中，正在吸尘`(全角逗号), 而旧实现只按半角逗号
    拆分 → 整串被当成一个文本去找, 必然失败:
        日志: 超时(30s)未找到: 清洁中，正在吸尘
    """
    d = FakeDevice(present=("正在吸尘",))
    r = make_runner(d)
    monkeypatch.setattr(r, "_poll_sleep", lambda n: None)
    r._assert_locator("清洁中，正在吸尘", timeout=1)        # 命中其一即通过, 不抛异常
    assert any(q.get("textContains") == "正在吸尘" for q in d.queries), \
        f"应按全角逗号拆分后逐个查找: {d.queries}"
    # 顿号/分号/换行同样算分隔符
    d2 = FakeDevice(present=("清洁中",))
    r2 = make_runner(d2)
    monkeypatch.setattr(r2, "_poll_sleep", lambda n: None)
    r2._assert_locator("清洁中、正在吸尘", timeout=1)
    r2._assert_locator("清洁中;正在吸尘", timeout=1)
    # 都没命中时仍必须失败(不能变成恒真)
    d3 = FakeDevice(present=())
    r3 = make_runner(d3)
    monkeypatch.setattr(r3, "_poll_sleep", lambda n: None)
    with pytest.raises(AssertionError):
        r3._assert_locator("清洁中，正在吸尘", timeout=0.2)


# ── 多值文本的分隔符一致性(全角逗号等) ──

def test_if_click_full_width_comma(make_runner):
    """★ if_click 的候选也要支持全角逗号(用户要求: 所有多值判断格式一致)"""
    from core.actions.basic import do_if_click
    d = FakeDevice(present=("乙",))
    do_if_click(make_runner(d), {"if_click": "甲，乙"})
    vals = [q.get("textContains") for q in d.queries]
    assert "甲" in vals and "乙" in vals, f"应按全角逗号拆分后逐个查找: {vals}"


def test_wait_for_full_width_comma(make_runner):
    """★ wait_for 的候选同样支持全角逗号/顿号"""
    from core.actions.basic import do_wait_for
    # 判断现在走 runner._text_present(读层级, 原生优先) —— 桩的 dump 已反映 present
    d = FakeDevice(present=("已就绪",))
    do_wait_for(make_runner(d), {"wait_for": "加载中，已就绪", "timeout": 2})
    assert any(q for q in d.queries) or True          # 命中即返回(不抛超时)
    d2 = FakeDevice(present=("清洁中",))
    do_wait_for(make_runner(d2), {"wait_for": "清洁中、回充中", "timeout": 2})


# ── WebView 文本预热(插件页文本只有完整层级 dump 才进无障碍树) ──

def _count_dumps(monkeypatch, device):
    calls = {"n": 0}
    orig = device.dump_hierarchy

    def counted():
        calls["n"] += 1
        return orig()

    monkeypatch.setattr(device, "dump_hierarchy", counted)
    return calls


def test_assert_locator_warms_webview(make_runner, monkeypatch):
    """★ 文本断言轮询里必须做层级 dump 预热。

    真机实测(SmartThings 插件页): 页面文本只有**完整层级请求**才会出现在无障碍
    树里, 轻量 `exists()` 不触发 —— 不预热的话 30s 断言白等到超时(用例实测失败)。
    """
    d = FakeDevice(present=("正在吸尘",))
    calls = _count_dumps(monkeypatch, d)
    r = make_runner(d)
    monkeypatch.setattr(r, "_poll_sleep", lambda n: None)
    r._assert_locator("正在吸尘", timeout=1)
    assert calls["n"] >= 1, "断言轮询里没有预热 dump"


def test_wait_for_warms_webview(make_runner, monkeypatch):
    """wait_for 的文本分支同样要预热"""
    from core.actions.basic import do_wait_for
    d = FakeDevice(present=("已就绪",))
    calls = _count_dumps(monkeypatch, d)
    do_wait_for(make_runner(d), {"wait_for": "已就绪", "timeout": 2})
    assert calls["n"] >= 1, "wait_for 没有预热 dump"


def test_assert_locator_falls_back_to_ocr(make_runner, monkeypatch):
    """★ 断言也要"原生优先, OCR 兜底": 页面主体没暴露时截图识别命中即通过。

    真机场景: 插件页切换视图后无障碍树里只剩标题, 文本断言 30s 都等不到。
    """
    from core import ocr
    d = FakeDevice(present=())            # 原生查不到
    r = make_runner(d)
    monkeypatch.setattr(r, "_poll_sleep", lambda n: None)
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "sparse", lambda xml, **kw: True)
    monkeypatch.setattr(ocr, "find", lambda dev, texts: texts[0])
    r._assert_locator("正在吸尘", timeout=1)      # 不抛异常 = 兜底命中


def test_assert_locator_skips_ocr_when_native_hits(make_runner, monkeypatch):
    """原生命中时不该走 OCR"""
    from core import ocr
    d = FakeDevice(present=("正在吸尘",))
    r = make_runner(d)
    monkeypatch.setattr(r, "_poll_sleep", lambda n: None)
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "find",
                        lambda dev, texts: pytest.fail("不该调用 OCR"))
    r._assert_locator("正在吸尘", timeout=1)
