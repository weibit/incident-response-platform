"""Context limits cover the final prompt and every summarization request."""

import asyncio
import copy
import json

import httpx
import pytest
from langchain_core.runnables import RunnableLambda
from langgraph.checkpoint.memory import InMemorySaver
from openai import BadRequestError
from pydantic import SecretStr, ValidationError

from incident_platform.integrations.context import (
    SUMMARY_SYSTEM,
    SUMMARY_TASK,
    ContextBudgetExceeded,
    EvidenceSummary,
    build_context,
    request_size,
)
from incident_platform.integrations.llm import Analyst, _model, _structured_model
from incident_platform.integrations.tools import MockEvidenceBackend
from incident_platform.models import Triage
from incident_platform.workflow.graph import build_graph


def sample(observations):
    return {
        "incident_id": "incident-1",
        "incident": {"service": "checkout", "severity": "high", "summary": "Errors"},
        "raw": {"summary": "PRIVATE DUPLICATE" * 10000},
        "plan": {"rationale": "STALE PLAN"},
        "approval": {"approved": True},
        "evidence": [
            {
                "evidence_id": f"e-{i}",
                "tool": "logs",
                "observation": text,
                "observed_at": "2026-09-23T00:00:00Z",
                "trusted": False,
                "simulated": False,
            }
            for i, text in enumerate(observations)
        ],
        "hypotheses": ["Deployment regression is possible"],
        "rounds": 2,
    }


@pytest.fixture
def limits(cfg):
    return cfg.model_copy(
        update={
            "context_window_tokens": 6000,
            "context_output_tokens": 256,
            "context_safety_tokens": 512,
            "context_summary_output_tokens": 256,
        }
    )


async def build(state, cfg, summarize, **kwargs):
    return await build_context(
        state,
        role=kwargs.pop("role", "rca"),
        system=kwargs.pop("system", "instructions"),
        task="Investigate",
        schema=Triage,
        cfg=cfg,
        summarize=summarize,
        **kwargs,
    )


async def forbidden(_):
    pytest.fail("No model call should be made")


async def test_small_context_has_no_summary_calls_and_filters_state(limits):
    state = sample(["Errors began after release"])
    original = copy.deepcopy(state)
    result = await build(state, limits, forbidden)
    view = json.loads(result.data)
    assert view["evidence"] == state["evidence"]
    assert "raw" not in view and "approval" not in view and "plan" not in view
    assert result.summary_calls == 0
    assert state == original


@pytest.mark.parametrize("body", ["error rate 12%. " * 900, "数据库异常。🚨" * 1100])
async def test_large_evidence_is_chunked_and_every_request_fits(limits, body):
    state = sample([body, "Recent deployment has not changed"])
    original = copy.deepcopy(state)
    calls = []

    async def summarize(data):
        calls.append(data)
        assert request_size(SUMMARY_SYSTEM, SUMMARY_TASK, data, EvidenceSummary) <= (
            limits.context_window_tokens
            - limits.context_summary_output_tokens
            - limits.context_safety_tokens
        )
        return EvidenceSummary(summary="Observed errors; cause uncertain.")

    result = await build(state, limits, summarize)
    view = json.loads(result.data)
    assert result.estimated_input_tokens <= (
        limits.context_window_tokens - limits.context_output_tokens - limits.context_safety_tokens
    )
    assert 1 < result.summary_calls == len(calls) <= limits.context_summary_max_calls
    assert view["evidence"][0]["evidence_id"] == "e-0"
    assert view["evidence"][0]["trusted"] is False
    assert view["evidence"][0]["context_representation"] == "llm_summary_not_original"
    assert len(view["evidence"][0]["original_sha256"]) == 64
    assert view["evidence"][1] == state["evidence"][1]
    assert state == original
    # The map calls cover the entire observation, including its final chunk.
    mapped = [json.loads(c)["text"] for c in calls[:-1]]
    assert "".join(mapped) == body


async def test_cited_evidence_cannot_be_summarized_to_force_a_plan(limits):
    state = sample(["critical original evidence " * 1000])
    state["rca"] = {"hypothesis": "h", "confidence": 0.9, "evidence_ids": ["e-0"]}
    with pytest.raises(ContextBudgetExceeded, match="Required context"):
        await build(state, limits, forbidden, role="plan")


async def test_old_uncited_evidence_compacts_but_cited_original_stays(limits):
    state = sample(["old logs " * 1200, "observed timeout"])
    state["rca"] = {"hypothesis": "h", "confidence": 0.9, "evidence_ids": ["e-1"]}

    async def summarize(_):
        return EvidenceSummary(summary="Old logs contain repetitive entries.")

    result = await build(state, limits, summarize, role="plan")
    view = json.loads(result.data)
    assert view["evidence"][1] == state["evidence"][1]
    assert view["rca"] == state["rca"]


async def test_instructions_schema_and_output_reserve_count(limits):
    state = sample([])
    with pytest.raises(ContextBudgetExceeded):
        await build(state, limits, forbidden, system="x" * limits.context_window_tokens)
    # Even a normal-sized state cannot consume the reserved output space.
    state["incident"]["summary"] = "x" * 5100
    with pytest.raises(ContextBudgetExceeded):
        await build(state, limits, forbidden)


async def test_call_limit_stops_huge_input(limits):
    limits.context_summary_max_calls = 2
    calls = []

    async def summarize(data):
        calls.append(data)
        return EvidenceSummary(summary="Observed errors.")

    with pytest.raises(ContextBudgetExceeded, match="budget exhausted"):
        await build(sample(["x" * 50000]), limits, summarize)
    assert len(calls) == 2


@pytest.mark.parametrize("kind", ["exception", "invalid", "empty", "no_reduction"])
async def test_bad_summary_does_not_fall_back_to_full_state(limits, kind):
    async def summarize(data):
        if kind == "exception":
            raise RuntimeError("sensitive provider response")
        if kind == "invalid":
            return {"summary": "x", "evidence_id": "invented"}
        if kind == "empty":
            return EvidenceSummary(summary="   ")
        # Return more UTF-8 bytes than the original using a short valid string.
        return EvidenceSummary(summary="🚨" * 1900)

    with pytest.raises(ContextBudgetExceeded) as error:
        await build(sample(["x" * 6000]), limits, summarize)
    assert "sensitive" not in str(error.value)


async def test_summary_timeout_and_cancellation(limits):
    limits.context_summary_timeout_seconds = 0.01

    async def slow(_):
        await asyncio.sleep(1)

    with pytest.raises(ContextBudgetExceeded, match="summarization failed"):
        await build(sample(["x" * 6000]), limits, slow)

    async def cancelled(_):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await build(sample(["x" * 6000]), limits, cancelled)


async def test_hypothesis_history_is_summarized_without_changing_state(limits):
    state = sample([])
    state["hypotheses"] = ["Possible causes " * 1000]
    original = copy.deepcopy(state)

    async def summarize(_):
        return EvidenceSummary(summary="Several causes remain possible.")

    result = await build(state, limits, summarize)
    assert "hypotheses_summary" in json.loads(result.data)
    assert state == original


@pytest.mark.parametrize("stage", ["triage", "investigate", "plan"])
async def test_context_failure_routes_to_manual_without_execution(cfg, store, incident, stage):
    analyst = Analyst(cfg)
    calls = []

    async def fail(state):
        calls.append(stage)
        raise ContextBudgetExceeded("unsafe oversized context")

    setattr(analyst, stage, fail)
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": incident["incident_id"]}}
    graph = build_graph(cfg, store, analyst, MockEvidenceBackend(), saver)
    state = await graph.ainvoke(incident, config)
    assert state["status"] == "manual_review"
    assert state["context_error"] == f"context_unavailable:{stage}"
    assert calls == [stage]
    assert not store.executions and not store.approvals
    assert store.events[-1] == (
        "incident.manual_review",
        {"reason": f"context_unavailable:{stage}"},
    )
    assert (await graph.aget_state(config)).values["evidence"] == state["evidence"]
    assert not (await graph.aget_state(config)).next


async def test_live_path_uses_bounded_context_and_summary_role(limits, monkeypatch):
    built = []
    inputs = []

    class Model:
        def with_structured_output(self, schema, **kwargs):
            def invoke(prompt):
                text = prompt.to_messages()[-1].content
                inputs.append((schema, text, kwargs))
                if schema is EvidenceSummary:
                    return EvidenceSummary(summary="Errors observed; cause uncertain.")
                return Triage(severity="high", investigate=True, reason="Investigate errors")

            return RunnableLambda(invoke)

    def model(cfg, alias):
        built.append(alias)
        return Model()

    monkeypatch.setattr("incident_platform.integrations.llm._model", model)
    limits.mock_mode = False
    analyst = Analyst(limits)
    state = sample(["large private observation " * 1000])
    original = copy.deepcopy(state)
    result = await analyst.structured("triage", Triage, "Assess", state)
    assert result.investigate is True
    assert built[-1] == limits.llm_model_summary
    final = json.loads(inputs[-1][1].split("Incident data: ", 1)[1])
    assert "raw" not in final
    assert final["evidence"][0]["context_representation"] == "llm_summary_not_original"
    assert state == original
    assert all(
        kwargs["max_completion_tokens"] == limits.context_summary_output_tokens
        for schema, _, kwargs in inputs
        if schema is EvidenceSummary
    )


def test_actual_model_binds_output_limit_and_summary_override(cfg):
    cfg.litellm_api_key = SecretStr("test")
    model = _model(cfg, "incident-triage")
    assert model.max_tokens == cfg.context_output_tokens
    bound = _structured_model(
        model, EvidenceSummary, {}, output_tokens=cfg.context_summary_output_tokens
    ).steps[0]
    assert bound.kwargs["max_completion_tokens"] == cfg.context_summary_output_tokens


async def test_provider_context_rejection_becomes_nonretryable(cfg):
    class Model:
        def with_structured_output(self, *_args, **_kwargs):
            def fail(_):
                raise BadRequestError(
                    "too large",
                    response=httpx.Response(400, request=httpx.Request("POST", "http://gateway")),
                    body={"code": "context_length_exceeded"},
                )

            return RunnableLambda(fail)

    analyst = Analyst(cfg)
    analyst.models["triage"] = Model()
    with pytest.raises(ContextBudgetExceeded, match="Provider rejected"):
        await analyst.structured("triage", Triage, "Assess", sample([]))


def test_invalid_budget_configuration_is_rejected(cfg):
    with pytest.raises(ValidationError, match="Context window"):
        type(cfg).model_validate({**cfg.model_dump(), "context_window_tokens": 4096})


async def test_wire_output_limit_and_trace_survive_summary_binding(cfg, monkeypatch):
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_openai.chat_models.base import BaseChatOpenAI

    from incident_platform.observability import trace_id_for

    class Captured(Exception):
        pass

    payload = {}
    original = BaseChatOpenAI._get_request_payload

    def capture(self, input_, *, stop=None, **kwargs):
        payload.update(original(self, input_, stop=stop, **kwargs))
        raise Captured

    monkeypatch.setattr(BaseChatOpenAI, "_get_request_payload", capture)
    cfg.litellm_api_key = SecretStr("test")
    chain = ChatPromptTemplate.from_messages([("human", "{data}")]) | _structured_model(
        _model(cfg, cfg.llm_model_summary),
        EvidenceSummary,
        {"incident_id": "i", "input_event_id": "e"},
        output_tokens=cfg.context_summary_output_tokens,
    )
    with pytest.raises(Captured):
        await chain.ainvoke({"data": "hello"})
    assert payload["max_completion_tokens"] == cfg.context_summary_output_tokens
    assert payload["extra_body"]["metadata"]["existing_trace_id"] == trace_id_for("i", "e")


async def test_full_graph_compacts_then_preserves_originals_in_checkpoint(
    limits, store, incident, monkeypatch
):
    from incident_platform.models import InvestigationStep, PlanDraft

    original_observation = "Topology observation " * 1000
    seen = []

    class Model:
        def with_structured_output(self, schema, **kwargs):
            def invoke(prompt):
                data = json.loads(prompt.to_messages()[-1].content.split("Incident data: ", 1)[1])
                if schema is EvidenceSummary:
                    return EvidenceSummary(summary="Checkout depends on payments.")
                seen.append((schema, data))
                if schema is Triage:
                    return Triage(severity="high", investigate=True, reason="Investigate")
                if schema is InvestigationStep:
                    evidence = data["evidence"]
                    tools = {e["tool"] for e in evidence}
                    for tool in ("metrics", "deployments"):
                        if tool not in tools:
                            return InvestigationStep(hypothesis="Possible regression", tool=tool)
                    return InvestigationStep(
                        hypothesis="Release regression",
                        conclusion={
                            "hypothesis": "Release regression",
                            "confidence": 0.9,
                            "evidence_ids": [
                                e["evidence_id"] for e in evidence if e["tool"] != "topology"
                            ],
                        },
                    )
                assert schema is PlanDraft
                return PlanDraft(
                    action="rollback_deployment",
                    target="checkout",
                    rationale="Regression",
                    rollback="Restore previous release",
                    verification="Observe metrics",
                )

            return RunnableLambda(invoke)

    class Backend(MockEvidenceBackend):
        async def query(self, tool, service, query, kind=""):
            evidence = await super().query(tool, service, query, kind)
            changes = {"simulated": False}
            if tool == "topology":
                changes["observation"] = original_observation
            return evidence.model_copy(update=changes)

    monkeypatch.setattr("incident_platform.integrations.llm._model", lambda cfg, alias: Model())
    limits.mock_mode = False
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": incident["incident_id"]}}
    graph = build_graph(limits, store, Analyst(limits), Backend(), saver)
    state = await graph.ainvoke(incident, config)
    assert state["__interrupt__"]  # Approval still required after compaction.
    assert state["evidence"][0]["observation"] == original_observation
    assert (await graph.aget_state(config)).values["evidence"] == state["evidence"]
    assert {schema for schema, _ in seen} == {Triage, InvestigationStep, PlanDraft}
    assert all(
        view["evidence"][0]["context_representation"] == "llm_summary_not_original"
        for _, view in seen
    )
    assert not store.executions


async def test_concurrent_context_builds_do_not_share_state(limits):
    async def summarize(data):
        await asyncio.sleep(0)
        text = json.loads(data)["text"]
        return EvidenceSummary(summary="alpha" if "alpha" in text else "beta")

    states = [sample([word * 1800]) for word in ("alpha", "beta")]
    before = copy.deepcopy(states)
    results = await asyncio.gather(*(build(s, limits, summarize) for s in states))
    assert "beta" not in results[0].data
    assert "alpha" not in results[1].data
    assert states == before
