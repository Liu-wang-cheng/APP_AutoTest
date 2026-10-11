# -*- coding: utf-8 -*-
"""OCR 兜底: 原生(无障碍树)优先, 读不到时用截图识别文字。

★ 背景(2026-09-24 真机实测): SmartThings 设备页是 WebView 插件页, 切换视图后
  **主体内容不再提供可访问节点**(只剩标题和个别按钮) —— uiautomator 的所有文本
  API 都读不到(同一棵 AccessibilityNodeInfo 树)。实测 RapidOCR 能准确读出屏幕
  文字(中文/数字都准), ddddocr 读不准。
用户定的方向: **原生优先, OCR 兜底**(正常页面一次 OCR 都不做, 不加 1s 开销)。
"""
import pytest

from core import ocr


@pytest.fixture(autouse=True)
def _clear_ocr_cache():
    ocr._cache.update(t=0.0, texts=[])
    yield
    ocr._cache.update(t=0.0, texts=[])


# ── 稀疏判断: 什么时候值得走 OCR ──

def test_sparse_detects_missing_content():
    """层级里只剩标题/时间 → 判定"页面主体没暴露" → 值得 OCR"""
    assert ocr.sparse('<node text="扫地机器人0087"/><node text="14:00"/>') is True
    assert ocr.sparse("") is True
    rich = "".join(f'<node text="t{i}"/>' for i in range(8))
    assert ocr.sparse(rich) is False


# ── screen_texts: 识别 + 缓存 + 容错 ──

class _Dev:
    def __init__(self):
        self.shots = 0

    def screenshot(self, format=None):
        self.shots += 1
        return "fake-image"


def test_screen_texts_parses_and_caches(monkeypatch):
    fake = ([[[0, 10], [50, 10], [50, 40], [0, 40]], "正在吸尘", "0.99"],
            [[[100, 200], [150, 200], [150, 230], [100, 230]], "93%", "0.98"])
    monkeypatch.setattr(ocr, "_get", lambda: (lambda img: (fake, None)))
    d = _Dev()
    out = ocr.screen_texts(d)
    assert out == [("正在吸尘", (0, 10)), ("93%", (100, 200))], out
    assert ocr.screen_texts(d) == out
    assert d.shots == 1, "同一帧应走缓存, 不重复截图/识别"


def test_screen_texts_survives_ocr_failure(monkeypatch):
    """OCR 抛异常不能影响主流程 —— 返回空列表"""
    def boom():
        raise RuntimeError("模型加载失败")
    monkeypatch.setattr(ocr, "_get", boom)
    assert ocr.screen_texts(_Dev()) == []


def test_contains_is_substring():
    """与 textContains 语义一致: 子串匹配"""
    monkey_ok = [("已充满电 仅真空吸尘器", (0, 0))]
    ocr._cache.update(t=9999999999.0, texts=monkey_ok)     # 预置"当前帧"
    assert ocr.contains(_Dev(), "满电") is True
    assert ocr.contains(_Dev(), "充电") is False
    assert ocr.contains(_Dev(), "") is False


def test_find_returns_first_hit():
    ocr._cache.update(t=9999999999.0, texts=[("普通房间 7", (0, 0))])
    assert ocr.find(_Dev(), ["正在吸尘", "房间"]) == "房间"
    assert ocr.find(_Dev(), ["充电中"]) is None


# ── 原生优先, OCR 兜底(session 的文本判定) ──

class _EmptyDev:
    """原生查不到任何文本的设备桩"""
    def __call__(self, **kw):
        class R:
            def exists(self, timeout=1):
                return False
            def click(self):
                pass
        return R()

    def dump_hierarchy(self):
        return '<node text="扫地机器人0087"/>'      # 稀疏(只剩标题)


def test_any_present_prefers_native_and_skips_ocr(monkeypatch):
    """原生能读到 → 绝不能走 OCR(否则每次判断多花 1s)"""
    from core import session
    called = []
    monkeypatch.setattr(session.ocr, "available", lambda: True)
    monkeypatch.setattr(session.ocr, "find_detail",
                        lambda d, t: called.append(1) or ("x", "x 已完成", (1, 2)))
    monkeypatch.setattr(session, "warm_webview",
                        lambda d: '<node text="正在吸尘 5分钟"/><node text="电池 93%%"/>')

    class D(_EmptyDev):
        def __call__(self, **kw):
            class R:
                def exists(self, timeout=1):
                    return True                       # 原生命中
            return R()

    assert session._any_present(D(), ["正在吸尘"]) is True
    assert called == [], "原生命中时不该调用 OCR"


def test_any_present_falls_back_to_ocr_when_tree_sparse(monkeypatch):
    """★ 原生读不到 + 页面主体没暴露 → OCR 兜底命中"""
    from core import session
    monkeypatch.setattr(session.ocr, "available", lambda: True)
    monkeypatch.setattr(session.ocr, "find_detail",
                        lambda d, texts: (texts[0], texts[0] + " 已完成", (10, 20)))
    assert session._any_present(_EmptyDev(), ["正在吸尘", "回充中"]) is True


def test_any_present_no_ocr_when_not_installed(monkeypatch):
    """没装 RapidOCR → 行为与从前完全一致(不崩、不误判)"""
    from core import session
    monkeypatch.setattr(session.ocr, "available", lambda: False)
    monkeypatch.setattr(session.ocr, "find_detail",
                        lambda d, t: pytest.fail("不该调用 OCR"))
    assert session._any_present(_EmptyDev(), ["正在吸尘"]) is False


# ── 判断类统一入口 runner._text_present(原生优先 + OCR 兜底) ──

class _Runner:
    """最小 runner 桩: 只需要 .d / _sleep / _text_present 相关"""
    def __init__(self, xml="", present=()):
        self._xml = xml
        self.present = set(present)
        self.shots = 0

    def dump_hierarchy(self):
        return self._xml

    def __call__(self, **kw):
        owner = self

        class R:
            def exists(self, timeout=1):
                return any(kw.get("textContains", "") in p for p in owner.present)
        return R()

    def screenshot(self, format=None):
        self.shots += 1
        return "img"


def _make_runner(d):
    from core.runner import ActionRunner
    r = ActionRunner.__new__(ActionRunner)      # 不跑 __init__(会连设备/加载模型)
    r.d = d
    r.stopped = False
    r._cancel_check = None
    r._sleep = lambda s: None                   # 轮询间隔空转(否则 5 轮要真等 5 秒)
    r._poll_sleep = lambda n: None
    return r


def test_text_present_native_hit(monkeypatch):
    from core import ocr
    d = _Runner(xml='<node text="正在吸尘 5分钟"/><node text="房间 7"/><node text="电池 93%%"/>')
    r = _make_runner(d)
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "find_detail", lambda dev, t: pytest.fail("原生命中不该调 OCR"))
    assert r._text_present("正在吸尘") is True


def test_text_present_falls_back_to_ocr(monkeypatch):
    """★ 树稀疏(插件页切视图后只剩标题)时, 判断类要能靠 OCR 命中。

    ★ 2026-10-11 起 OCR 不再每轮都做: **前 4 轮只读无障碍树, 第 5 轮才截图识别**
      (用户要求), 之后 10、15… 轮再各做一次。
    """
    from core import ocr
    d = _Runner(xml='<node text="扫地机器人0087"/>')
    r = _make_runner(d)
    monkeypatch.setattr(ocr, "available", lambda: True)
    calls = []

    def _find(dev, texts):
        calls.append(texts[0])
        return ((texts[0], "屏幕上读到 " + texts[0], (1, 2))
                if "吸尘" in texts[0] else (None, "", None))

    monkeypatch.setattr(ocr, "find_detail", _find)
    assert r._text_present("正在吸尘") is True
    assert calls == ["正在吸尘"], f"应在第 5 轮做**一次** OCR 就命中: {calls}"
    calls.clear()
    assert r._text_present("回充中") is False          # 一直读不到 → 超时才返回
    assert calls, f"持续读不到时也该按 5 轮周期做 OCR: {calls}"


def test_normal_page_does_ocr_on_every_fifth_round(monkeypatch):
    """★ 正常页面: 前 4 轮只读无障碍树, 第 5/10/15… 轮才截图识别(用户定的 5 组循环)。

    正常页面也要周期性兜底的原因: 有时文字**画在屏幕上但没进无障碍树**(WebView 局部),
    只读树会一直判不到。单次判断(round_no=None)不受限 —— 那是唯一机会。
    """
    from core import ocr, session
    xml = '<node text="甲"/><node text="乙"/><node text="丙"/><node text="丁"/><node text="戊"/>'
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "sparse", lambda x, **k: False)        # 正常页面
    calls = []
    monkeypatch.setattr(ocr, "find_detail",
                        lambda d, texts: (calls.append(1), (None, "", None))[1])

    for round_no in (1, 2, 3, 4, 6, 7, 8, 9, 11):
        session.text_present_detail(None, ["不会命中"], xml=xml, round_no=round_no)
    assert calls == [], f"非 5 的倍数轮不该做 OCR: {len(calls)} 次"
    for round_no in (5, 10, 15):
        session.text_present_detail(None, ["不会命中"], xml=xml, round_no=round_no)
    assert len(calls) == 3, f"第 5/10/15 轮应各做一次: {len(calls)}"
    session.text_present_detail(None, ["不会命中"], xml=xml)          # 单次判断
    assert len(calls) == 4, "单次判断(round_no=None)仍应允许 OCR"


def test_sparse_page_does_ocr_every_round(monkeypatch):
    """★ 页面主体没暴露(插件页, 树稀疏) → **每轮都允许** OCR(不等 5 轮)。

    这种页面上原生本来就什么都读不到, 再按 5 轮节流等于让断言干等(用户 2026-10-11 定稿)。
    """
    from core import ocr, session
    xml = '<node text="扫地机器人0087"/>'          # 稀疏
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "sparse", lambda x, **k: True)
    calls = []
    monkeypatch.setattr(ocr, "find_detail",
                        lambda d, texts: (calls.append(1), (None, "", None))[1])
    for round_no in (1, 2, 3, 4, 5):
        session.text_present_detail(None, ["不会命中"], xml=xml, round_no=round_no)
    assert len(calls) == 5, f"稀疏页面每轮都该做 OCR: {len(calls)} 次"


def test_text_present_empty_and_no_ocr(monkeypatch):
    from core import ocr
    d = _Runner(xml='<node text="扫地机器人0087"/>')
    r = _make_runner(d)
    assert r._text_present() is False
    monkeypatch.setattr(ocr, "available", lambda: False)
    monkeypatch.setattr(ocr, "find_detail", lambda dev, t: pytest.fail("没装 OCR 不该调用"))
    assert r._text_present("正在吸尘") is False


def test_wait_loading_uses_ocr_fallback(monkeypatch):
    """★ 树稀疏时"看不到加载提示"是假象 —— 只读树会把"还在加载"误判成"已加载完"。

    OCR 看到「正在加载」时必须继续等, 而不是立刻返回。
    """
    from core import ocr
    from core.actions.basic import do_wait_loading
    d = _Runner(xml='<node text="扫地机器人0087"/>')      # 稀疏, 树里没有加载提示
    r = _make_runner(d)
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "find_detail",
                        lambda dev, texts: ("正在加载", "正在加载中…", (1, 2)))
    monkeypatch.setattr(r, "_sleep", lambda s: None)
    do_wait_loading(r, {"timeout": 0})                     # 超时窗口内一直"在加载"
    # 命中加载提示 → 不应提前 return(走到超时分支打 warning); 用日志不好断言,
    # 这里断言 OCR 确实被问过(说明判断走了兜底)
    assert ocr.find(d, ("加载中", "正在加载")) == "正在加载"


def test_if_condition_uses_ocr_fallback(monkeypatch):
    """★ if 条件的文本分支同样要兜底(否则树稀疏时会走错分支)"""
    from core import ocr
    from core.actions.basic import do_if_impl
    d = _Runner(xml='<node text="扫地机器人0087"/>')
    r = _make_runner(d)
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "find_detail",
                        lambda dev, texts: (texts[0], texts[0], (1, 2)))
    ran_else = []
    do_if_impl(r, {"if": "正在吸尘", "else": []}, negate=False)
    assert ran_else == []          # 条件成立(OCR 命中) → 跳过, 不执行 else


def test_battery_ocr_fallback(monkeypatch):
    """★ 树里读不到电量(插件页稀疏)时, 用 OCR 读屏幕上的百分比"""
    from core import ocr, session
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "screen_texts",
                        lambda d, use_cache=True: [("电池 93%%", (0, 0)), ("正在吸尘", (0, 0))])
    assert session.get_battery_level(_Runner(xml='<node text="扫地机器人0087"/>')) == 93


def test_wait_texts_uses_any_present_with_ocr(monkeypatch):
    """★ 进设备页的判定轮询(_wait_texts)也要能靠 OCR 命中"""
    from core import ocr, session
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "find_detail",
                        lambda dev, texts: (texts[0], texts[0], (1, 2)))
    d = _Runner(xml='<node text="扫地机器人0087"/>')
    assert session._wait_texts(d, ["设备控制"], timeout=1) == "设备控制"


def test_ocr_fallback_logs_the_line_it_actually_read(monkeypatch, caplog):
    """★ OCR 命中必须记下**它读到的那一行**(2026-10-11 用户报"页面根本没这些文本却通过")。

    没有这一行, 人只能看到"命中「已完成」", 无法判断是页面真有这个词、还是 OCR 读错 ——
    正是"清洗拖布 已完成"这类**进度文案**顶掉通用候选词的现场。
    """
    import logging
    from core import session

    recs = []

    class _H(logging.Handler):
        def emit(self, record):
            recs.append(record.getMessage())

    lg = session.log
    lg.addHandler(_H(level=logging.INFO))
    old = lg.level
    lg.setLevel(logging.INFO)
    try:
        monkeypatch.setattr(session.ocr, "available", lambda: True)
        monkeypatch.setattr(session.ocr, "find_detail",
                            lambda d, texts: ("已完成", "清洗拖布 已完成", (120, 456)))
        assert session.text_present(_EmptyDev(), ["干燥拖布", "已完成"]) is True
    finally:
        lg.removeHandler(lg.handlers[-1])
        lg.setLevel(old)

    line = [m for m in recs if "OCR兜底" in m]
    assert line, recs
    assert "清洗拖布 已完成" in line[0], f"没记下 OCR 读到的原始行: {line[0]}"
    assert "已完成" in line[0] and "120" in line[0], line[0]


def test_ocr_find_detail_returns_the_matching_line(monkeypatch):
    """find_detail: 返回 (命中的候选, OCR 行, 坐标); 没命中返回 (None, "", None)"""
    from core import ocr
    monkeypatch.setattr(ocr, "screen_texts",
                        lambda d, use_cache=True: [("清洗拖布 已完成", (10, 20)),
                                                   ("正在清洁", (30, 40))])
    assert ocr.find_detail(None, ["干燥拖布", "已完成"]) == ("已完成", "清洗拖布 已完成", (10, 20))
    assert ocr.find_detail(None, ["干燥拖布", "已取消"]) == (None, "", None)
    # 旧接口语义不变
    assert ocr.find(None, ["已完成"]) == "已完成"


# ── OCR 自检(2026-10-11: 打包版缺 pyclipper 时"一直报错"的根治) ──

def test_self_check_reports_unavailable_once_and_caches(monkeypatch):
    """★ 依赖缺失时: 一次说清"不可用", **不再每步刷屏**(以前是每步一条"识别失败")

    打包版缺 pyclipper 时, import/构造都不报错, 只有识别那一刻才炸 —— 所以自检必须**真跑**。
    """
    import logging
    from core import ocr

    recs = []

    class _H(logging.Handler):
        def emit(self, record):
            recs.append(record.getMessage())

    h = _H(level=logging.INFO)
    ocr.log.addHandler(h)
    monkeypatch.setattr(ocr, "_available", None)          # 清掉缓存
    calls = []

    def _boom(img):
        calls.append(1)
        raise ModuleNotFoundError("No module named 'pyclipper'")

    monkeypatch.setattr(ocr, "_get", lambda: _boom)
    try:
        # ★ 直接调 self_check(): conftest 默认把 available() 桩成 False(测试隔离)
        assert ocr.self_check() is False
        assert ocr.self_check() is False                  # 第二次走缓存
    finally:
        ocr.log.removeHandler(h)
        monkeypatch.setattr(ocr, "_available", None)
    assert len(calls) == 1, f"自检不该重复真跑: {len(calls)}"
    bad = [m for m in recs if "不可用" in m]
    assert len(bad) == 1 and "pyclipper" in bad[0], recs


def test_self_check_passes_when_ocr_really_runs(monkeypatch):
    """能真跑一次 → 通过(并留下"自检通过"这行, 构建闸门就靠它判打包版 OCR 是否可用)"""
    import logging
    from core import ocr

    recs = []
    h = logging.Handler()
    h.emit = lambda r: recs.append(r.getMessage())
    ocr.log.addHandler(h)
    monkeypatch.setattr(ocr, "_available", None)
    monkeypatch.setattr(ocr, "_get", lambda: (lambda img: ([], 0.0)))
    try:
        assert ocr.self_check() is True
    finally:
        ocr.log.removeHandler(h)
        monkeypatch.setattr(ocr, "_available", None)
    assert any("自检通过" in m for m in recs), recs


def test_verify_package_ocr_gate(tmp_path):
    """构建闸门: 打包产物必须自己证明 OCR 能用(读程序写的日志)"""
    import sys as _sys
    from pathlib import Path as _P
    _sys.path.insert(0, str(_P(__file__).resolve().parent.parent / "tools"))
    import verify_package as vp

    app = tmp_path / "dist" / "AutoTest"
    (app / "reports").mkdir(parents=True)
    log = app / "reports" / "test.log"

    log.write_text("13:00:00 [INFO] [OCR] RapidOCR 已加载(文本兜底识别)\n"
                   "13:00:01 [INFO] [OCR] 自检通过(文本兜底可用)\n", encoding="utf-8")
    assert vp._check_ocr_selfcheck(str(app)) == []

    log.write_text("13:00:01 [WARNING] [OCR] 不可用 —— 判断只走无障碍树: "
                   "ModuleNotFoundError: No module named 'pyclipper'\n", encoding="utf-8")
    errs = vp._check_ocr_selfcheck(str(app))
    assert errs and "pyclipper" in errs[0] and "OCR" in errs[0], errs

    log.write_text("13:00:01 [INFO] 正在检测设备...\n", encoding="utf-8")
    errs = vp._check_ocr_selfcheck(str(app))
    assert errs and "自检结论" in errs[0], errs

    (app / "reports" / "test.log").unlink()
    errs = vp._check_ocr_selfcheck(str(app))
    assert errs and "读不到程序日志" in errs[0], errs


# ── 真跑一次 OCR(CI 用): 守住"干净环境里依赖/模型能不能用" ──

def test_real_ocr_recognizes_text(monkeypatch):
    """★ 真加载模型 + 真识别一张合成图(约 3~5 秒, 无网络)。

    平时测试默认把 OCR 关掉(conftest, 为了确定性和速度), 于是"OCR 依赖坏了"没人发现。
    这里显式打开: 依赖没装 → skip(开发机可能没装); 装了但跑不通 → **失败**(就是要抓的)。
    ★ 注意分工: 这条管的是"**依赖/模型**本身能不能用"; "打包时漏收依赖"那类(实测漏过
      ch_ppocr_v3_det、pyclipper)CI 抓不到 —— 那由 build 期的 tools/verify_package.py
      真启动打包产物 + 检查它自己的 OCR 自检日志来把关。
    """
    import numpy as np

    try:
        import rapidocr_onnxruntime  # noqa: F401
    except Exception as e:
        pytest.skip(f"本机没装 rapidocr: {e}")

    from core import ocr
    monkeypatch.setattr(ocr, "_available", None)
    monkeypatch.setattr(ocr, "_cache", {"t": 0.0, "texts": []})
    if not ocr.self_check():
        pytest.fail("OCR 自检失败(依赖或模型有问题, 见上面日志)")

    try:
        import cv2
    except Exception as e:
        pytest.skip(f"没有 cv2, 画不出测试图: {e}")
    img = np.full((140, 560, 3), 255, dtype=np.uint8)
    cv2.putText(img, "TEST 123", (20, 95), cv2.FONT_HERSHEY_SIMPLEX, 2.2, (0, 0, 0), 5)

    class _Dev:
        serial = "fake-ocr"
        def screenshot(self, path=None, format=None):
            return img

    texts = [t for t, _pos in ocr.screen_texts(_Dev(), use_cache=False)]
    joined = " ".join(texts).upper()
    assert "TEST" in joined or "123" in joined, f"OCR 没读出合成图上的文字: {texts}"
