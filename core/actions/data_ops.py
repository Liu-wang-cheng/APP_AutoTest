# -*- coding: utf-8 -*-
"""数据抓取与对比: grab 存 store → match 跨页对比(数值允许 ±1 浮动)。

注意: grab/match 不注册进动作分发表 —— 它们与主动作共存于一步,
由 runner._execute 单独处理。
"""
import re
import time

from core.driver import split_texts      # 关键字多个: 半角/全角逗号、顿号、分号、换行
from core.logger import get_logger

log = get_logger()

GRAB_TIMEOUT = 10   # 面板展开动画一般 <1s,10s 足够宽容


def do_grab(runner, keywords):
    """抓取页面上包含关键字的文本及相邻数字,存入 runner.store

    click 展开面板 + grab 常写在同一步:点击后面板有滑入动画,立刻 dump
    层级大概率抓到渲染前的空面板 —— 所以这里轮询等待数据出现,而不是
    一击不中就报错(2026-09-16 全局清扫实测踩坑)。
    """
    if isinstance(keywords, str):
        keywords = split_texts(keywords)
    start = time.time()
    missing = list(keywords)
    results = []
    attempt = 0
    while missing:
        nodes = runner._get_all_nodes()   # 带 bounds,供空间邻近匹配
        pending = []
        for kw in missing:
            val = extract_value(runner, nodes, kw)
            if val is None:
                pending.append(kw)
            else:
                runner.store[kw] = val
                results.append(f"{kw}={val}")
        missing = pending
        if not missing or time.time() - start >= GRAB_TIMEOUT:
            break
        attempt += 1
        runner._poll_sleep(attempt)
    if missing:
        raise RuntimeError(f"grab 在 {GRAB_TIMEOUT}s 内未找到 {missing} 的数据")
    runner._last_compare_msg = "主页: " + ", ".join(results)


GRAB_TIMEOUT = 10    # 面板展开动画一般 <1s,10s 足够宽容
MATCH_TIMEOUT = 8    # 记录页转场动画/数据加载的轮询上限


def do_match(runner, keywords):
    """对比当前页面数据与 grab 存储的数据(数值允许 ±1 浮动)

    点开记录详情后面板有转场动画 —— 和 grab 同理,轮询到数据一致或超时,
    不能拿转场中的错位几何(比如把状态卡的 99% 当面积)一击定胜负。
    """
    if isinstance(keywords, str):
        keywords = split_texts(keywords)
    start = time.time()
    mismatches, match_results = [], []
    attempt = 0
    while True:
        nodes = runner._get_all_nodes()   # 带 bounds,供空间邻近匹配
        mismatches, match_results = [], []
        for kw in keywords:
            stored = runner.store.get(kw, "")
            found = extract_value(runner, nodes, kw)
            # 预约时间特殊处理: 从层级中取第一个非状态栏(y>80)的 HH:MM
            if found is None and kw == "预约时间":
                xml = runner.d.dump_hierarchy()
                items = re.findall(
                    r'text="(?:\d{4}-\d{2}-\d{2} )?(\d{1,2}:\d{2})"[^>]*bounds="\[\d+,(\d+)\]', xml)
                times = [t for t, y in items if int(y) > 80]
                if times:
                    found = times[0]
            match_results.append(f"{kw}={found}")
            if found is None:
                # grab 在主页抓到过、记录页却找不到 → 判失败,防止数据缺失假通过
                mismatches.append(f"主页={stored}, 记录页未找到'{kw}'")
            elif found != stored:
                try:
                    if abs(float(stored) - float(found)) <= 1:
                        continue
                except ValueError:
                    pass
                mismatches.append(f"主页={stored}, 记录={found}")
        if not mismatches or time.time() - start >= MATCH_TIMEOUT:
            break
        attempt += 1
        runner._poll_sleep(attempt)
    if mismatches:
        runner._last_compare_msg = ("记录: " + ", ".join(match_results)
                                    + " | " + "; ".join(mismatches))
        raise AssertionError(f"数据不一致: {'; '.join(mismatches)}")
    runner._last_compare_msg = "记录: " + ", ".join(match_results) + " | 一致"


def extract_value(runner, nodes, kw):
    """从页面文本中提取关键字的对应数值
    面积优先找'数字+㎡',时间优先找'数字+min'
    支持模糊匹配: 面积 可匹配 清扫面积, 时间 可匹配 用时

    nodes 是 _get_all_nodes() 的 (text, bounds) 列表。有 bounds 时优先用空间邻近
    —— XML 文档顺序不等于视觉顺序,下标法在列顺序变化时会静默取到隔壁指标的
    数字,而且取到的值看起来完全正常,很难发现。

    页面上可能同时存在多处同名标签(主页数据面板 / 记录列表条目'面积2㎡' /
    记录详情),空间法逐个尝试,某个失败不影响后面的 —— 2026-09-16 记录页
    实测:第一个标签因转场动画错位,数据其实在详情页的另一个'面积'节点旁。
    """
    texts = [t for t, _ in nodes]
    # 面积/时间 对应的单位关键词
    unit_map = {
        '面积': '㎡', '清扫面积': '㎡', '时间': 'min', '用时': 'min',
        '清扫时间': 'min',
    }
    prefer_unit = unit_map.get(kw, '')
    # 扩展匹配: 也尝试匹配包含该关键词的变体
    match_words = [kw]
    if kw == '面积': match_words.extend(['清扫面积'])
    if kw == '时间': match_words.extend(['用时', '清扫时间'])

    first_idx = None
    for i, (t, box) in enumerate(nodes):
        if kw not in t and not any(mw in t for mw in match_words):
            continue
        if first_idx is None:
            first_idx = i
        # 空间邻近优先(预约时间的 HH:MM 特例仍走下标)
        if box is not None and kw != '预约时间':
            found = value_near_label(nodes, i, prefer_unit)
            if found is not None:
                return found
            # 该节点空间未命中 —— 页面可能还有别的同名标签,继续试下一个
            continue
        # 无 bounds(或预约时间):空间法无从下手,直接下标回退
        return _index_fallback(texts, nodes, i, prefer_unit, kw)
    # 所有匹配节点的空间法都未命中:
    if first_idx is None:
        return None
    #   · 数字节点带 bounds —— 版面可判定,同列确实没有数值,不回退下标法
    #     (回退只会从隔壁列捞一个看起来完全正常的数字回来,静默错值更糟)
    #   · 都没带(层级里解析不出 bounds)—— 空间法本就无从下手,退回下标法
    #     总比误报"未找到数据"强
    if any(b is not None and re.match(r'^\d', t.strip()) for t, b in nodes):
        return None
    return _index_fallback(texts, nodes, first_idx, prefer_unit, kw)


def _index_fallback(texts, nodes, i, prefer_unit, kw):
    """按下标邻近取值:预约时间先找 HH:MM;再找"数字+单位"配对;最后最近纯数字"""
    if kw == '预约时间':
        for offset in range(-3, 4):
            j = i + offset
            if 0 <= j < len(texts):
                # search 而非 match: 预约时间常带日期前缀
                # ("2026-09-15 08:30"),整串匹配会一个都取不到,
                # 然后掉进下面的纯数字回退,把年份 "2026" 当时间返回。
                m = re.search(r'(\d{1,2}:\d{2})', texts[j].strip())
                if m:
                    return m.group(1)
    # 1. 优先找"数字+单位"配对(如[1]=8, [2]=㎡)
    for offset in range(-3, 4):
        j = i + offset
        if 0 <= j < len(texts) and texts[j].strip():
            val = texts[j].strip()
            if ":" in val or "%" in val:
                continue                    # 时间/百分比不是数值候选
            m = re.match(r'^(\d+\.?\d*)', val)
            if not m:
                continue
            # 检查下一项是否是匹配的单位
            nj = j + 1
            if 0 <= nj < len(texts) and prefer_unit and prefer_unit in texts[nj]:
                return m.group(1)
    # 2. 回退:找最近纯数字
    candidates = []
    for offset in range(-2, 3):
        j = i + offset
        if 0 <= j < len(texts) and texts[j].strip():
            if ":" in texts[j] or "%" in texts[j]:
                continue                    # 时间/百分比不是数值候选
            m = re.match(r'^(\d+\.?\d*)', texts[j].strip())
            if m:
                candidates.append((abs(offset), m.group(1)))
    if candidates:
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1]
    return None


def value_near_label(nodes, idx, prefer_unit):
    """取标签正上方同列最近的数字,返回字符串;没有可信候选返回 None

    主页清扫数据是"数值在上、标签在下"的两列版面(8㎡ / 清扫面积),所以按
    垂直邻近 + 同列判定。同列容差取标签自身宽度(至少 60px),避免把隔壁列
    的数字算进来。带正确单位的候选优先,其次比垂直距离。
    """
    _, label_box = nodes[idx]
    lx1, ly1, lx2, ly2 = label_box
    lcx = (lx1 + lx2) / 2
    col_tol = max(60, lx2 - lx1)

    cands = []
    for j, (t, box) in enumerate(nodes):
        if j == idx or box is None:
            continue
        m = re.match(r'^(\d+\.?\d*)', t.strip())
        if not m:
            continue
        # 时间(17:47 / 2026-09-16)与百分比(99%)永远不是面积/时间的数值
        # —— 记录列表条目上方是日期行,状态卡是电量,混进来就是静默错值
        if ":" in t or "%" in t:
            continue
        x1, y1, x2, y2 = box
        if abs((x1 + x2) / 2 - lcx) > col_tol:
            continue                      # 不在标签所在列
        vgap = ly1 - y2                   # 标签上沿 - 候选下沿
        if vgap < -10:
            continue                      # 候选不在标签上方
        ok = unit_attached(nodes, j, prefer_unit)
        cands.append((0 if ok else 1, abs(vgap), m.group(1)))
    if not cands:
        return None
    cands.sort(key=lambda c: (c[0], c[1]))
    return cands[0][2]


def unit_attached(nodes, j, prefer_unit):
    """数字节点同一行右侧是否紧跟期望单位(如 8 ㎡)

    单位可能是上标(㎡),竖直中心和数字不完全对齐,所以行判定容差放宽到整行高。
    """
    if not prefer_unit:
        return False
    _, (x1, y1, x2, y2) = nodes[j]
    cy = (y1 + y2) / 2
    h = max(1, y2 - y1)
    for k, (t, box) in enumerate(nodes):
        if k == j or box is None or prefer_unit not in t:
            continue
        ox1, oy1, ox2, oy2 = box
        if abs((oy1 + oy2) / 2 - cy) > h:
            continue                      # 不在同一行
        gap = ox1 - x2
        if -5 <= gap <= h * 4:            # 紧邻右侧
            return True
    return False
