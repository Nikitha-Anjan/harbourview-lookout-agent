"""Regulation retrieval over ``regulations/*.md``.

Lexical, not semantic: the corpus is four short, well-structured documents, so
keyword scoring over Markdown sections is enough and stays fully explainable -
every hit is a real paragraph from a named file. See TECHNICAL_DESIGN.md for
why a vector store was not used.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_STOPWORDS = {
    "a", "an", "the", "of", "for", "to", "and", "or", "is", "are", "was", "were",
    "what", "which", "who", "how", "do", "does", "did", "can", "we", "our", "us",
    "on", "in", "at", "by", "it", "its", "be", "with", "that", "this", "have",
    "has", "if", "when", "about", "there", "any", "want", "wants", "customer",
    "please", "would", "should", "could", "policy", "rule", "rules", "allow",
}

# Topic -> extra query terms, so short questions still hit the right file.
_TOPIC_HINTS = {
    "refund": ["refund", "cancellation", "weather", "closure", "re-entry"],
    "capacity": ["occupancy", "capacity", "fire", "zone", "threshold", "evacuation"],
    "staffing": ["staff", "attendant", "staffing", "visitors", "monitor"],
    "hours": ["hours", "season", "opening", "closing", "holiday"],
    "weather": ["weather", "wind", "lightning", "storm", "closure", "precipitation"],
    "accessibility": ["accessibility", "elevator", "restroom", "service animal"],
}


@dataclass
class RegulationHit:
    file: str
    score: int
    sections: list[str]

    def as_dict(self) -> dict:
        return {"file": f"regulations/{self.file}", "score": self.score, "sections": self.sections}


class RegulationStore:
    def __init__(self, regulations_dir: str | Path):
        self.regulations_dir = Path(regulations_dir)
        self._cache: dict[str, str] | None = None

    def _files(self) -> dict[str, str]:
        if self._cache is None:
            self._cache = {
                p.name: p.read_text(encoding="utf-8")
                for p in sorted(self.regulations_dir.glob("*.md"))
            }
        return self._cache

    @staticmethod
    def _terms(text: str) -> list[str]:
        tokens = re.findall(r"[a-z0-9][a-z0-9-]*", text.lower())
        return [t for t in tokens if t not in _STOPWORDS and len(t) > 2]

    def _expanded_terms(self, query: str) -> set[str]:
        terms = set(self._terms(query))
        lowered = query.lower()
        for topic, hints in _TOPIC_HINTS.items():
            if topic in lowered or any(h in lowered for h in hints):
                terms.update(hints)
        return terms

    def search(self, query: str, top_k: int = 3) -> list[dict]:
        terms = self._expanded_terms(query)
        if not terms:
            return []
        hits: list[RegulationHit] = []
        for filename, text in self._files().items():
            # One section per Markdown heading, keeping all its numbered clauses.
            sections = [s.strip() for s in re.split(r"\n(?=#{1,6}\s)", text) if s.strip()]
            matched: list[tuple[int, str]] = []
            score = 0
            for section in sections:
                overlap = len(terms & set(self._terms(section)))
                if overlap:
                    score += overlap
                    matched.append((overlap, re.sub(r"\s+", " ", section)))
            if score:
                matched.sort(key=lambda m: m[0], reverse=True)
                hits.append(RegulationHit(filename, score, [m[1][:600] for m in matched[:3]]))
        hits.sort(key=lambda h: h.score, reverse=True)
        return [h.as_dict() for h in hits[:top_k]]

    def policy_text(self, topic: str) -> str:
        hits = self.search(topic, top_k=1)
        if not hits:
            return "No matching regulation text found."
        filename = Path(hits[0]["file"]).name
        return self._files().get(filename, "No matching regulation text found.")
