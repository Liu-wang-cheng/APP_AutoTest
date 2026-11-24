# -*- coding: utf-8 -*-
"""本地中文向量模型（bge-small-zh-v1.5，ONNX 量化版）。

* 随程序内置在 `models/bge-small-zh-v1.5/`（约 24MB），离线可用；
* CLS 向量 + L2 归一化，用于 PRD 语音与事件的语义匹配；
* 模型/依赖缺失时返回 None，调用方退回字符 n-gram（不阻塞功能）。
"""

import os
import threading

import numpy as np

_MODEL_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "models",
    "bge-small-zh-v1.5")

_lock = threading.Lock()
_shared = None


class Embedder:
    def __init__(self, model_dir=None):
        self.dir = model_dir or _MODEL_DIR
        self._session = None
        self._tokenizer = None
        self._load()

    def _load(self):
        model = os.path.join(self.dir, "model_quantized.onnx")
        tok = os.path.join(self.dir, "tokenizer.json")
        if not (os.path.exists(model) and os.path.exists(tok)):
            return
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer
            self._session = ort.InferenceSession(
                model, providers=["CPUExecutionProvider"])
            self._tokenizer = Tokenizer.from_file(tok)
            self._tokenizer.enable_truncation(max_length=128)
            self._tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
        except Exception:
            self._session = None

    @property
    def available(self):
        return self._session is not None and self._tokenizer is not None

    def encode(self, texts):
        """文本列表 → 归一化向量矩阵 (N, 512)；不可用返回 None。"""
        if not self.available or not texts:
            return None
        encs = self._tokenizer.encode_batch([str(t or "") for t in texts])
        ids = np.array([e.ids for e in encs], dtype=np.int64)
        mask = np.array([e.attention_mask for e in encs], dtype=np.int64)
        feed = {"input_ids": ids, "attention_mask": mask,
                "token_type_ids": np.zeros_like(ids)}
        with _lock:
            outs = self._session.run(None, feed)
        emb = outs[0][:, 0, :].astype(np.float32)   # CLS
        norm = np.linalg.norm(emb, axis=1, keepdims=True)
        norm[norm == 0] = 1.0
        return emb / norm


def get_embedder():
    """进程内共享的 Embedder；不可用时返回 None。"""
    global _shared
    if _shared is None:
        try:
            _shared = Embedder()
        except Exception:
            _shared = Embedder.__new__(Embedder)   # 占位, available=False
            _shared._session = None
            _shared._tokenizer = None
    return _shared if getattr(_shared, "available", False) else None
