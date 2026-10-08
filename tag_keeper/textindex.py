"""文字の2文字組（bigram）の TF-IDF による、小さな類似検索。

日本語は単語の区切りが無いので、形態素解析の代わりに文字の2文字組を使う（依存を増やさないため）。
移動先の候補のフォルダの絞り込み（organize）と、似た過去の判断の検索（decisions）で使う。
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Sequence


def bigrams(text: str) -> list[str]:
    """表記をそろえ（NFKC・小文字）、数字と記号を除いた文字の2文字組。1文字の語はそのまま。"""
    t = unicodedata.normalize("NFKC", text).lower()
    t = re.sub(r"[\d\W_]+", " ", t)
    out: list[str] = []
    for word in t.split():
        if len(word) == 1:
            out.append(word)
        out.extend(word[i : i + 2] for i in range(len(word) - 1))
    return out


class TfIdf:
    """文書の集まり（各文書は語の出現回数）に対する TF-IDF の余弦類似度の検索。"""

    def __init__(self, docs: Sequence[Counter[str]]) -> None:
        df: Counter[str] = Counter()
        for c in docs:
            df.update(c.keys())
        n = max(len(docs), 1)
        self.idf = {t: math.log(1 + n / d) for t, d in df.items()}
        self.postings: dict[str, list[tuple[int, float]]] = defaultdict(list)
        for i, c in enumerate(docs):
            vec = {t: (1 + math.log(v)) * self.idf[t] for t, v in c.items()}
            norm = math.sqrt(sum(w * w for w in vec.values())) or 1.0
            for t, w in vec.items():
                self.postings[t].append((i, w / norm))

    def search(self, text: str) -> list[tuple[int, float]]:
        """text に近い文書の（番号, 類似度）を、近い順にすべて返す。"""
        q = Counter(bigrams(text))
        vec = {t: (1 + math.log(v)) * self.idf[t] for t, v in q.items() if t in self.idf}
        norm = math.sqrt(sum(w * w for w in vec.values())) or 1.0
        scores: dict[int, float] = defaultdict(float)
        for t, w in vec.items():
            for i, dw in self.postings.get(t, ()):
                scores[i] += (w / norm) * dw
        return sorted(scores.items(), key=lambda x: -x[1])
