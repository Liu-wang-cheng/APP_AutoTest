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
MATCH_TIMEOUT = 8   # 记录页转场动画/数据加载的轮询上限


#: 关键字里含这些词 → 不是「本次清扫指标」, 走原来的"关键字 + 邻近数字"匹配。
#: 例: 预约时间/预约日期 —— 它们不是本次清扫的面积或时长。
_NON_METRIC_KW = ("预约",)


def metric_kind_of(kw):
    """这个关键字该按哪个指标识别: 面积类→"area", 时间类→"time", 其它→None"""
    kw = str(kw or "")
    if any(x in kw for x in _NON_METRIC_KW):
        return None
    if "面积" in kw:
        return "area"
    if "时间" in kw or "用时" in kw or "时长" in kw:
        return "time"
    return None


def _grab_plan(value, time_kw):
    """把步骤字段摊成 [(存储键, kind|None, 关键字提示, 老式关键字)]。

    两种写法:
      · 新界面: grab: true + grab_area: <关键字可空> + grab_time: <关键字可空>
        —— 面积/时间各一个输入框, 留空 = 自动识别(见 extract_metric)
      · 老写法(兼容): grab: 面积,时间 / grab: 预约时间
        —— 含"面积/时间"的字按新方式识别(顺带修掉"抓到累计值"的老毛病),
           其它字(如 预约时间)原样走老的关键字匹配
    """
    items = []
    # bool 值 = 新写法(grab: true)。★ 老用例 `grab: 面积,时间` 被 GUI 保存过之后会被
    # 规范化成 `grab: true`(schema.FIXED_BOOL), 所以 bool 必须按新写法处理(两个框都空)
    if time_kw is not None or isinstance(value, bool):
        area_hint = "" if isinstance(value, bool) else str(value or "").strip()
        items.append(("面积", "area", area_hint, None))
        items.append(("时间", "time", str(time_kw or "").strip(), None))
        return items
    kws = split_texts(value) if isinstance(value, str) else list(value or [])
    for kw in kws:
        kind = metric_kind_of(kw)
        if not kind:
            items.append((kw, None, "", kw))
            continue
        key = "面积" if kind == "area" else "时间"
        if all(it[0] != key for it in items):      # 同一指标只抓一次
            items.append((key, kind, "", None))
    return items


def _lookup(runner, nodes, item):
    """按计划取一个值: 指标走 extract_metric(排除累计), 其它走老的关键字匹配"""
    _key, kind, hint, legacy = item
    if kind:
        return extract_metric(nodes, kind, hint)
    return extract_value(runner, nodes, legacy)


def do_grab(runner, value="", time_kw=None):
    """抓「本次清扫面积 / 时间」到 runner.store(键名仍是 面积 / 时间, 供 match 比对)。

    · 面积: 认 m²/㎡, **自动排除总面积/累计**(真机"打扫报告"页上两者同屏)
    · 时间: 认 分/分钟/小时(复合时长如「1 小时 5 分钟」换算成 65 分钟), 排除总时间
    · 关键字框**留空 = 自动识别**; 填了就只认标签含它的候选
    · 页面上有多个候选(如"真空吸尘器 20m²"与"拖布 18m²")时**报错并列出候选**,
      让人填关键字指明 —— 猜错不会报错, 只会让后面的比对悄悄比错数据

    click 展开面板 + grab 常写在同一步: 点击后面板有滑入动画, 立刻 dump 大概率抓到
    渲染前的空面板 —— 所以这里轮询等待, 而不是一击不中就报错(2026-09-16 实测踩坑)。
    """
    items = _grab_plan(value, time_kw)
    if not items:
        raise RuntimeError("grab 步骤没有可抓的内容(面积/时间两个框都是空的?)")
    start = time.time()
    missing, results = list(items), []
    attempt = 0
    while missing:
        nodes = runner._get_all_nodes()   # 带 bounds,供空间邻近匹配
        pending = []
        for item in missing:
            try:
                val = _lookup(runner, nodes, item)
            except ValueError as e:
                # 页面还没渲染好 / 有多个候选: 再等等看(候选歧义会一直等不到, 最后原样抛出)
                pending.append((item, e))
                continue
            runner.store[item[0]] = val
            results.append(f"{item[0]}={val}")
        missing = [it for it, _e in pending]
        if not missing or time.time() - start >= GRAB_TIMEOUT:
            if pending:
                raise RuntimeError(f"grab 在 {GRAB_TIMEOUT}s 内没抓到 "
                                   f"{[it[0] for it, _e in pending]}: {pending[0][1]}")
            break
        attempt += 1
        runner._poll_sleep(attempt)
    runner._last_compare_msg = "主页: " + ", ".join(results)


def do_match(runner, value="", time_kw=None):
    """对比当前页面数据与 grab 存储的数据(数值允许 ±1 浮动)

    点开记录详情后面板有转场动画 —— 和 grab 同理, 轮询到数据一致或超时,
    不能拿转场中的错位几何(比如把状态卡的 99% 当面积)一击定胜负。
    """
    items = _grab_plan(value, time_kw)
    start = time.time()
    mismatches, match_results = [], []
    attempt = 0
    while True:
        nodes = runner._get_all_nodes()   # 带 bounds,供空间邻近匹配
        mismatches, match_results = [], []
        for item in items:
            key = item[0]
            stored = runner.store.get(key, "")
            try:
                found = _lookup(runner, nodes, item)
            except ValueError as e:
                match_results.append(f"{key}=取不到({e})")
                mismatches.append(f"记录页取不到'{key}': {e}")
                continue
            # 预约时间特殊处理: 从层级中取第一个非状态栏(y>80)的 HH:MM
            if found is None and key == "预约时间":
                xml = runner.d.dump_hierarchy()
                items_t = re.findall(
                    r'text="(?:\d{4}-\d{2}-\d{2} )?(\d{1,2}:\d{2})"[^>]*bounds="\[\d+,(\d+)\]',
                    xml)
                times = [t for t, y in items_t if int(y) > 80]
                if times:
                    found = times[0]
            match_results.append(f"{key}={found}")
            if found is None:
                # grab 在主页抓到过、记录页却找不到 → 判失败,防止数据缺失假通过
                mismatches.append(f"主页={stored}, 记录页未找到'{key}'")
            elif stored == "":
                mismatches.append(f"'{key}'没有抓取过(主页为空), 记录页={found}")
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


# ── 「本次」清扫指标的识别(面积 / 时间) ──────────────────────────────
# ★ 为什么单独做一套(2026-10-10 真机实测「打扫报告」页):
#   页面上**同时**有两组数据:
#       总面积 1,300 m², 总时间 29 小时 57 分钟, 总次数 47 次     ← 累计/总计
#       真空吸尘器 20m² / 拖布 18m² / 时间 30 分                  ← 本次清扫
#   而原来的 grab 是"关键字子串匹配 + 标签正上方同列找数字":
#     · `面积` 会命中「总面积」→ 文档顺序里累计在前就抓累计值(合成版式实测抓到 128,
#       本该 25; 时间同理 36 vs 12), 数字看着正常, 静默错值;
#     · 单位只认 `㎡`/`min`, 真机上是 `m²`/`分` → 单位判据完全失效;
#     · 版式只认"数值在上、标签在下"(主页), 报告页底部是**标签在上、数值在下** → 取不到。
#   所以这里按【指标】识别: 认单位、排除累计词、标签上下两向都找、多候选不猜。

#: 累计/统计类词: 抓「本次」指标时, 标签或所在行含这些词一律排除
_TOTAL_WORDS = ("总", "累计", "总计", "历史", "所有", "平均", "统计", "上次",
                "本月", "本周", "共")

#: 单位 → (指标, 换算成标准单位的倍数)。面积标准单位 m², 时间标准单位 分钟
_UNITS = (
    ("m²", "area", 1.0), ("㎡", "area", 1.0), ("m2", "area", 1.0),
    ("平方米", "area", 1.0), ("平米", "area", 1.0),
    ("min", "time", 1.0), ("分钟", "time", 1.0), ("分", "time", 1.0),
    ("小时", "time", 60.0), ("时", "time", 60.0), ("h", "time", 60.0),
)
_NUM_HEAD = re.compile(r"^\s*(\d[\d,]*(?:\.\d+)?)\s*(.*)$")


def _to_float(text):
    """'1,300' / '25' → float(去千分位); 不是纯数字返回 None"""
    m = re.match(r"^\s*(\d[\d,]*(?:\.\d+)?)\s*$", str(text or ""))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _row_of(nodes, box, tol=28):
    """与 box 同一行的文本(垂直中心相差不超过 tol)"""
    cy = (box[1] + box[3]) / 2
    return " ".join(t.strip() for t, b in nodes
                    if b and t.strip() and abs((b[1] + b[3]) / 2 - cy) <= tol)


def _is_total(text):
    return any(w in text for w in _TOTAL_WORDS)


def _looks_numeric(text):
    """像"数值(可带单位)"的短文本 —— 这种不能当标签"""
    t = str(text or "").strip()
    return bool(_NUM_HEAD.match(t)) or bool(re.fullmatch(r"[\d,\.\s]+\S{0,3}", t))


def _nearest_label(nodes, idx, max_gap=300):
    """数字节点最近的**非数值文本**节点(向上、向下都找); 找不到返回 None。

    ★ 两个方向都要找: 主页数据面板是"数值在上、标签在下", 而真机「打扫报告」页底部是
      "标签在上、数值在下"(真空吸尘器/拖布/时间 并排三列) —— 只看一个方向就会漏。
      同列用水平中心差判定, 免得把隔壁列的数字配到本列标签上。
    """
    _, box = nodes[idx]
    cx = (box[0] + box[2]) / 2
    tol = max(80, (box[2] - box[0]) / 2 + 40)
    best, best_gap = None, None
    for j, (t, b) in enumerate(nodes):
        if j == idx or not b or not t.strip() or _looks_numeric(t):
            continue
        if abs((b[0] + b[2]) / 2 - cx) > tol:
            continue
        gap = abs((b[1] - box[3]) if b[1] >= box[3] else (box[1] - b[3]))
        if gap > max_gap:
            continue
        if best_gap is None or gap < best_gap:
            best, best_gap = j, gap
    return best


def _time_minutes(text):
    """"30 分"→30, "1 小时 5 分钟"→65, "2h"→120; 认不出返回 None

    ★ 复合时长必须换算: 「1 小时 5 分钟」按 1 分钟算就是静默错值(比对会假失败)。
    """
    nums = re.findall(r"(\d[\d,]*(?:\.\d+)?)", text)
    if not nums:
        return None
    vals = [float(n.replace(",", "")) for n in nums]
    has_h = any(u in text for u in ("小时", "时", "h"))
    has_m = any(u in text for u in ("分", "min"))
    if has_h and has_m and len(vals) >= 2:
        return vals[0] * 60 + vals[1]
    if has_h:
        return vals[0] * 60
    return vals[0]


def _unit_near(nodes, idx, box):
    """数字节点**旁边**的单位(同一行、水平最近的那个) → (kind, factor)。

    ★ 为什么不能拿"整行文本"去认单位: 主页数据面板是"面积 ㎡ | 时间 min"并排,
      整行里两个单位都在 —— 用 _UNITS 的顺序去撞, 第二个指标永远被判成第一个
      (实测: 时间被当成面积, 于是时间取不到值)。单位总是紧挨着自己那个数字。
    """
    cy = (box[1] + box[3]) / 2
    best, best_d = None, None
    for j, (t, b) in enumerate(nodes):
        if j == idx or not b or not t.strip():
            continue
        if abs((b[1] + b[3]) / 2 - cy) > 28:          # 不在同一行
            continue
        k, f = _unit_kind(t)
        if k is None:
            continue
        d = abs((b[0] - box[2]) if b[0] >= box[2] else (box[0] - b[2]))
        if best_d is None or d < best_d:
            best, best_d = (k, f), d
    return best or (None, 1.0)


def _unit_kind(hay):
    """从文本里认出单位 → (kind, factor); 认不出 (None, 1.0)"""
    for u, k, f in _UNITS:
        if u in hay:
            return k, f
    return None, 1.0


def _metric_candidates(nodes, kind):
    """页面上「本次」该指标的候选: [(值(已换算成标准单位), 标签, 所在行)]"""
    out = []
    for i, (raw, box) in enumerate(nodes):
        if not box:
            continue
        text = str(raw or "").strip()
        m = _NUM_HEAD.match(text)
        if not m:
            continue                      # 不是"数字开头"的节点(累计那行以"总"开头, 天然排除)
        tail = m.group(2).strip()
        if ":" in text or "%" in text:
            continue                      # 时间点(19:44)/百分比(99%)不是指标值
        row = _row_of(nodes, box)
        # ★ 先认**节点自带**的单位("30 分"/"20m²"), 认不到才退到整行找(主页面板里
        #   单位是独立节点)。反过来会让同一行里的 m² 把"30 分"判成面积 —— 实测踩到。
        unit, factor = _unit_kind(tail)           # ① 本节点自带("20m²"/"30 分")
        if unit is None:
            unit, factor = _unit_near(nodes, i, box)   # ② 同行最近的单位节点
        if unit is None:
            unit, factor = _unit_kind(row)         # ③ 整行兜底(单指标页面)
        if unit != kind:
            continue
        lab_idx = _nearest_label(nodes, i)
        label = nodes[lab_idx][0].strip() if lab_idx is not None else ""
        if _is_total(label) or _is_total(row):
            continue                      # ★ 累计/总计一律不要
        num = float(m.group(1).replace(",", ""))
        if kind == "time":
            # ★ 数值**只从本节点取**(复合时长"1 小时 5 分钟"也都在本节点文本里);
            #   从整行抠数字会把隔壁列的分钟数抓过来 —— 实测踩到: "12 min" 变成 36。
            #   单位可以借整行(主页面板里单位是独立节点)。
            val = (_time_minutes(text)
                   if any(u in text for u in ("分", "小时", "时", "min", "h")) else num)
        else:
            val = num * factor
        if val is None:
            continue
        out.append((val, label, row))
    seen, uniq = set(), []
    for v, lab, row in out:
        if (v, lab) in seen:
            continue
        seen.add((v, lab))
        uniq.append((v, lab, row))
    return uniq


def extract_metric(nodes, kind, want=""):
    """取「本次」清扫面积/时间的数值(字符串); 取不到或有歧义就抛 ValueError。

    kind: "area"(m²/㎡ → m²) / "time"(分/分钟/小时 → 统一换算成分钟)
    want: 用户在步骤里填的关键字(可空)。给了就只认标签/行里含它的候选。

    ★ 有歧义就**报错**, 不猜: 抓错不会报错, 只会让后面的比对悄悄比错数据(真机上
      报告页就有"真空吸尘器 20m²"和"拖布 18m²"两个面积, 取哪个是业务语义, 得由人定)。
    """
    name = "面积" if kind == "area" else "时间"
    cands = _metric_candidates(nodes, kind)
    if want:
        cands = [c for c in cands if want in c[1] or want in c[2]]
    if not cands:
        page = " | ".join(t.strip() for t, _b in nodes if t.strip())[:180]
        raise ValueError(f"页面上没找到「本次{name}」—— 可能只有累计值、或页面还没渲染好。"
                         f"当前可见文本: {page}")
    if len(cands) > 1:
        desc = ", ".join(f"{v:g}({lab or '无标签'})" for v, lab, _ in cands)
        raise ValueError(f"页面上有多个{name}候选: {desc} —— 请在步骤的{name}框里填关键字"
                         f"(如 真空吸尘器)指明要哪一个; 猜错不会报错, 只会悄悄比错数据")
    return f"{cands[0][0]:g}"


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
