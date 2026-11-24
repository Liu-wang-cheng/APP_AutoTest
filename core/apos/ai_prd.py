# -*- coding: utf-8 -*-
"""用 AI 解析 PRD：提取事件对应的语音序号、灯显、故障码。

PRD 各项目内容/格式都可能不同，所以不在代码里写死映射，而是：

1. `extract_text(path)` 把 xlsx/docx/pdf/txt/md 读成纯文本；
2. 把「本项目可模拟的事件列表 + 已知灯效词表」和 PRD 文本一起发给
   OpenAI 兼容的中转站接口（运行时配置 base_url / api_key / model）；
3. 解析模型返回的 JSON，得到每个事件的期望语音/灯显/故障码。

解析结果由界面展示、可编辑，并按它验证故障模拟（用户 2026-11-12）。
"""

import json
import re

DEFAULT_TIMEOUT = 180
MAX_PRD_CHARS = 90000
#: 标记为 1M 上下文的模型: PRD 可以放得更宽
MAX_PRD_CHARS_1M = 900000
#: 默认最大输出 tokens（推理模型会先消耗大量 reasoning，8192 很容易被截断）
DEFAULT_MAX_TOKENS = 32768
#: 默认中转站地址（用户 2026-11-12 指定）
DEFAULT_BASE_URL = "http://192.168.160.145:8081"
#: 内置 Claude 解析模型（仍走中转站，Anthropic Messages 协议）
BUILTIN_CLAUDE_MODEL = "claude-sonnet-4.6"


def is_1m_model(model):
    """模型名里带 1m 标记(如 xxx-1m / xxx_1m / 1m-context) → 1M 上下文。"""
    s = str(model or "").strip().lower()
    return bool(re.search(r"(?:^|[-_/ .])1m(?:$|[-_/ .])", s))



def _read_xlsx(path):
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True)
    lines = []
    for ws in wb.worksheets:
        lines.append(f"### Sheet: {ws.title}")
        for row in ws.iter_rows(values_only=True):
            cells = [str(x).strip() for x in row
                     if x is not None and str(x).strip()]
            if cells:
                lines.append(" | ".join(cells))
    return "\n".join(lines)


def _read_docx(path):
    import docx
    doc = docx.Document(path)
    lines = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                lines.append(" | ".join(cells))
    return "\n".join(lines)


def _read_pdf(path):
    from pypdf import PdfReader
    reader = PdfReader(path)
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def extract_text(path, max_chars=MAX_PRD_CHARS):
    """把 PRD 文件读成纯文本；不支持的格式或空文档抛 ValueError。"""
    low = str(path).lower()
    if low.endswith((".xlsx", ".xlsm")):
        text = _read_xlsx(path)
    elif low.endswith(".docx"):
        text = _read_docx(path)
    elif low.endswith(".pdf"):
        text = _read_pdf(path)
    elif low.endswith(".doc"):
        raise ValueError("不支持旧版 .doc，请另存为 .docx")
    elif low.endswith((".txt", ".md", ".csv", ".json")):
        raw = open(path, "rb").read()
        for enc in ("utf-8", "gbk", "utf-16"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = raw.decode("utf-8", "replace")
    else:
        raise ValueError(f"不支持的 PRD 格式：{path}")
    text = (text or "").strip()
    if not text:
        raise ValueError("PRD 里没有提取到文字（扫描版 PDF 需先转文字）")
    if len(text) > max_chars:
        text = text[:max_chars] + "\n……（内容过长，已截断）"
    return text


_CANCEL_KEYS = ("取消播报", "暂不提供", "不播报", "暂不加语音", "不加语音",
                "无此功能")
_REUSE_RE = re.compile(r"复用\s*\[?\s*Q?0*(\d+)")

#: PRD 明确写「不需要使用这条语音」的标记(LG Comment 的 Not apply this
#: voice)。这类语音不要计入语音包检查的必需文件(用户 2026-11-17)。
_VOICE_SKIP_MARKERS = (
    "not apply this voice",
    "not apply the voice",
    "do not use this voice",
    "no need to play this voice",
    "不需要播放",
    "无需播放",
    "无需使用",
    "不再使用",
    "不需要此语音",
)


def voice_not_needed(*texts):
    """PRD 是否明确说这条语音不需要使用(与划线取消不同)。

    LG 新版 comment 里常写成条件句「If there is no such detection, We will
    not apply this voice」——这不是无条件取消, 只有非条件句才算(用户 2026-11-17)。
    """
    # 先把换行并成空格再按句切分: "If ...\nWe will not apply this voice"
    # 是同一条件句, 不能因为换行把 if 分到上一行而误判为取消(用户 2026-11-17)
    flat = " ".join(str(t or "") for t in texts).lower()
    flat = re.sub(r"\s+", " ", flat)
    sentences = [s for s in re.split(r"[.。!?；;]", flat) if s.strip()]
    for sentence in sentences:
        if not any(marker in sentence for marker in _VOICE_SKIP_MARKERS):
            continue
        if not re.search(r"\bif\b|如果|若", sentence):
            return True
    return False


_PLACEHOLDER_WORDS = ("无此功能", "暂不提供", "不播报", "取消播报",
                     "暂不加语音", "复用", "同上", "待定", "仅旋律",
                     "僅旋律", "melody only")


def voice_display_text(condition="", chinese="", text=""):
    """PRD 语音的可读名称: 中文列是【无此功能】等占位时改用触发条件。"""
    c = str(chinese or "").strip()
    if c and not any(w in c.lower() for w in _PLACEHOLDER_WORDS):
        return c
    return str(condition or "").strip() or str(text or "").strip() or c


def _cell_text(value):
    return str(value or "").strip()


def _row_reason(struck, texts):
    if struck:
        return "划线取消"
    joined = " ".join(texts)
    for key in _CANCEL_KEYS:
        if key in joined:
            return key
    return ""


def _voice_rows_from_xlsx(path):
    import openpyxl
    wb = openpyxl.load_workbook(path)
    ws = wb["主机语音定义"] if "主机语音定义" in wb.sheetnames else None
    rows = []
    if ws is not None:
        for row in ws.iter_rows(min_row=2):
            values = [c.value for c in row]
            texts = [_cell_text(v) for v in values[:8]]
            qid_text = texts[0].upper()
            if not qid_text.startswith("Q"):
                continue
            m = re.search(r"\d+", qid_text)
            if not m:
                continue
            struck = any(bool(getattr(c.font, "strike", False))
                         for c in row[:8])
            rows.append({"qid": int(m.group(0)), "texts": texts,
                         "struck": struck,
                         # PRD 表头就是 Q 号 → 显示时保留 Q(HS4)
                         "label": f"Q{int(m.group(0)):03d}"})
    if rows:
        return rows
    # ★ 其它项目 PRD 的语音表格式不同(LG MR: 语音-故障-推送, 列为
    #   触发场景说明/语音序号/中文…), 按表头识别, 不能直接放弃解析
    #   (用户 2026-11-17: 换项目后 354 个候选事件全丢给 AI, 解析不贴合 PRD)。
    return _voice_rows_generic(path)


def _voice_rows_generic(path):
    """按表头找语音表: 含「语音序号/语音编号」的表格(支持合并分类列)."""
    import openpyxl
    # ★ 需要读字体判断划线取消, 不能用 read_only(values_only)
    wb = openpyxl.load_workbook(path, data_only=True)
    for ws in wb.worksheets:
        rows = list(ws.iter_rows())
        for hi, header in enumerate(rows[:12]):
            cells = [_cell_text(getattr(c, "value", None)).replace(
                "\n", "") for c in header]
            compact = [c.replace(" ", "").lower() for c in cells]
            qcol = next((i for i, c in enumerate(cells)
                         if "语音序号" in c or "语音编号" in c
                         or "voiceindex" in compact[i]), None)
            if qcol is None:
                continue
            ccol = next((i for i, c in enumerate(cells)
                         if "触发场景" in c or "触发条件" in c
                         or "语音播报场景" in c), None)
            if ccol is None:
                continue
            cncol = next((i for i, c in enumerate(cells)
                          if (("中文" in c and not any(k in c for k in (
                              "英文", "english", "韩", "korea",
                              "台湾", "taiwan")))
                              or "chinese" in compact[i])), None)
            encol = next((i for i, c in enumerate(cells)
                          if "英文" in c), None)
            krcol = next((i for i, c in enumerate(cells)
                          if "korea" in compact[i]
                          or "韩" in c), None)
            twcol = next((i for i, c in enumerate(cells)
                          if "taiwan" in compact[i]
                          or "台湾" in c), None)
            comcol = next((i for i, c in enumerate(cells)
                           if "comment" in compact[i] or "备注" in c), None)
            # Ref Model / LITE Voice Index 写 Not apply → 这条语音对本项目不适用
            refcol = next((i for i, c in enumerate(cells)
                           if "refmodel" in compact[i]), None)
            litecol = next((i for i, c in enumerate(cells)
                            if "litevoiceindex" in compact[i]), None)
            out = []
            kind = ""
            for row in rows[hi + 1:]:
                vals = [_cell_text(getattr(c, "value", None)) for c in row]
                struck = any(bool(getattr(getattr(c, "font", None),
                                           "strike", False)) for c in row)
                q = vals[qcol] if qcol < len(vals) else ""
                m = re.search(r"\d+", q)
                if not m:
                    continue
                chinese = (vals[cncol]
                           if cncol is not None and cncol < len(vals) else "")
                if chinese.strip() in ("/", "-", "—"):
                    # 语音内容明确写 "/" 才是仅推送/无语音; 中文留空可能只是
                    # 没翻译, 语音号/音效仍然存在, 不能跳过(用户 2026-11-17)
                    continue
                k = vals[0] if vals else ""
                if k:
                    kind = k        # 分类列是合并单元格, 向下沿用
                english = (vals[encol]
                           if encol is not None and encol < len(vals) else "")
                korea = (vals[krcol]
                         if krcol is not None and krcol < len(vals) else "")
                taiwan = (vals[twcol]
                          if twcol is not None and twcol < len(vals) else "")
                comment = (vals[comcol]
                           if comcol is not None and comcol < len(vals)
                           else "")
                condition = vals[ccol] if ccol < len(vals) else ""
                # Ref Model / LITE Voice Index 写 Not apply → 本项目不适用,
                # 不进解析/测试/语音包检查(用户 2026-11-17: 0822/0823)
                not_applied = any(
                    "not apply" in (vals[i].lower() if i is not None
                                    and i < len(vals) else "")
                    for i in (refcol, litecol))
                # 条件句(If ... not apply this voice)不算不适用: LG 只是说
                # 没有对应检测时不加语音, 例如 0830/0835 基站温度(用户 2026-11-17)
                if not_applied:
                    lines = [ln for ln in re.split(r"[\n。.]", comment)
                             if "not apply" in ln.lower()]
                    if lines and all(
                            re.search(r"\bif\b|如果|若", ln, re.I)
                            for ln in lines):
                        not_applied = False
                # 语义文本兜底: 中文 > 英文 > 韩文 > 台湾中文
                fallback = (chinese or english or korea or taiwan)

                def meaningful(value):
                    """去掉 [LG_...] 标签和「Please add this voice」后的有效长度。"""
                    text = re.sub(r"\[[^\]]*\]", "", str(value or ""))
                    text = re.sub(
                        r"(please|need\s+to)\s+add\s+(this|the)\s+voice",
                        "", text, flags=re.I)
                    return len(text.strip(" .:：-")) >= 4

                # 场景列 TBD/空/占位, 或只是「Please add this voice」标签时,
                # 用翻译/ Comment 的真实含义描述场景(用户 2026-11-17)
                cond_bare = re.sub(r"\[[^\]]*\]", "", condition).strip(
                    " .:：-")
                cond_weak = (condition.strip().upper() in (
                    "TBD", "待定", "N/A", "NA", "") or len(cond_bare) < 4
                    or cond_bare.lower() in ("melody only", "melodyonly",
                                             "仅旋律", "僅旋律")
                    or not meaningful(cond_bare))
                if cond_weak:
                    # Comment 只有标签/「请加这条语音」时没有语义, 用翻译
                    condition = (
                        comment if meaningful(comment) else
                        (chinese or korea or taiwan or english
                         or fallback or comment))
                out.append({
                    "qid": int(m.group(0)),
                    "texts": [q, condition, fallback, chinese, english,
                              kind],
                    "english": english,
                    "korea": korea,
                    "taiwan": taiwan,
                    "comment": comment,
                    "struck": struck,
                    "voice_width": len(m.group(0)),
                    # PRD 没有 Q 号, 语音序号按原样显示(0001 / 0030)
                    "label": m.group(0),
                    "not_applied": not_applied,
                })
            if out:
                return out
    return None


def _voice_rows_from_docx(path):
    import docx
    doc = docx.Document(path)
    rows = []
    for table in doc.tables:
        header = " ".join(c.text for c in table.rows[0].cells)             if table.rows else ""
        if "语音编号" not in header and "触发条件" not in header:
            continue
        for tr in table.rows[1:]:
            cells = tr.cells
            texts = [c.text.strip() for c in cells[:8]]
            qid_text = texts[0].upper() if texts else ""
            if not qid_text.startswith("Q"):
                continue
            m = re.search(r"\d+", qid_text)
            if not m:
                continue
            struck = any(bool(run.font.strike) for c in cells[:8]
                         for p in c.paragraphs for run in p.runs)
            rows.append({"qid": int(m.group(0)), "texts": texts,
                         "struck": struck,
                         # PRD 表头就是 Q 号 → 显示时保留 Q(HS4)
                         "label": f"Q{int(m.group(0)):03d}"})
    return rows


def extract_voice_catalog(path):
    """解析 PRD 语音表：排除划线/取消播报/暂不提供，并把复用归一到最终语音。

    返回 {"active": {qid: {...}}, "excluded": {qid: reason},
          "map": {qid: final_qid}}；不支持的格式返回 None（由 AI 自行判断）。
    """
    low = str(path).lower()
    if low.endswith((".xlsx", ".xlsm")):
        rows = _voice_rows_from_xlsx(path)
    elif low.endswith(".docx"):
        rows = _voice_rows_from_docx(path)
    else:
        return None
    if not rows:
        return None
    groups = {}
    for r in rows:
        groups.setdefault(r["qid"], []).append(r)

    def active_rows(qid):
        return [r for r in groups.get(qid, [])
                if not _row_reason(r["struck"], r["texts"])
                and not r.get("not_applied")]

    def resolve(qid, seen=None):
        seen = set(seen or ())
        if qid in seen:
            return qid
        seen.add(qid)
        for r in active_rows(qid):
            joined = " ".join(r["texts"])
            m = _REUSE_RE.search(joined)
            if m and int(m.group(1)) != qid:
                return resolve(int(m.group(1)), seen)
        return qid

    width = max(
        [int(r.get("voice_width") or len(str(r.get("qid")))) for r in rows]
        or [3])
    catalog = {"active": {}, "excluded": {}, "map": {},
               "voice_width": width}
    for qid, rs in groups.items():
        active = [r for r in rs
                  if not _row_reason(r["struck"], r["texts"])
                  and not r.get("not_applied")]
        if not active:
            if any(r.get("not_applied") for r in rs):
                catalog["excluded"][qid] = "不适用"
            else:
                catalog["excluded"][qid] = _row_reason(
                    rs[0]["struck"], rs[0]["texts"]) or "不可用"
            continue
        rep = active[0]
        final = resolve(qid)
        catalog["active"][qid] = {
            "qid": f"Q{qid:03d}",
            # 报告/表格里显示的编号: PRD 本来带 Q 才是 Q 号(HS4),
            # 只有语音序号的项目(LG MR)按原样显示, 不加 Q(用户 2026-11-17)
            "label": str(rep.get("label") or f"Q{qid:03d}"),
            "kind": rep["texts"][5],
            "condition": rep["texts"][1],
            "text": rep["texts"][2],
            "chinese": rep["texts"][3],
            "english": rep.get("english", ""),
            "korea": rep.get("korea", ""),
            "taiwan": rep.get("taiwan", ""),
            "comment": rep.get("comment", ""),
            "skip_voice": voice_not_needed(
                rep.get("comment"), rep.get("texts") and " ".join(
                    str(x or "") for x in rep.get("texts") or [])),
            "final": final,
        }
        catalog["map"][qid] = final
    return catalog


_FAULT_WORDS = ("异常", "故障", "卡住", "堵转", "被困", "错误", "失败", "报错",
                "缺水", "水满", "已满", "未安装", "脏", "遮挡", "开路", "短路",
                "高温", "低温", "无法", "断开", "不足")
_REMIND_WORDS = ("提醒", "请", "注意", "完成", "超时", "低电", "电量", "升级",
                 "等待", "已无操作", "暂停", "勿扰", "静音", "联网", "配网",
                 "回充", "回洗", "到期", "请检查")
_STATION_WORDS = ("station_", "base_station", "集尘", "洗拖布", "烘干",
                  "清水箱", "污水箱", "清洁液", "尘袋", "清洁站",
                  "基站风机", "基站水泵", "基站清水泵", "基站污水泵",
                  "基站电磁阀")
#: 表格「类型」下拉的四类(用户 2026-11-12)
KIND_OPTIONS = ("故障", "提醒", "通用", "基站")
_KIND_ALIAS = {"错误": "故障", "故障": "故障", "提醒": "提醒", "提示": "提醒",
               "通用": "通用", "基站": "基站"}


def infer_kind(event, name="", voice_text=""):
    """按事件/文案推断类型：故障 / 提醒 / 通用 / 基站。"""
    e = str(event or "").lower()
    text = f"{event or ''} {name or ''} {voice_text or ''}".lower()
    if any(w in text for w in _STATION_WORDS):
        return "基站"
    if (e.startswith("error") or "trapped" in e
            or any(w in text for w in _FAULT_WORDS)
            or any(w in e for w in ("error", "stuck", "fault", "dirty"))):
        return "故障"
    if any(w in text for w in _REMIND_WORDS):
        return "提醒"
    return "通用"


def normalize_kind(kind, event="", name="", voice_text=""):
    """把 PRD/AI 的类型归一到四类之一。"""
    k = _KIND_ALIAS.get(str(kind or "").strip())
    if k:
        return k
    return infer_kind(event, name, voice_text)


def _voice_int(value):
    m = re.search(r"\d+", str(value or ""))
    return int(m.group(0)) if m else None


_REUSE_NOTE_RE = re.compile(
    r"复用\s*自?\s*Q?0*\d+"
    r"(?:\s*(?:→|->|=>)\s*(?:最终)?\s*Q?0*\d+|\s*最终\s*Q?0*\d+)?"
    r"(?:\s*[（(][^）)]*[）)])?")


def _reuse_note(note, final):
    """备注里的复用说明统一成「复用Q020」这种最终编号。"""
    text = _REUSE_NOTE_RE.sub("", str(note or ""))
    text = re.sub(r"[；;]{2,}", "；", text).strip("；;，, ")
    tag = f"复用Q{final:03d}" if final < 1000 else f"复用Q{final}"
    return f"{text}；{tag}".strip("；") if text else tag


def apply_voice_catalog(items, catalog):
    """把 AI 结果里的语音换成最终复用编号；被取消/暂不提供的条目丢弃。"""
    if not catalog:
        return items
    active = catalog.get("active") or {}
    excluded = catalog.get("excluded") or {}
    mapping = catalog.get("map") or {}
    out = []
    for it in items:
        q = _voice_int(it.get("voice"))
        if q is None:
            if it.get("voice"):
                out.append(it)
            continue
        if q in excluded and q not in active:
            continue                    # 划线/取消播报/暂不提供 → 不解析
        final = int(mapping.get(q, q))
        info = active.get(q) or active.get(final) or {}
        if not it.get("kind"):
            it["kind"] = (info.get("kind")
                          or (active.get(final) or {}).get("kind", ""))
        it["kind"] = normalize_kind(it.get("kind"), it.get("event"),
                                    it.get("name"), it.get("voice_text"))
        if final != q:
            it["note"] = _reuse_note(it.get("note"), final)
        if final < 1000:
            it["voice"] = f"{final:03d}"
        else:
            it["voice"] = str(final)
        out.append(it)
    return out


def normalize_rows_to_catalog(rows, catalog):
    """已保存的行按 PRD 语音归并: 丢弃 PRD 没有的语音, 同一最终语音保留一行。"""
    if not catalog:
        return rows
    mapping = {int(k): int(v) for k, v in (catalog.get("map") or {}).items()}
    out = []
    seen = {}
    for r in rows or []:
        q = _voice_int(r.get("voice"))
        if q is None or q not in mapping:
            continue
        final = mapping[q]
        if final not in seen:
            seen[final] = r
            out.append(r)
            continue
        keep = seen[final]
        alias = str(r.get("event") or "")
        if alias and alias != keep.get("event"):
            note = keep.get("note") or ""
            extra = f"同一语音事件：{alias}"
            if alias not in note:
                keep["note"] = (note + "；" + extra).strip("；")
        for key in ("led_home", "led_home_text", "led_start",
                    "led_start_text"):
            if not keep.get(key) and r.get(key):
                keep[key] = r[key]
    return out


def rows_from_catalog(items, catalog):
    """按 PRD 的 active 事件逐条生成表格行(PRD 数量为准)。

    ★ 2026-11-13 用户要求: PRD 里每个事件一行, 即使它复用另一条语音
      (如 Q187 复用 Q017)也要保留; 只有 extract_voice_catalog 已排除的
      「取消播报/划线取消/暂不提供」不出现。多行可以共用同一个语音序号,
      再分别与 AP 平台事件匹配。
    """
    if not catalog:
        return list(items or [])
    from core.apos import faults, prd_memory
    active = catalog.get("active") or {}
    mapping = catalog.get("map") or {}
    by_final = {}
    for it in items or []:
        q = _voice_int(it.get("voice"))
        if q is None:
            continue
        final = int(mapping.get(q, q))
        by_final.setdefault(final, []).append(it)
    # 语音号按 PRD 实际位数显示(HS4 三位 003 / ZY2L 四位 0003)
    width = int(catalog.get("voice_width") or 3)

    def fmt_voice(n):
        return f"{n:0{width}d}" if n < 1000 else str(n)

    rows = []
    for qid in sorted(active, key=lambda x: int(x)):
        info = active[qid] or {}
        try:
            qid_int = int(qid)
        except (TypeError, ValueError):
            continue
        final = int(info.get("final", qid_int))
        cands = by_final.get(final) or []
        # 报告/表格显示 PRD 实际编号: 有 Q 号才带 Q, 否则用语音序号(0030)
        qtext = str(info.get("label") or info.get("qid")
                    or f"Q{qid_int:03d}")
        use_q = qtext[:1].upper() == "Q"

        def fmt_label(n):
            return f"Q{n:03d}" if use_q else fmt_voice(n)

        qkey = prd_memory.voice_key(info.get("condition"),
                                    info.get("chinese"), info.get("text"))
        # 复用同一语音时, 优先用该 Q 号自己的记忆; 没有才用整条语音的候选
        by_qid = [it for it in cands
                  if str(it.get("qid") or "") == qtext]
        by_key = [it for it in cands
                  if str(it.get("voice_key") or "") == qkey]
        if by_qid:
            cands = by_qid
        elif by_key:
            cands = by_key
        # 同一语音匹配到多个事件 → 下拉让用户选(用户 2026-11-12)
        options = []
        seen_events = set()
        for c in cands:
            ev = str(c.get("event") or "")
            if not ev or ev in seen_events:
                continue
            seen_events.add(ev)
            options.append({
                "event": ev,
                "name": str(c.get("name") or ""),
                "fault_code": str(c.get("fault_code") or ""),
                "fault_push": str(c.get("fault_push") or ""),
                "kind": str(c.get("kind") or ""),
                "source": str(c.get("source") or "ai"),
            })
        pick = next((o for o in options
                     if faults.has_simulation(o.get("event"))),
                    options[0] if options else None)
        # 备注写清 PRD 里的含义 + 是否复用了别的语音(用户 2026-11-13)
        meaning = str(info.get("condition") or "").strip()
        if not meaning:
            meaning = voice_display_text(
                info.get("condition"), info.get("chinese"),
                info.get("text")) or qtext
        # Q 号单独一行, 短备注也保持两行结构, 表格里显示更一致(用户 2026-11-13)
        note = f"PRD：{meaning}\n（{qtext}）"
        if final != qid_int:
            note += f"；复用语音{fmt_label(final)}"
        notes = [note]
        if pick is None:
            notes.append("未找到对应事件")
        row = {
            "qid": qtext,
            "event": str((pick or {}).get("event") or ""),
            "name": voice_display_text(
                info.get("condition"), info.get("chinese"),
                info.get("text")) or str((pick or {}).get("name") or ""),
            "kind": normalize_kind(
                info.get("kind") or (pick or {}).get("kind"),
                (pick or {}).get("event"), info.get("condition"),
                info.get("chinese")),
            # 语音列显示 PRD 自己的 Q 号; 复用的最终语音单独保存, 判定时两者都接受
            "voice": fmt_voice(qid_int),
            "reuse_voice": (fmt_voice(final) if final != qid_int else ""),
            "voice_text": voice_display_text(
                info.get("condition"), info.get("chinese"),
                info.get("text")),
            "fault_code": str((pick or {}).get("fault_code") or ""),
            "fault_push": str((pick or {}).get("fault_push") or ""),
            "led_home": "", "led_home_text": "",
            "led_start": "", "led_start_text": "",
            "voice_key": prd_memory.voice_key(
                info.get("condition"), info.get("chinese"),
                info.get("text")),
            # PRD 明确「Not apply this voice」: 语音包检查不计入必需文件
            "comment": str(info.get("comment") or ""),
            "skip_voice": bool(info.get("skip_voice")),
            "source": str((pick or {}).get("source") or "ai"),
            "event_options": options,
            "note": "；".join(notes),
        }
        rows.append(row)
    return rows


def _voice_catalog_prompt(catalog):
    if not catalog:
        return ""
    lines = ["PRD 可用语音清单（已排除划线/取消播报/暂不提供，复用已给出最终发声音）："]
    for qid in sorted(catalog.get("active", {})):
        info = catalog["active"][qid]
        final = info["final"]
        reuse = (f"复用→最终 Q{final:03d}" if final != qid else
                 f"最终 Q{final:03d}")

        def clean(value):
            return str(value or "").replace("\n", " ").replace(
                "\r", " ").strip()

        vals = [clean(info.get(k)) for k in
                ("chinese", "english", "korea", "taiwan")]
        vals = [v for v in vals if v and v not in ("/", "-", "—")]
        if not vals or all(v.strip().lower() in ("melody only", "melodyonly")
                           or v.strip().upper().startswith("SND_")
                           for v in vals):
            vals = [clean(info.get("comment"))
                    or clean(info.get("text"))]
        desc = " / ".join(dict.fromkeys(vals))
        lines.append(f"{info['qid']} | {clean(info.get('kind'))} | "
                     f"{clean(info['condition'])} | {desc} | {reuse}")
    excluded = catalog.get("excluded") or {}
    if excluded:
        lines.append("以下语音已取消/暂不提供，禁止使用：")
        lines.append("、".join(f"Q{q:03d}（{r}）"
                               for q, r in sorted(excluded.items())))
    return "\n".join(lines)


def _led_vocab(led_names):
    """[(设备, identifier, 中文名)] → 给模型看的词表文本。"""
    rows = []
    for dev, name, effect_id, effect_cn in (led_names or []):
        rows.append(f"{dev} | {effect_id} | {effect_cn} | {name}")
    return "\n".join(rows)


def build_messages(prd_text, events, led_names=None, voice_catalog=None):
    """构造发给中转站的 messages。events: [{event,name}]。"""
    ev_lines = "\n".join(f'- {e["event"]} | {e["name"]}'
                         for e in (events or []))
    catalog_text = _voice_catalog_prompt(voice_catalog)
    system = (
        "你是扫地机器人测试配置解析助手。根据 PRD 内容，找出每个可模拟事件"
        "（故障类、通用类、提醒类都要）对应的语音序号和故障码。事件必须按"
        "PRD 的中文关键字/语义匹配 AP 事件，不能只因为语音号相同就选择；"
        "中文列为空、或场景列写 TBD 时，用英文、韩文或台湾中文列的内容"
        "理解场景语义；"
        "语音号、故障码和灯显在不同项目可能变化。灯显由人工配置，不要输出。"
        "只输出 JSON，不要解释。")
    user = f"""PRD 内容：
<<<PRD
{prd_text}
PRD>>>

本项目可模拟的事件（event | 中文名），故障类/通用类/提醒类都要：
{ev_lines}

{catalog_text}

输出 JSON 对象：
{{"items":[{{
  "event": "事件 identifier（必须来自上面的列表）",
  "voice": "语音序号数字，如 066；没有就空字符串",
  "voice_text": "PRD 里该语音的中文内容",
  "fault_code": "故障码数字，如 156；没有就空字符串",
  "fault_push": "Push ID，如 DA_RC_E_156",
  "kind": "场景类型（错误/通用/提醒）",
  "note": "不确定或复用说明"
}}]}}

规则：
0. 先提取 PRD 事件的中文关键字（对象 + 动作/异常，如 雷达/边刷/摆臂/离地/
   上桩/烘干/水泵/集尘），再用这些关键字匹配 AP 事件中文名和 identifier；
   不要仅凭语音序号相同选择事件。语音号/故障码/灯显在不同项目可能变化；
1. 只为 PRD 里明确配置了的事件输出条目，不要编造；故障类、通用类、提醒类都要输出；
2. 语音/故障码只要数字；PRD 里没有的字段填空字符串；
3. 灯显不要输出（由测试人员在界面按 AP 方案事件配置手动选择）；
4. 同一个事件在 PRD 出现多次（不同场景）时合并成一条；
5. 语音必须来自上面的「PRD 可用语音清单」；标注复用的，voice 填**最终发声音**，
   note 注明"复用自 Qxxx"；
6. 语音被划线/取消播报/暂不提供的，不要为该事件输出条目；
7. 若 PRD 未标注场景类型，请根据事件和语音含义推断 kind（错误/通用/提醒）；
8. 直接输出 JSON，不要调用工具，不要输出 <tool_call>、DSML 等标记。"""
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


def _endpoint(base_url):
    b = str(base_url or "").strip().rstrip("/")
    if not b:
        raise ValueError("未配置中转站地址")
    if b.endswith("/chat/completions"):
        return b
    return b + "/chat/completions"


def _base_endpoint(base_url):
    b = str(base_url or "").strip().rstrip("/")
    if not b:
        raise ValueError("未配置中转站地址")
    for suffix in ("/chat/completions", "/models"):
        if b.endswith(suffix):
            b = b[:-len(suffix)]
    return b


def fetch_models(base_url, api_key, timeout=30):
    """GET <中转站>/models，返回可用模型 id 列表。

    同时带 Authorization 和 x-api-key/Anthropic 版本头，兼容只认某一种
    鉴权方式的中转站（Claude / OpenAI 兼容入口）。
    """
    import requests
    headers = {"anthropic-version": "2023-06-01"}
    key = str(api_key or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
        headers["x-api-key"] = key
    url = _base_endpoint(base_url) + "/models"
    resp = requests.get(url, headers=headers, timeout=timeout)
    if resp.status_code >= 400:
        raise RuntimeError(f"获取模型失败 HTTP {resp.status_code}："
                           f"{resp.text[:200]}")
    data = resp.json()
    raw = data.get("data") if isinstance(data, dict) else data
    if raw is None and isinstance(data, dict):
        raw = data.get("models")
    out = []
    for item in raw or []:
        if isinstance(item, dict):
            mid = str(item.get("id") or item.get("name") or "").strip()
        else:
            mid = str(item or "").strip()
        if mid:
            out.append(mid)
    return sorted(set(out))


def is_claude_model(model):
    """Claude 模型走 Anthropic Messages 协议(中转站也支持)。"""
    s = str(model or "").strip().lower()
    return s.startswith("claude") or "anthropic" in s


def _anthropic_endpoint(base_url):
    b = _base_endpoint(base_url)
    if b.endswith("/v1"):
        return b + "/messages"
    return b + "/v1/messages"


def _anthropic_headers(api_key):
    headers = {"Content-Type": "application/json",
               "anthropic-version": "2023-06-01"}
    key = str(api_key or "").strip()
    if key:
        headers["x-api-key"] = key
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _call_claude(base_url, api_key, model, messages, timeout, max_tokens):
    """Anthropic Messages API：system 单独字段，返回 content 块列表。"""
    import requests
    system = "\n".join(m["content"] for m in messages
                       if m.get("role") == "system")
    chat = [{"role": m.get("role"), "content": m.get("content")}
            for m in messages if m.get("role") != "system"]
    payload = {"model": str(model).strip(), "messages": chat,
               "max_tokens": int(max_tokens or DEFAULT_MAX_TOKENS),
               "temperature": 0}
    if system:
        payload["system"] = system
    resp = requests.post(_anthropic_endpoint(base_url),
                         headers=_anthropic_headers(api_key),
                         json=payload, timeout=timeout)
    if resp.status_code in (404, 405):
        # 该中转站只有 OpenAI 兼容入口时自动回退
        return None
    if resp.status_code >= 400:
        raise RuntimeError(f"中转站返回 HTTP {resp.status_code}："
                           f"{resp.text[:300]}")
    try:
        data = resp.json()
    except Exception:
        raise RuntimeError(f"中转站返回的不是 JSON：{resp.text[:300]}")
    blocks = data.get("content") or []
    text = "".join(str(b.get("text", "")) if isinstance(b, dict)
                   else str(b) for b in blocks if
                   (not isinstance(b, dict) or b.get("type") == "text"))
    text = text.strip()
    if not text:
        stop = str(data.get("stop_reason") or "")
        usage = data.get("usage") or {}
        if stop == "max_tokens":
            raise RuntimeError(
                "Claude 输出被截断（达到 max_tokens）。"
                f" output_tokens={usage.get('output_tokens')}，"
                "请在 AI 配置里增大「最大输出 tokens」。")
        raise RuntimeError(f"Claude 返回内容为空（stop_reason={stop}）")
    return text


def translate_texts(texts, base_url, api_key, model, timeout=DEFAULT_TIMEOUT,
                    max_tokens=2048):
    """把 PRD 里的英文/韩文场景说明翻译成简体中文(报告可读性, 用户 2026-11-17)。

    返回 {原文: 中文}; 解析失败返回 {}（调用方保留原文）。
    """
    items = [str(t or "").strip() for t in texts or [] if str(t or "").strip()]
    if not items:
        return {}
    payload = json.dumps(items, ensure_ascii=False)
    prompt = (
        "把下面 JSON 数组里的 PRD 语音场景说明逐条翻译成简体中文。\n"
        "要求：只翻译含义，不添加解释；保留产品术语；只输出 JSON 对象，"
        "格式 {\"原文\": \"中文译文\"}。\n" + payload)
    text = call_relay(base_url, api_key, model,
                      [{"role": "user", "content": prompt}],
                      timeout=timeout, max_tokens=max_tokens)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for key, val in data.items():
        src = str(key or "").strip()
        zh = str(val or "").strip()
        if src and zh and re.search(r"[一-鿿]", zh):
            out[src] = zh
    return out


def call_relay(base_url, api_key, model, messages, timeout=DEFAULT_TIMEOUT,
               max_tokens=DEFAULT_MAX_TOKENS):
    """调用中转站：Claude 模型走 Messages 协议，其它走 OpenAI 兼容。"""
    import requests
    if not str(model or "").strip():
        raise ValueError("未配置模型名称")
    if is_claude_model(model):
        out = _call_claude(base_url, api_key, model, messages, timeout,
                           max_tokens)
        if out is not None:
            return out
    headers = {"Content-Type": "application/json"}
    if str(api_key or "").strip():
        headers["Authorization"] = f"Bearer {str(api_key).strip()}"
    payload = {"model": str(model or "").strip(), "messages": messages,
               "temperature": 0}
    if max_tokens and int(max_tokens) > 0:
        payload["max_tokens"] = int(max_tokens)
    resp = requests.post(_endpoint(base_url), headers=headers,
                         json=payload, timeout=timeout)
    if resp.status_code >= 400:
        detail = resp.text[:300]
        raise RuntimeError(f"中转站返回 HTTP {resp.status_code}：{detail}")
    try:
        data = resp.json()
    except Exception:
        raise RuntimeError(f"中转站返回的不是 JSON：{resp.text[:300]}")
    try:
        choice = data["choices"][0]
        msg = choice["message"]
    except Exception:
        raise RuntimeError(f"中转站返回格式异常：{str(data)[:300]}")
    content = msg.get("content")
    if isinstance(content, list):        # 有些中转站返回分段内容
        content = "".join(str(c.get("text", "")) if isinstance(c, dict)
                          else str(c) for c in content)
    content = str(content or "").strip()
    if not content:
        finish = str(choice.get("finish_reason") or "")
        usage = data.get("usage") or {}
        reasoning = str(msg.get("reasoning_content") or "")
        if finish == "length" or reasoning:
            raise RuntimeError(
                "模型输出被截断（推理占满了输出长度，未给出正文）。"
                f" completion_tokens={usage.get('completion_tokens')}，"
                "请在 AI 配置里增大「最大输出 tokens」或换非推理模型。")
        raise RuntimeError(f"模型返回内容为空（finish_reason={finish}）")
    return content


def parse_response(text, events=None):
    """从模型输出里取 JSON，返回规范化后的条目列表。

    容错：代码围栏、前后说明文字、Claude Code/工具调用标记（<tool_call>、
    DSML）等。优先找 `{"items"`，其次找列表，用 raw_decode 截取第一个
    完整 JSON，后面的杂文本忽略。
    """
    raw = str(text or "").strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw,
                 flags=re.MULTILINE).strip()
    raw = re.sub(r"<\|?\|?DSML\|?\|?>", "", raw)
    raw = re.sub(r"</?tool_call>|</?invoke>|</?parameter[^>]*>", "", raw)
    data = None
    decoder = json.JSONDecoder()
    for marker in ('{"items"', "[{", '{"event"'):
        idx = raw.find(marker)
        if idx < 0:
            continue
        try:
            data, _end = decoder.raw_decode(raw[idx:])
            break
        except Exception:
            continue
    if data is None:
        for pat in (r"\{.*\}", r"\[.*\]"):
            m = re.search(pat, raw, re.S)
            if m:
                try:
                    data = json.loads(m.group(0))
                    break
                except Exception:
                    continue
    if data is None:
        raise ValueError("AI 返回内容不是合法 JSON")
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError("AI 返回 JSON 里没有 items 列表")
    known = {e["event"] for e in (events or [])} if events else None
    names = {e["event"]: e.get("name", "") for e in (events or [])}
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        event = str(it.get("event") or "").strip()
        if not event or (known is not None and event not in known):
            continue
        out.append({
            "event": event,
            "name": names.get(event) or str(it.get("name") or "").strip(),
            "voice": _digits(it.get("voice")),
            "voice_text": str(it.get("voice_text") or "").strip(),
            "led_home": str(it.get("led_home") or "").strip(),
            "led_home_text": str(it.get("led_home_text") or "").strip(),
            "led_start": str(it.get("led_start") or "").strip(),
            "led_start_text": str(it.get("led_start_text") or "").strip(),
            "fault_code": _digits(it.get("fault_code")),
            "fault_push": str(it.get("fault_push") or "").strip(),
            "kind": str(it.get("kind") or "").strip(),
            "note": str(it.get("note") or "").strip(),
        })
    return out


def _digits(value):
    m = re.search(r"\d+", str(value or ""))
    return m.group(0) if m else ""


def parse_prd(path, events, base_url, api_key, model, led_names=None,
              max_chars=MAX_PRD_CHARS, max_tokens=DEFAULT_MAX_TOKENS,
              voice_catalog=None):
    """完整流程：读 PRD → 调中转站 → 返回解析条目。"""
    text = extract_text(path, max_chars=max_chars)
    messages = build_messages(text, events, led_names, voice_catalog)
    content = call_relay(base_url, api_key, model, messages,
                         max_tokens=max_tokens)
    items = parse_response(content, events)
    return apply_voice_catalog(items, voice_catalog)
