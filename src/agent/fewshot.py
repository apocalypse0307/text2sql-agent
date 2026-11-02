"""
Few-shot 示例检索器（P2-S2）。

用途：给定用户问题，从 evals/few_shots.json 里检索最相似的 Top-K 条注入 prompt。
原理（工程取舍）：
  - 为什么不用 BGE-M3 向量检索？P2 是 SQL Agent，架构上刻意不依赖 GPU embedding 模型
    （保持轻量、可 CPU 部署）；few-shot 库只有几十条，TF 关键词相似度已足够。
  - 相似度 = 问题分词后的词频加权余弦。中文分词用「字符 bigram + 业务关键词白名单」：
    bigram 对 '订单/金额/月份' 这类词很稳，白名单词（订单/客户/卖家/品类/运费/复购/评价…）
    单独加权重，因为这些词决定了「查哪张表」。
  - README 注明：库规模上千后切 BGE-M3 编码即可，接口签名不变（retrieve_topk(q, k)）。
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

from src.core.config import settings

# 业务关键词白名单：命中即加 2.0 权重（这些词直接决定选表）
_KEYWORDS = [
    "订单", "客户", "卖家", "商品", "品类", "运费", "复购", "评价", "支付",
    "金额", "收入", "销量", "销售额", "客单", "州", "城市", "月", "季度", "年",
    "信用卡", "分期", "退款", "取消", "送达",
]

_CJK = re.compile(r"[\u4e00-\u9fff]")


def _bigrams(s: str) -> list[str]:
    """取中文字符 bigram；非中文 token 原样保留。"""
    toks: list[str] = []
    for m in re.finditer(r"[\u4e00-\u9fff]+|[a-zA-Z0-9_]+", s.lower()):
        t = m.group()
        if _CJK.match(t):
            toks += [t[i:i + 2] for i in range(max(len(t) - 1, 1))] or [t]
        else:
            toks.append(t)
    return toks


def _weighted_tf(question: str) -> dict[str, float]:
    """词频向量；命中业务关键词的词额外加权。"""
    tf: dict[str, float] = {}
    for tok in _bigrams(question):
        tf[tok] = tf.get(tok, 0.0) + 1.0
    for kw in _KEYWORDS:
        if kw in question:
            for tok in _bigrams(kw):
                tf[tok] = tf.get(tok, 0.0) + 2.0
    return tf


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    inter = set(a) & set(b)
    if not inter:
        return 0.0
    dot = sum(a[t] * b[t] for t in inter)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


class FewShotRetriever:
    def __init__(self, path: str | Path | None = None):
        p = Path(path or settings.fewshots_path)
        self._items: list[dict] = json.loads(p.read_text(encoding="utf-8"))
        self._vecs: list[dict[str, float]] = [_weighted_tf(it["question"]) for it in self._items]

    @property
    def size(self) -> int:
        return len(self._items)

    def retrieve_topk(self, question: str, k: int = 3) -> list[dict]:
        """返回与 question 最相似的 Top-K 条 few-shot（含 question/sql/reasoning）。"""
        qv = _weighted_tf(question)
        scored = sorted(
            ((_cosine(qv, v), i) for i, v in enumerate(self._vecs)),
            key=lambda x: x[0], reverse=True,
        )
        return [self._items[i] for s, i in scored[:k] if s > 0]
