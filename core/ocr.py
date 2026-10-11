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
_cache = {"t": 0.0, "texts": []}
CACHE_SECS = 1.5          # 同一帧不重复识别(一次识别约 1s)
SPARSE_MIN_TEXTS = 5      # 层级里可见文本少于此数 ⇒ 认为页面主体没暴露


def available():
    """装了 RapidOCR 才启用兜底"""
    try:
        import rapidocr_onnxruntime  # noqa: F401
        return True
    except Exception:
        return False


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


def screen_texts(d, use_cache=True):
    """截图 → OCR → [(文本, (x, y))];失败返回空列表(绝不影响主流程)"""
    now = time.time()
    if use_cache and now - _cache["t"] < CACHE_SECS:
        return _cache["texts"]
    try:
        img = d.screenshot(format="opencv")
        res, _ = _get()(img)
    except Exception as e:
        log.warning(f"[OCR] 识别失败(忽略): {e}")
        return []
    out = []
    for item in (res or []):
        try:
            box, txt = item[0], item[1]
            out.append((str(txt), (int(box[0][0]), int(box[0][1]))))
        except Exception:
            continue
    _cache.update(t=now, texts=out)
    return out


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
