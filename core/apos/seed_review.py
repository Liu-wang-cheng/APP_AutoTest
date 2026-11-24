# -*- coding: utf-8 -*-
"""AI 复核待共享的学习种子: 只放行 AI 没有提出修正的条目。

共享种子面向所有新用户, 宁可少放, 不能把明显错误的映射带出去。
"""
import json


def event_catalog(expect=None):
    """AI 审核可选的 AP 事件表 {event: 中文名}。

    AP 配置里的名称优先，其次内置翻译/常用故障标签。设备页和学习库页共用。
    """
    from core.apos import faults
    # 有 AP 配置时只允许 AP 平台真实存在的事件; 内置事件表只作为
    # 没加载 AP 配置时的兜底, 避免把库映射到平台不存在的事件。
    if expect:
        return {str(event): str((info or {}).get("name") or event)
                for event, info in expect.items()}
    out = {}
    for p in faults.presets({}):
        event = str(p.get("event") or "")
        if event:
            out[event] = str(p.get("name") or event)
    for event, name in faults.EVENT_NAMES.items():
        out.setdefault(str(event), str(name))
    return out


def _sound(info):
    snd = (info or {}).get("sound")
    if isinstance(snd, dict):
        snd = snd.get("identifier")
    return str(snd or "")


def _entry_line(entry, expect):
    ev = str(entry.get("event") or "")
    voice = str(entry.get("voice") or "")
    cands = []
    for other, info in (expect or {}).items():
        if _sound(info) != voice:
            continue
        name = str(info.get("name") or other)
        cands.append(f"{other}={name}")
    cur_name = str((expect.get(ev) or {}).get("name") or ev)
    return (f"{entry.get('key')} | 语音{voice} | PRD含义: "
            f"{entry.get('voice_text') or entry.get('key')} | 当前: "
            f"{ev}({cur_name}) | 候选: {'; '.join(cands) or ev}")


def _parse_items(text):
    raw = str(text or "").strip().replace("```json", "").replace("```", "")
    dec = json.JSONDecoder()
    for mark in ("{", "["):
        idx = raw.find(mark)
        if idx >= 0:
            try:
                data = dec.raw_decode(raw[idx:])[0]
                return data.get("items") if isinstance(data, dict) else data
            except Exception:
                continue
    return None


def audit_entries(entries, allowed_events, call, batch_size=25,
                  progress=None):
    """AI 审核本机学习库, 返回 (corrections, checked, errors)。

    corrections 只包含 AI 明确指出且落在可用事件表里的修正; 当前映射正确的
    条目不输出。调用方负责决定是否写回学习库。
    """
    allowed = {str(k): str(v or k) for k, v in (allowed_events or {}).items()
               if str(k or "").strip()}
    corrections, errors, checked = [], [], 0
    if not entries:
        return corrections, checked, errors
    allowed_text = "\n".join(f"{k} = {v}" for k, v in sorted(allowed.items()))
    total = len(entries)
    for start in range(0, total, batch_size):
        batch = entries[start:start + batch_size]
        lines = []
        for entry in batch:
            ev = str(entry.get("event") or "")
            name = str(entry.get("event_name") or allowed.get(ev) or ev)
            lines.append(
                f"key: {entry.get('key')}\n"
                f"PRD含义: {entry.get('voice_text') or entry.get('key')}\n"
                f"当前事件: {ev}（{name}）")
        prompt = (
            "你是扫地机器人测试工具的学习库审核员。每条记录是"
            "「PRD 事件关键字/含义 → AP 平台事件」的学习映射。请先提取"
            "PRD 的对象和动作/异常关键字，再按中文语义核对当前事件是否匹配。"
            "语音号、故障码、灯显在不同项目可能变化，不能作为选择事件的依据。"
            "只能从下方「可用事件」中选择事件；只有明显错误或存在更合适的"
            "事件时才输出修正，正确的不输出。不要修改语音序号和故障码。"
            "严格输出 JSON：{\"items\":[{\"key\":\"...\",\"event\":\"...\","
            "\"reason\":\"...\"}]}，没有修正就输出 {\"items\":[]}。\n\n"
            "可用事件：\n" + allowed_text + "\n\n待审核：\n"
            + "\n\n".join(lines))
        try:
            items = _parse_items(call(prompt))
        except Exception as e:
            errors.append(str(e))
            continue
        if items is None:
            errors.append("AI 返回不是合法 JSON")
            continue
        checked += len(batch)
        by_key = {str(e.get("key") or ""): e for e in batch
                  if str(e.get("key") or "")}
        seen = set()
        for it in items:
            if not isinstance(it, dict):
                continue
            key = str(it.get("key") or "")
            event = str(it.get("event") or "")
            entry = by_key.get(key)
            if not key or not event or key in seen or entry is None:
                continue
            if event not in allowed or event == str(entry.get("event") or ""):
                continue
            seen.add(key)
            corrections.append({
                "key": key,
                "event": event,
                "current_event": str(entry.get("event") or ""),
                "reason": str(it.get("reason") or "")[:200],
            })
        if progress:
            try:
                progress(min(start + batch_size, total), total)
            except Exception:
                pass
    return corrections, checked, errors


def review_entries(entries, expect, call, batch_size=25):
    """返回 (approved, rejected, errors)。

    call(prompt) -> AI 文本。AI 只为可疑条目返回修正; 有修正的条目拒绝共享。
    """
    approved, rejected, errors = [], [], []
    for start in range(0, len(entries), batch_size):
        batch = entries[start:start + batch_size]
        lines = [_entry_line(e, expect) for e in batch]
        prompt = (
            "你是共享学习库的审核员。下面每条是 PRD 事件→AP 事件的映射。"
            "请严格按 PRD 含义检查当前事件是否合理；只能从候选里选。"
            "只对明显错误的条目输出修正，正确的不输出。"
            "严格输出 JSON：{\"items\":[{\"key\":\"...\","
            "\"event\":\"...\",\"reason\":\"...\"}]}。\n\n"
            + "\n".join(lines))
        try:
            items = _parse_items(call(prompt))
        except Exception as e:
            errors.append(str(e))
            rejected.extend(batch)
            continue
        if items is None:
            errors.append("AI 返回不是合法 JSON")
            rejected.extend(batch)
            continue
        bad = {str(it.get("key") or ""): it for it in items
               if isinstance(it, dict)}
        for entry in batch:
            key = str(entry.get("key") or "")
            if key in bad:
                rejected.append({**entry,
                                 "review_reason": str(
                                     bad[key].get("reason") or "")[:160]})
            else:
                approved.append(entry)
    return approved, rejected, errors
