from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass

from ..config import settings
from .language import normalize

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Minimal multilingual stopword list; enough to keep BM25 from matching on filler.
_STOP = {
    "el", "la", "los", "las", "un", "una", "de", "del", "que", "que", "y", "o", "u", "en", "con", "por",
    "para", "me", "mi", "mis", "se", "es", "son", "como", "cuando", "cuanto", "a", "al", "lo", "le", "te",
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "do", "i", "my", "is", "are",
    "what", "when", "how", "you", "your", "it", "be", "can", "I",
}


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(normalize(text)) if t not in _STOP and len(t) > 1]


@dataclass
class FaqDoc:
    id: str
    lang: str
    topic: str
    question: str
    answer: str
    tokens: list[str]


class FaqIndex:
    """Small lexical BM25 retriever over the client FAQ.

    No embeddings dependency: the corpus is ~26 documents, so BM25 is faster,
    deterministic, inspectable and works offline. Swapping in embeddings later
    only changes `_score` (documented in docs/architecture.md).
    """

    def __init__(self, path=None):
        path = path or settings.data_dir / "faq.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.docs = [
            FaqDoc(i["id"], i["lang"], i["topic"], i["q"], i["a"], tokenize(f"{i['q']} {i['a']}"))
            for i in raw["items"]
        ]
        self.avg_len = sum(len(d.tokens) for d in self.docs) / max(1, len(self.docs))
        df: dict[str, int] = {}
        for doc in self.docs:
            for token in set(doc.tokens):
                df[token] = df.get(token, 0) + 1
        self.df = df
        self.n_docs = len(self.docs)

    def _idf(self, token: str) -> float:
        df = self.df.get(token, 0)
        return math.log(1 + (self.n_docs - df + 0.5) / (df + 0.5))

    def search(self, query: str, lang: str | None = None, k: int = 2) -> list[FaqDoc]:
        q_tokens = tokenize(query)
        if not q_tokens:
            return []
        k1, b = 1.5, 0.75
        scored: list[tuple[float, FaqDoc]] = []
        for doc in self.docs:
            if not doc.tokens:
                continue
            freq: dict[str, int] = {}
            for token in doc.tokens:
                freq[token] = freq.get(token, 0) + 1
            score = 0.0
            for token in q_tokens:
                f = freq.get(token, 0)
                if not f:
                    continue
                score += self._idf(token) * (f * (k1 + 1)) / (f + k1 * (1 - b + b * len(doc.tokens) / self.avg_len))
            if lang and doc.lang == lang:
                score *= 1.35
            if score > 0:
                scored.append((score, doc))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [doc for _, doc in scored[:k]]

    def context_block(self, query: str, lang: str | None = None, k: int = 2) -> tuple[str, list[str]]:
        docs = self.search(query, lang=lang, k=k)
        if not docs:
            return "", []
        block = "\n".join(f"- [{d.id}] {d.answer}" for d in docs)
        return block, [d.id for d in docs]


FAQ = FaqIndex()
