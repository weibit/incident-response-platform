import re
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


TERM = re.compile(r"\w+")


def _tsquery(query: str) -> str:
    """OR the query terms.

    plainto_tsquery ANDs them, which makes a realistic multi-word investigator query
    match nothing at all, because chunks are only a few sentences long. Relevance
    belongs to ranking, not to requiring every term in one chunk. Dropping non-word
    characters also means no tsquery metacharacter survives into the query.
    """
    return " | ".join(TERM.findall(query.lower()))


LEXICAL_SQL = """
SELECT c.chunk_id, c.doc_id, d.title, c.heading, c.body, d.source
FROM knowledge_chunk c JOIN knowledge_document d USING(doc_id)
WHERE d.kind = %(kind)s
  AND (d.service IS NULL OR d.service = %(service)s)
  AND c.tsv @@ to_tsquery('english', %(tsq)s)
ORDER BY ts_rank_cd(c.tsv, to_tsquery('english', %(tsq)s)) DESC, c.chunk_id
LIMIT %(limit)s
"""


def _cap(snippets: list[Snippet]) -> list[Snippet]:
    """Bound context and cost. Enforced here so no caller can exceed it."""
    capped: list[Snippet] = []
    budget = MAX_TOTAL_CHARS
    for snippet in snippets[:MAX_SNIPPETS]:
        body = snippet.body[:MAX_SNIPPET_CHARS]
        if len(body) > budget:
            body = body[:budget]
        if not body:
            break
        budget -= len(body)
        capped.append(
            Snippet(
                chunk_id=snippet.chunk_id,
                doc_id=snippet.doc_id,
                title=snippet.title,
                heading=snippet.heading,
                body=body,
                source=snippet.source,
            )
        )
    return capped


DENSE_SQL = """
SELECT c.chunk_id
FROM knowledge_chunk c JOIN knowledge_document d USING(doc_id)
WHERE d.kind = %(kind)s
  AND (d.service IS NULL OR d.service = %(service)s)
  AND c.embedding IS NOT NULL
ORDER BY c.embedding <=> %(vector)s, c.chunk_id
LIMIT %(limit)s
"""


async def dense_ids(pool, embedder, kind: str, service: str, query: str) -> list[str]:
    """Dense candidate ids, or an empty list when no provider is configured."""
    if embedder is None:
        return []
    vector = await embedder.embed_query(query)
    if vector is None:
        return []
    params = {"kind": kind, "service": service, "vector": str(vector), "limit": MAX_SNIPPETS}
    async with pool.connection() as conn:
        rows = await (await conn.execute(DENSE_SQL, params)).fetchall()
    return [row["chunk_id"] for row in rows]


async def search(
    pool, kind: str, service: str, query: str, limit: int = MAX_SNIPPETS, embedder=None
) -> list[Snippet]:
    """Hybrid search. Lexical always; dense only when a provider is configured.

    Documents with a NULL service are global and always eligible.
    """
    limit = max(1, min(limit, MAX_SNIPPETS))
    # Always fetch MAX_SNIPPETS candidates regardless of `limit`. Fusion needs more
    # material than the caller ultimately wants, and the dense leg fuses against this
    # list before the final trim.
    params = {"kind": kind, "service": service, "tsq": _tsquery(query), "limit": MAX_SNIPPETS}
    async with pool.connection() as conn:
        rows = await (await conn.execute(LEXICAL_SQL, params)).fetchall()
    by_id = {
        row["chunk_id"]: Snippet(
            chunk_id=row["chunk_id"],
            doc_id=row["doc_id"],
            title=row["title"],
            heading=row["heading"],
            body=row["body"],
            source=row["source"],
        )
        for row in rows
    }
    rankings = [[row["chunk_id"] for row in rows]]
    dense = await dense_ids(pool, embedder, kind, service, query)
    if dense:
        missing = [key for key in dense if key not in by_id]
        if missing:
            async with pool.connection() as conn:
                extra = await (
                    await conn.execute(
                        "SELECT c.chunk_id, c.doc_id, d.title, c.heading, c.body, d.source "
                        "FROM knowledge_chunk c JOIN knowledge_document d USING(doc_id) "
                        "WHERE c.chunk_id = ANY(%s)",
                        (missing,),
                    )
                ).fetchall()
            for row in extra:
                by_id[row["chunk_id"]] = Snippet(
                    chunk_id=row["chunk_id"],
                    doc_id=row["doc_id"],
                    title=row["title"],
                    heading=row["heading"],
                    body=row["body"],
                    source=row["source"],
                )
        rankings.append(dense)
    ranked = fuse(rankings)
    return _cap([by_id[key] for key in ranked if key in by_id])[:limit]
