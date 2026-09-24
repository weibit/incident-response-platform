"""Explicit opt-in hooks. Redact inputs before sending them to an external Langfuse deployment."""

from langfuse import get_client


def load_prompt(name: str, fallback: str) -> str:
    return get_client().get_prompt(name, fallback=fallback, label="production").compile()


def record_score(
    trace_id: str,
    name: str,
    value: float | str,
    comment: str = "",
    score_id: str | None = None,
    data_type: str | None = None,
):
    """Attach one score to a trace.

    A score must carry a trace_id: the server silently drops scores that name only a session,
    and silently drops scores naming a trace that does not exist. score_id is an upsert key,
    so a redelivered event overwrites its previous scores instead of appending.
    """
    get_client().create_score(
        trace_id=trace_id,
        name=name,
        value=value,
        comment=comment,
        score_id=score_id,
        data_type=data_type,
    )


def add_dataset_case(dataset: str, case_id: str, inputs: dict, expected: dict):
    client = get_client()
    client.create_dataset(name=dataset)
    client.create_dataset_item(
        dataset_name=dataset, id=case_id, input=inputs, expected_output=expected
    )
