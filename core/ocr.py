# -*- coding: utf-8 -*-
"""屏幕文字 OCR 兜底: 无障碍树读不到时, 用截图识别文字。

★ 为什么要它(2026-09-24 真机实测)
  SmartThings 设备页是 WebView 插件页, 页面切换视图后**主体内容不再提供可访问
  节点** —— 只剩页面标题和个别 DOM 按钮。此时 uiautomator 的**所有**文本 API
  (`dump_hierarchy` / `text` / `textContains` / `xpath`) 都读不到, 因为它们读的是
  同一棵 AccessibilityNodeInfo 树; 换 Appium 也一样。
  实测: RapidOCR(离线中文)能把屏幕文字准确读出来(约 1s/张, 中文与数字都准),
        ddddocr 读不准中文与百分比(它被项目用于读定时器纯数字)。

★ 用法(用户定的方向): **原生(无障碍树)优先, OCR 兜底**
  只有原生**查不到**且**页面主体没暴露**时才走 OCR —— 正常页面一次 OCR 都不做,
  不给每次判断加 1s。

★ 依赖: rapidocr-onnxruntime(未安装时 `available()` 为 False, 行为与从前完全一致)
"""
import re
import threading
import time

from core.logger import get_logger

log = get_logger()

_ocr = None
_lock = threading.Lock()
_cache = {"t": 0.0, "texts": [], "boxes": []}   # texts=左上角口径, boxes=整框
CACHE_SECS = 1.5          # 同一帧不重复识别(一次识别约 1s)
SPARSE_MIN_TEXTS = 5      # 层级里可见文本少于此数 ⇒ 认为页面主体没暴露

#: **正常页面**上判断类轮询里每隔几轮做一次 OCR 兜底(用户要求 2026-10-11:
#: "分为5组检测, 前四次普通检测后一次为OCR兜底, 然后继续按这五组循环")。
#: 前 4 轮只读无障碍树(快、准), 第 5 轮才截图识别, 之后 10/15… 轮各一次 ——
#: OCR 一次约 1 秒、且不如原生可靠, 没必要每轮都做。
#: ★ **页面主体没暴露**(树稀疏, 插件页)不走这条: 那种页面每轮都允许 OCR(见
#:   core/session.py::text_present_detail), 否则原生读不到就只能干等。
OCR_EVERY_N_ROUNDS = 5


def round_allows_ocr(round_no):
    """这一轮要不要允许 OCR 兜底(仅用于**正常页面**)。round_no 从 1 开始计数。

    单次判断(没有轮询)传 None —— 那种场合 OCR 是**唯一**的机会, 不该被轮次限制挡掉。
    """
    return round_no is None or (round_no > 0 and round_no % OCR_EVERY_N_ROUNDS == 0)


_available = None          # 自检结果缓存(None = 还没查过)


def self_check():
    """OCR 到底**能不能用** —— 真的构造一次并识别一张小图, 不是只看包装没装。

    ★ 为什么必须真跑一次(2026-10-11 用户报"OCR 还在报错"查出来的): 打包版缺
      `pyclipper`(rapidocr 的运行时依赖)时, `import rapidocr_onnxruntime` 照样成功、
      `RapidOCR()` 构造也照样成功 —— **只有识别的那一刻**才抛
      `No module named 'pyclipper'`。于是日志先打"RapidOCR 已加载", 然后每次判断都刷一条
      "[OCR] 识别失败(忽略)", 看上去像"偶发", 其实兜底**一直是死的**。
      真跑一次之后: 要么一次通过, 要么一次说清"不可用+原因", 不再每步刷屏。
    """
    global _available
    if _available is None:
        try:
            import numpy as np
            _get()(np.full((32, 128, 3), 255, dtype=np.uint8))     # 真的识别一次
            _available = True
            log.info("[OCR] 自检通过(文本兜底可用)")
        except Exception as e:
            _available = False
            log.warning(f"[OCR] 不可用 —— 判断只走无障碍树(不影响用例, 但插件页可能读不到): "
                        f"{type(e).__name__}: {e}")
    return _available


def available():
    """装了 RapidOCR **并且真的跑得起来** 才启用兜底"""
    return self_check()


def _get():
    global _ocr
    with _lock:
        if _ocr is None:
            from rapidocr_onnxruntime import RapidOCR
            log.info("[OCR] RapidOCR 已加载(文本兜底识别)")
            _ocr = RapidOCR()
        return _ocr


def sparse(xml, min_texts=SPARSE_MIN_TEXTS):
    """层级里可见文本太少 ⇒ 页面主体没暴露(插件页典型症状), 值得走 OCR 兜底"""
    return len([t for t in re.findall(r'text="([^"]*)"', xml) if t]) < min_texts


def screen_boxes(d, use_cache=True, exclude=None):
    """截图 → (可选: 屏蔽指定区域) → OCR → [(文本, (x1, y1, x2, y2))]

    exclude: 要**屏蔽掉再做识别**的矩形 [(x1,y1,x2,y2)] —— 手机系统 UI(状态栏/通知栏)。
    ★ 为什么在识别**之前**屏蔽(用户 2026-10-16: "应该把手机系统那一段排除掉识别才对"):
      识别完再丢只是在"结果层面"补救 —— 系统栏上的字仍会被读出来(日志里能看到它命中了
      什么), 也可能在边界上干扰相邻行的切分。抹黑之后再识别, 那一段从一开始就不存在。
    ★ 用**抹黑**而不是裁图: 裁掉会让后面所有行的 y 坐标整体偏移, 每个调用方都得做坐标回填
      (漏一处就是"点错位置"); 抹黑不动坐标系, 调用方无感。
    ★ 缓存键带上屏蔽区域: 同一帧用不同屏蔽范围识别, 结果不能互相顶替。
    """
    rects = tuple(tuple(int(v) for v in r) for r in (exclude or []))
    now = time.time()
    if use_cache and now - _cache["t"] < CACHE_SECS and _cache.get("key") == rects:
        return _cache["boxes"]
    try:
        img = d.screenshot(format="opencv")
        if rects:
            img = img.copy()
            h, w = img.shape[:2]
            for x1, y1, x2, y2 in rects:
                img[max(0, y1):max(0, y2), max(0, x1):max(0, x2)] = 0   # 抹黑: 坐标系不变
        res, _ = _get()(img)
    except Exception as e:
        log.warning(f"[OCR] 识别失败(忽略): {e}")
        return []
    out = []
    for item in (res or []):
        try:
            box, txt = item[0], item[1]
            xs = [int(p[0]) for p in box]
            ys = [int(p[1]) for p in box]
            out.append((str(txt), (min(xs), min(ys), max(xs), max(ys))))
        except Exception:
            continue
    _cache.update(t=now, boxes=out, key=rects,
                  texts=[(t, (b[0], b[1])) for t, b in out])
    return out


def screen_texts(d, use_cache=True, exclude=None):
    """截图 → OCR → [(文本, (x, y))];失败返回空列表(绝不影响主流程)

    (x, y) = 识别框左上角 —— 老调用方的口径, 保持原样; 需要整框的用 screen_boxes()。
    exclude 见 screen_boxes(): 手机系统 UI 的区域在**识别前**抹掉。
    """
    now = time.time()
    rects = tuple(tuple(int(v) for v in r) for r in (exclude or []))
    if use_cache and now - _cache["t"] < CACHE_SECS and _cache.get("key") == rects:
        return _cache["texts"]
    return [(t, (b[0], b[1])) for t, b in screen_boxes(d, use_cache=use_cache,
                                                       exclude=exclude)]


def contains(d, text):
    """屏幕上有没有这段文字(子串匹配, 与 textContains 语义一致)"""
    if not text:
        return False
    return any(text in t for t, _ in screen_texts(d))


def find_detail(d, texts):
    """texts 里第一个在屏幕上出现的(OCR) → (命中的候选, OCR 实际读到的那一行, 坐标)。

    没命中返回 (None, "", None)。

    ★ 为什么要带上"读到的那一行"(2026-10-11 用户报"页面根本没有这些文本却判通过"):
      OCR 兜底读的是**无障碍树里看不到的那部分界面**(插件页主体), 所以"原生没命中"
      是常态、不代表页面上真的没有。日志只写"命中「已完成」"时, 人根本无法判断
      到底是页面真有这个词、还是 OCR 读错了 —— 于是真假通过分不清。
      带上原始行就能一眼看出: 例如「清洗拖布 已完成」这种进度文案顶掉了候选「已完成」。
    """
    for t in texts or []:
        if not t:
            continue
        for line, pos in screen_texts(d):
            if t in line:
                return t, line, pos
    return None, "", None


def find(d, texts):
    """texts 里第一个在屏幕上出现的(OCR), 找不到返回 None"""
    hit, _line, _pos = find_detail(d, texts)
    return hit
