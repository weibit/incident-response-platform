# Self-hosted Langfuse tracing for the incident workflow

Status: approved design, 2026-09-23.

## Goal

Make every incident's model and graph behaviour inspectable in a self-hosted Langfuse
deployment, and make each trace addressable by an identifier any component can recompute,
so evaluation can attach scores to it without storing a mapping.

The starter is most of the way there and stops just short of usable. `telemetry.graph_config`
already attaches `langfuse.langchain.CallbackHandler` with session and correlation metadata,
and `worker/main.py` already flushes on shutdown. Three things are missing:

1. Nothing knows a trace's identifier. `integrations/evaluation.py:record_score` takes a
   `trace_id` that no caller can supply, so the evaluation hooks are unreachable code.
2. Nothing initialises the Langfuse client explicitly, so the SDK's `mask` hook — a
   constructor-only parameter — can never be set. Prompts reach the trace store unredacted.
3. Traces are declared external in `README.md`, which conflicts with sending evidence bodies
   to them. Self-hosting resolves the conflict rather than working around it.

## Non-goals

- No operational metrics plane. Inbox backlog, DLQ depth, policy denials, approval age and
  checkpoint failures stay out of scope; `docs/operations.md` continues to name them as
  signals an operator wires up, and Langfuse is the wrong instrument for them. Dropped
  deliberately, not deferred.
- No scores, no LLM-as-judge, no datasets, no dataset runs. Those are a separate spec that
  depends on this one for trace identity. `integrations/evaluation.py` is not edited here.
- No change to graph topology, the node set, the analyst's tool vocabulary, `mock_mode`, or
  any policy rule. Tracing observes the workflow; it never participates in it.
- No prompt management via Langfuse. `evaluation.load_prompt` stays unused.
- No sampling or retention policy beyond documenting that ClickHouse growth is the operator's
  responsibility, consistent with the existing retention language.

## Decisions

| Decision | Choice |
|---|---|
| Deployment | Self-hosted, in `compose.yaml` behind an `observability` profile, off by default. |
| Trace payload | Full fidelity. Evidence, ticket and runbook bodies are traced verbatim. |
| Masking | Exact-value redaction of the configured secrets only. No heuristics. |
| Trace identity | Derived: `create_trace_id(seed=f"{incident_id}:{event_id}")`. Never stored. |
| Trace granularity | One trace per delivered event, grouped into one session per incident. |
| Trace sources | Both. Worker via the LangChain handler; gateway via LiteLLM's Langfuse callback, nested into the same trace. |
| Credentials | Read from env by the SDK. Not added to `Settings`, not in the distinct-secrets check. |
| Verification | Unit tests against a fake client; the live stack verified by hand in `docs/verification.md`. |

## Architecture

The `observability` profile adds six services. None of them are reachable from, or required
by, the default `docker compose up`.

- `langfuse-web` — UI and ingestion API. The worker and `litellm` both export to it.
- `langfuse-worker` — Langfuse's own async ingestion worker.
- `clickhouse` — trace storage.
- `langfuse-postgres` — Langfuse's transactional database. Separate from both the incident
  database and `litellm-postgres`, so no schema shares an instance with another tier's
  migrations. This follows the precedent `litellm-postgres` already set.
- `langfuse-minio` — S3-compatible blob storage for event payloads.
- `langfuse-redis` — Langfuse's queue. Deliberately *not* the existing `redis`, which serves
  LiteLLM; two tiers sharing one keyspace is an avoidable coupling.

Project API keys are provisioned headlessly through the image's `LANGFUSE_INIT_*` variables,
so the stack comes up already usable and the existing `init` service pattern still holds. No
UI click-through is part of bringing the platform up.

## Trace identity

`Langfuse.create_trace_id(seed=...)` (verified present in langfuse 3.15.0) makes the trace
identifier a pure function of data the platform already carries:

    trace_id = create_trace_id(seed=f"{incident_id}:{event_id}")

Both values already exist at the call site in `worker/main.py:process_event`, travel on every
`Envelope`, and `input_event_id` is persisted in graph state, so the identifier survives
worker restarts, checkpoint replays and approval resumes. Consequences:

- No new table, column or state field, and no write ordering to get wrong.
- Any component can recompute it. The judge in the follow-on spec needs no handoff.
- A **retry** of the same event recomputes the same identifier, so replayed observations
  append to the existing trace instead of creating a near-duplicate. This is deliberate: it
  keeps any future score attachment idempotent. The visible cost is that a retried event's
  trace shows both attempts in one tree.
- An **approval resume** is a different `event_id`, so it is its own trace, grouped with the
  original by `session_id = incident_id`.

`CallbackHandler` accepts `trace_context={"trace_id": ...}` as a documented parameter, so the
handler binds to this identifier directly. No hand-rolled wrapper span is needed; an earlier
draft of this design had one and it was removed as redundant.

`graph_config` therefore takes `event_id` in addition to its current arguments, and constructs
the handler with `update_trace=True`. That flag promotes the graph invocation's input and
output to trace level, which is what makes the trace list readable in the UI and is a hard
requirement for the trace-based dataset runs in the follow-on spec. Session, user and tag
attributes are applied from metadata independently of that flag, so the existing
`langfuse_session_id` behaviour is unaffected.

## Gateway nesting

`Analyst.structured` derives the same trace identifier from `state["incident_id"]` and
`state["input_event_id"]` via `telemetry.trace_id_for`, and binds it per call:

    model.bind(
        extra_body={"metadata": {"existing_trace_id": trace_id}}
    ).with_structured_output(schema)

The key is written provisionally as `existing_trace_id`. Task 1 confirms the name the proxy
actually honours and the spec is corrected to match; nothing else in this design depends on
which name it turns out to be.

`extra_body` is a model field that `langchain_openai` merges into the request payload as
`{**self._default_params, **kwargs}`, and `_model()` sets no `extra_body` of its own, so a
bound value lands in the request body with nothing to clobber. `Analyst.structured` keeps its
current signature; no trace plumbing is threaded through the graph.

`deploy/litellm/config.yaml` enables the Langfuse success callback so the proxy reports each
generation — with the cost it actually billed — into that same trace.

**This is the one unverified link.** `litellm` is a container image, absent from the local
environment, so the metadata key the proxy honours for attaching to an existing trace cannot
be confirmed from the repository. It is a Task 1 verification gate. If propagation does not
work, the fallback is two deliberate planes: worker traces for behaviour, gateway traces in a
separate Langfuse project for cost, correlated by `incident_id` metadata and not nested. Take
the fallback and report it; do not force a resolution.

## Masking

`Langfuse(mask=...)` is constructor-only, so the worker initialises the client once in
`main()` via `telemetry.init_tracing(cfg)`. `get_client()` then returns that configured
singleton, which is what makes the bare `get_client()` calls in `integrations/evaluation.py`
safe to use later.

The mask redacts exact occurrences of the secrets the process already holds — the five
`SecretStr` credentials in `config.py`, plus `litellm_api_key` and `knowledge_mcp_token` —
and `Authorization: Bearer ...` shaped substrings. It walks nested dicts and lists and returns
JSON-serialisable data.

It does **not** attempt entropy scoring or PII regexes. Those false-positive on log lines,
stack traces and ticket text, and no test can pin their behaviour. Evidence bodies pass
through verbatim; that is what self-hosting bought. A test asserts this explicitly, as a
regression guard against a later "improvement" into heuristic redaction.

The SDK substitutes `"<fully masked due to failed mask function>"` and logs if a mask raises,
so a buggy mask fails closed: it degrades a trace, and cannot leak or break an incident.

## Configuration

`LANGFUSE_ENABLED`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` and `LANGFUSE_BASE_URL` are
already in `.env.example`. The base URL's default changes to the in-profile service, and a
`LANGFUSE_INIT_*` block is added for headless provisioning. The keys stay out of `Settings`
and out of `validate_credentials`.

A mistyped Langfuse key therefore surfaces as a failed export, not a refused startup. This is
intentional: `docs/operations.md` states core correctness must not depend on trace delivery, so
blocking worker boot on a telemetry credential would invert that priority. `init_tracing` logs
a structured warning when tracing is enabled and the client cannot authenticate.

## Failure handling

Handler construction currently sits on the incident-processing path, where an import error or
a constructor failure would fail every incident. `graph_config` wraps it so any failure
degrades to `callbacks: []` and logs once. Tracing is never a reason an incident does not get
processed. The existing `flush()` on worker shutdown is unchanged; traces lost to a hard kill
are acceptable by the same principle.

## Changes by file

| File | Change |
|---|---|
| `src/incident_platform/telemetry.py` | Add `build_mask(cfg)`, `init_tracing(cfg)`, `trace_id_for(incident_id, event_id)`. `graph_config` gains `event_id`, `update_trace=True`, and degrade-on-failure. Grows from 42 to roughly 100 lines; purpose unchanged. |
| `src/incident_platform/integrations/llm.py` | `structured()` binds `extra_body` carrying the derived trace id. Signature unchanged. |
| `src/incident_platform/worker/main.py` | Call `init_tracing(cfg)` in `main()`; pass `event.event_id` to `graph_config`. |
| `deploy/litellm/config.yaml` | Langfuse success callback and its environment. |
| `compose.yaml` | The `observability` profile and its six services. |
| `.env.example` | `LANGFUSE_BASE_URL` default plus the `LANGFUSE_INIT_*` block. |
| `tests/test_telemetry.py` | New. |
| `tests/test_llm_roles.py` | One case for the bound trace metadata. |
| `src/incident_platform/integrations/evaluation.py` | None. |

## Verification

`tests/test_telemetry.py`, requiring no Langfuse server:

- the mask redacts every configured secret across nested dicts, lists and strings, and returns
  JSON-serialisable data;
- the mask leaves an evidence body byte-identical;
- trace identifiers are stable for a given `(incident_id, event_id)` and differ across events;
- `graph_config` produces no callbacks when disabled, and degrades to no callbacks rather than
  raising when handler construction fails;
- `langfuse_session_id` equals the incident id.

`tests/test_llm_roles.py` asserts each role's bound `extra_body` carries the derived identifier.

By hand, recorded in `docs/verification.md`: bring up the `observability` profile, run
`scripts/demo.py`, and confirm one trace per delivered event, LLM generations nested under the
graph tree, gateway-reported cost present on the generation, both traces of an approved
incident grouped in one session, and a deliberately planted secret redacted in the trace.

## Documentation changes

Three of these correct statements that this spec makes false:

- `AGENTS.md` — "Do not log credentials or raw evidence" is amended to distinguish sinks:
  credentials never, anywhere; raw evidence may reach an in-boundary trace store, never an
  external one. A new invariant is added: trace delivery must never affect incident
  correctness. Without this amendment the spec silently contradicts a stated invariant.
- `README.md:235` — "No extra local analytics/clickhouse/object-storage stack is needed" is now
  false and becomes a description of the opt-in profile. `README.md:5` and the "External
  Langfuse" node in the diagram at `README.md:30` change accordingly.
- `docs/operations.md:87` — keeps "Langfuse is optional", and gains the profile and a ClickHouse
  retention note consistent with the existing operator-responsibility language.

## Known consequences

- The default `docker compose up` is unchanged, but the full observability path costs six more
  services and a ClickHouse instance on a developer machine.
- Traces hold unredacted evidence. That is safe only while Langfuse is in-boundary, which makes
  "do not point `LANGFUSE_BASE_URL` at an external host" a deployment invariant rather than a
  preference.
- A retried event produces one trace containing both attempts.
- Gateway cost attribution depends on the unverified propagation link. Under the fallback,
  behaviour and cost are visible in two projects rather than one tree.

## To verify at implementation time

1. The metadata key LiteLLM's Langfuse integration honours for attaching to an existing trace,
   and whether the proxy forwards `extra_body` contents into it. Fallback stated above.
2. The exact `LANGFUSE_INIT_*` variable set and a pinned server image tag. The local
   environment proves the 3.15.0 SDK only, not the server.
3. Whether the SDK's export path can raise into caller code when the collector is unreachable,
   and whether the client exposes an authentication check usable at startup.
4. That `update_trace=True` does not overwrite the session attributes set from metadata.
