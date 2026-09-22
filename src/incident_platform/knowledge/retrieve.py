from dataclasses import dataclass

RRF_K = 60
MAX_SNIPPETS = 5
MAX_SNIPPET_CHARS = 1200
MAX_TOTAL_CHARS = 6000


@dataclass(frozen=True)
class Snippet:
    chunk_id: str
    doc_id: str
    title: str
    heading: str
    body: str
    source: str


def fuse(rankings: list[list[str]]) -> list[str]:
    """Reciprocal Rank Fusion over one or more ranked id lists. Pure; no I/O.

    Scores are an ordering device only and are never returned - nothing downstream
    should reason about a retrieval score.
    """
    scores: dict[str, float] = {}
    order: list[str] = []
    for ranking in rankings:
        for rank, key in enumerate(ranking):
            if key not in scores:
                scores[key] = 0.0
                order.append(key)
            scores[key] += 1.0 / (RRF_K + rank + 1)
    # Ties break on first-seen order, so the result is stable across runs.
    return sorted(order, key=lambda key: (-scores[key], order.index(key)))
