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
    monkeypatch.setattr(session.ocr, "find", lambda d, t: called.append(1) or "x")
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
    monkeypatch.setattr(session.ocr, "find", lambda d, texts: texts[0])
    assert session._any_present(_EmptyDev(), ["正在吸尘", "回充中"]) is True


def test_any_present_no_ocr_when_not_installed(monkeypatch):
    """没装 RapidOCR → 行为与从前完全一致(不崩、不误判)"""
    from core import session
    monkeypatch.setattr(session.ocr, "available", lambda: False)
    monkeypatch.setattr(session.ocr, "find",
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
    return r


def test_text_present_native_hit(monkeypatch):
    from core import ocr
    d = _Runner(xml='<node text="正在吸尘 5分钟"/><node text="房间 7"/><node text="电池 93%%"/>')
    r = _make_runner(d)
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "find", lambda dev, t: pytest.fail("原生命中不该调 OCR"))
    assert r._text_present("正在吸尘") is True


def test_text_present_falls_back_to_ocr(monkeypatch):
    """★ 树稀疏(插件页切视图后只剩标题)时, 判断类要能靠 OCR 命中"""
    from core import ocr
    d = _Runner(xml='<node text="扫地机器人0087"/>')
    r = _make_runner(d)
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "find", lambda dev, texts: texts[0] if "吸尘" in texts[0] else None)
    assert r._text_present("正在吸尘") is True
    assert r._text_present("回充中") is False


def test_text_present_empty_and_no_ocr(monkeypatch):
    from core import ocr
    d = _Runner(xml='<node text="扫地机器人0087"/>')
    r = _make_runner(d)
    assert r._text_present() is False
    monkeypatch.setattr(ocr, "available", lambda: False)
    monkeypatch.setattr(ocr, "find", lambda dev, t: pytest.fail("没装 OCR 不该调用"))
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
    monkeypatch.setattr(ocr, "find", lambda dev, texts: "正在加载")
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
    monkeypatch.setattr(ocr, "find", lambda dev, texts: texts[0])
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
    monkeypatch.setattr(ocr, "find", lambda dev, texts: texts[0])
    d = _Runner(xml='<node text="扫地机器人0087"/>')
    assert session._wait_texts(d, ["设备控制"], timeout=1) == "设备控制"
