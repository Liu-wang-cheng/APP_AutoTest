# -*- coding: utf-8 -*-
"""PRD 解析记忆库：把语音 → 事件/故障码/类型 的对应关系存在本机并复用。

* 命中记忆库的语音不再发给 AI（可离线、秒级）；
* 对措辞略有不同的语音，用本地字符向量（char n-gram + 余弦）做近似匹配；
* 人工修改表格后按 `source=user` 写回，以后优先于 AI 结果；
* 文件位于 `~/.autotest/prd_memory.json`，只存在本机。
"""

import json
import os
import re
import time
from collections import Counter

_SUB_RE = re.compile(r"[\s,，。.;；:：、()（）/\-]+")


def norm(text):
    """归一化中文描述：去掉空白/标点, 统一小写。"""
    return _SUB_RE.sub("", str(text or "")).lower()


def voice_key(condition="", chinese="", text=""):
    """语音的稳定键: 优先触发条件(更具体), 其次中文文案。"""
    return norm(condition) or norm(chinese) or norm(text)


def _vec(text):
    s = norm(text)
    if len(s) < 2:
        return Counter([s]) if s else Counter()
    return Counter(s[i:i + 2] for i in range(len(s) - 1))


def _cosine(a, b):
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    dot = sum(a[k] * b[k] for k in common)
    na = sum(v * v for v in a.values()) ** 0.5
    nb = sum(v * v for v in b.values()) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def default_seed_path():
    """随包种子库路径: 打包后 datas 在 _MEIPASS, 不在 exe 目录。

    开发环境/打包环境都先找用户数据根, 再找 bundled_root(), 避免 onefile
    发布版读不到种子库(用户 2026-11-17 代码审查)。
    """
    rel = os.path.join("config", "fault_events", "seed_mappings.json")
    candidates = []
    try:
        from core.driver import BASE_DIR
        candidates.append(os.path.join(BASE_DIR, rel))
    except Exception:
        pass
    try:
        from core.bootstrap import bundled_root
        candidates.append(os.path.join(bundled_root(), rel))
    except Exception:
        pass
    for path in candidates:
        if os.path.isfile(path):
            return path
    return candidates[0] if candidates else rel


class PrdMemory:
    """极简本地记忆库(JSON 文件, 线程内使用)。"""

    #: 学习库只保存「PRD 语义 → AP 事件」。语音号/Q号/故障码/灯显/机型
    #: 都随项目变化, 存进学习库会干扰跨项目复用(用户 2026-11-17)。
    _SEMANTIC_FIELDS = ("event", "event_name", "kind", "voice_text")
    _META_FIELDS = ("source", "updated", "verified_at")

    def __init__(self, path=None):
        self.path = path or os.path.join(
            os.path.expanduser("~"), ".autotest", "prd_memory.json")
        self.items = {}
        self._vectors = {}
        self._emb_sig = None
        self._emb_mat = None
        #: 本次实例删除过的键(墓碑): 合并磁盘时不复活
        self._deleted = set()
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            raw = data if isinstance(data, dict) else {}
        except Exception:
            raw = {}
        items = {}
        changed = False
        for key, val in raw.items():
            if not isinstance(val, dict):
                changed = True
                continue
            clean = self._clean_entry(val)
            if clean != val:
                changed = True
            items[norm(key)] = clean
        self.items = items
        self._vectors = {k: _vec(k) for k in self.items}
        self._emb_sig = None
        self._emb_mat = None
        if changed:
            # 老版本存过语音号/故障码/灯显/机型, 加载时顺手清掉
            self.save()

    def delete(self, key):
        """删除映射(带墓碑), 防止另一实例保存时把删掉的条目写回来。"""
        k = norm(key)
        self.items.pop(k, None)
        self._vectors.pop(k, None)
        self._deleted = getattr(self, "_deleted", set())
        self._deleted.add(k)
        self.save()

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            # 设备页/AI 配置页各持一份实例: 保存前合并磁盘上更新的条目,
            # 避免后保存的一方把先保存的修改回滚(用户 2026-11-17 审查)
            deleted = getattr(self, "_deleted", set())
            try:
                with open(self.path, encoding="utf-8") as f:
                    disk = json.load(f)
            except Exception:
                disk = {}
            if isinstance(disk, dict):
                for key, val in disk.items():
                    k = norm(key)
                    if k in deleted or not isinstance(val, dict):
                        continue
                    fresh = self._clean_entry(val)
                    cur = self.items.get(k)
                    if cur is None or str(fresh.get("updated") or "") > str(
                            cur.get("updated") or ""):
                        self.items[k] = fresh
                        self._vectors[k] = _vec(k)
                self._emb_sig = None
                self._emb_mat = None
            # 原子写: 崩溃/杀进程不会把学习库截断
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.items, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
            self._deleted = set()
        except Exception:
            pass

    def exact(self, key):
        return self.items.get(norm(key))

    def best(self, key, threshold=0.72, embedder=None):
        """先精确命中, 再本地向量近似匹配。返回 (mapping, score)。

        embedder 可用时用 bge-small 语义向量, 否则退回字符 n-gram 向量。
        """
        k = norm(key)
        if not k:
            return None, 0.0
        if k in self.items:
            return self.items[k], 1.0
        if embedder is not None and getattr(embedder, "available", False):
            items = list(self.items.items())
            signature = [(key, str(val.get("event_name") or ""))
                         for key, val in items]
            if self._emb_mat is None or self._emb_sig != signature:
                texts = [key for key, _n in signature]
                texts += [name for _k, name in signature]
                self._emb_sig = signature
                self._emb_mat = embedder.encode(texts)
            q = embedder.encode([k])
            n = len(items)
            if self._emb_mat is not None and q is not None and n:
                mat = self._emb_mat
                by_key = mat[:n] @ q[0]
                names = [name for _k, name in signature]
                best_idx, best_score = -1, 0.0
                for i in range(n):
                    score = float(by_key[i])
                    if names[i]:
                        score = max(score,
                                    float(mat[n + i] @ q[0]))
                    if score > best_score:
                        best_idx, best_score = i, score
                if best_idx >= 0 and best_score >= threshold:
                    return items[best_idx][1], best_score
                return None, best_score
        kv = self._vectors.get(k) or _vec(k)
        best, score = None, 0.0
        for other, vec in self._vectors.items():
            s = _cosine(kv, vec)
            name = str((self.items.get(other) or {}).get("event_name")
                       or "")
            if name:
                s = max(s, _cosine(kv, _vec(name)))
            if s > score:
                best, score = other, s
        if best is not None and score >= threshold:
            return self.items[best], score
        return None, score

    # 种子库只共享「PRD 关键词/文案 → AP 事件」的语义映射；
    # 语音号/故障码/灯显/项目指纹随项目变化, 不跨项目复用(用户 2026-11-16)。
    _SEED_FIELDS = ("event", "event_name", "kind", "voice_text")

    def export_seed(self, path, sources=("verified", "ai_review")):
        """导出脱敏学习种子(不含 IP/SN/用户信息), 供仓库共享/新用户继承。"""
        entries = []
        for key, val in sorted(self.items.items()):
            if not isinstance(val, dict) or val.get("source") not in sources:
                continue
            if not val.get("event"):
                continue
            item = {"key": key}
            for field in self._SEED_FIELDS:
                if val.get(field) not in (None, ""):
                    item[field] = val[field]
            item["source"] = val.get("source")
            entries.append(item)
        data = {"version": 1,
                "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "entries": entries}
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        return len(entries)

    def load_seed_file(self, path):
        """加载随工具发布的种子库; 只补空缺, 不覆盖本机学习/用户确认。"""
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return 0
        entries = data.get("entries") if isinstance(data, dict) else data
        added = 0
        for item in entries or []:
            if not isinstance(item, dict):
                continue
            key = norm(item.get("key") or item.get("voice_key") or "")
            if not key or key in self.items:
                continue
            val = {k: item.get(k) for k in self._SEED_FIELDS
                   if item.get(k) not in (None, "")}
            if not val.get("event"):
                continue
            val["source"] = "seed"
            self.items[key] = val
            self._vectors[key] = _vec(key)
            added += 1
        if added:
            self._emb_sig = None
            self._emb_mat = None
        return added

    @classmethod
    def _clean_entry(cls, mapping):
        """只留语义字段 + 运营元数据, 丢弃语音号/故障码/灯显/机型等."""
        entry = {}
        for field in cls._SEMANTIC_FIELDS:
            if mapping.get(field) not in (None, ""):
                entry[field] = mapping[field]
        for field in cls._META_FIELDS:
            if mapping.get(field) not in (None, ""):
                entry[field] = mapping[field]
        return entry

    def put(self, key, mapping, source="ai", save=True, force=False):
        k = norm(key)
        if not k or not isinstance(mapping, dict):
            return
        old = self.items.get(k) or {}
        # 人工确认/真机验证过的结果不能被低优先级来源覆盖;
        # AI 审核明确要求修正时可 force
        if (old.get("source") in ("user", "verified", "ai_review")
                and source not in ("user", "verified", "ai_review")
                and not force):
            return
        entry = self._clean_entry(mapping)
        entry["source"] = source
        entry["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.items[k] = entry
        self._vectors[k] = _vec(k)
        self._emb_sig = None
        self._emb_mat = None
        # 批量学习时先不落盘, 最后 save() 一次(100+ 条逐条写 JSON 很卡)
        if save:
            self.save()
