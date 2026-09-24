"""Pure scoring helpers for the judge service.

Deterministic scores restate facts the workflow already produced. A score whose source is
absent is omitted rather than defaulted: a missing score and a false score mean different
things, and recording the second for the first would corrupt every aggregate over it.
"""

from typing import NamedTuple
from uuid import NAMESPACE_URL, uuid5

VERDICT_VALUES = {"supported": 1.0, "partially_supported": 0.5, "unsupported": 0.0}


class Score(NamedTuple):
    name: str
    value: float | str
    data_type: str
    comment: str = ""


def score_id_for(incident_id: str, name: str) -> str:
    """Derive a score's identity so a redelivered event overwrites instead of duplicating.

    Nested rather than a joined string: a flat "{incident_id}:{name}" seed collides whenever
    the two arguments' concatenation is ambiguous, which would silently merge two distinct
    scores into one upsert.
    """
    return str(uuid5(uuid5(NAMESPACE_URL, incident_id), name))


def verdict_value(label: str) -> float:
    return VERDICT_VALUES[label]


def deterministic_scores(state: dict) -> list[Score]:
    scores = []
    if "verified" in state:
        scores.append(Score("outcome_verified", int(bool(state["verified"])), "BOOLEAN"))
    if "status" in state:
        scores.append(Score("outcome_status", state["status"], "CATEGORICAL"))
    if "rounds" in state:
        scores.append(Score("investigation_rounds", state["rounds"], "NUMERIC"))
    if "rca_valid" in state:
        scores.append(Score("rca_citations_valid", int(bool(state["rca_valid"])), "BOOLEAN"))
    return scores


def cited_evidence(state: dict) -> list[dict]:
    """Only the evidence the RCA actually cites.

    Uncited evidence is irrelevant to whether the stated conclusion follows, and passing it
    would ask the judge a different question.
    """
    cited = set((state.get("rca") or {}).get("evidence_ids") or [])
    return [e for e in state.get("evidence") or [] if e.get("evidence_id") in cited]
