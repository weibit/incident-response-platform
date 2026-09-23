# LiteLLM as shared LLM and MCP gateway

Status: approved design, 2026-09-22.

## Goal

Route both model calls and MCP tool calls through a single LiteLLM proxy, so the incident
cluster names roles rather than providers and the AI platform tier owns model selection,
tool entitlements and usage accounting.

This is the gateway tier the deployment diagram already specifies (`work/render_diagrams.py`:
"Shared LiteLLM gateway for models and MCP tools", proxy pods exposing both
`/v1/chat/completions` and `/mcp`). The starter currently implements neither: `llm.py` calls
`init_chat_model` directly, and the worker's MCP client connects straight to `runbooks:8080`.

## Non-goals

- No change to graph topology, node set, or the analyst's tool vocabulary.
- No change to `mock_mode` or the coupling it enforces between simulated evidence,
  simulated execution and the RCA rules. That coupling is an invariant in `AGENTS.md`.
- No virtual key provisioning, quota policy or budget enforcement. The infrastructure to
  support them is present; the policy is not configured.
- No live adapters for the six unimplemented evidence tools, and no mutation MCP service.
- No direct-to-server MCP fallback in the worker. The gateway is the only path.

## Decisions

| Decision | Choice |
|---|---|
| Gateway | One LiteLLM proxy serving both `/v1/chat/completions` and `/mcp`. |
| Role routing | App config holds gateway aliases; LiteLLM maps aliases to real models. |
| MCP path | Gateway only. The worker never connects to `runbooks:8080` again. |
| MCP auth | `x-litellm-api-key` to the gateway; runbooks' own bearer token passed through. |
| Deployment | Dedicated `litellm-postgres` plus `redis`, matching the diagram. |
| Upstream for tests | A stub OpenAI-compatible service returning real `tool_calls`. |
| Trust boundary | `allowed_tools` pins the exposed set; the server's registration is no longer the only guarantee. |

## Architecture

Three new Compose services form an AI platform tier:

- `litellm` — the proxy. Reads a config file declaring model aliases and `mcp_servers`.
- `litellm-postgres` — a separate PostgreSQL instance for keys, config and usage. Separate
  rather than a second database in the existing instance, so the incident schema keeps sole
  ownership of its own server and neither side's migrations touch the other.
- `redis` — shared rate limits and routing state.

`runbooks` is unchanged. It keeps its bearer token, its `allowed_hosts` list and its two
registered tools. It is simply no longer reachable from the worker; only LiteLLM calls it.

## LLM path

`Analyst` selects a model per role. Three settings hold gateway aliases, not provider names:

| Role | Setting | Default value | LiteLLM maps to |
|---|---|---|---|
| `triage()` | `LLM_MODEL_TRIAGE` | `incident-triage` | GPT-4o |
| `investigate()` | `LLM_MODEL_RCA` | `incident-rca` | GPT-5 |
| `plan()` | `LLM_MODEL_PLAN` | `incident-plan` | Claude Sonnet |

Each falls back to `LLM_MODEL` when unset, so an existing deployment that sets only
`LLM_MODEL` behaves exactly as before. `init_chat_model` targets the proxy's
OpenAI-compatible endpoint via `LITELLM_BASE_URL` and `LITELLM_API_KEY`.

The consequence worth stating plainly: changing which model handles RCA becomes a gateway
config change, not an application redeploy, and the application no longer names a provider
anywhere.

## MCP path

Three changes, all forced by LiteLLM's gateway semantics:

1. **URL.** `knowledge_mcp_url` points at the proxy's `/mcp`.
2. **Tool names.** LiteLLM namespaces tools as `<server_name>-<tool_name>`, so `TOOL_CALLS`
   becomes `runbooks-search_runbooks` and `runbooks-search_similar_incidents`.
3. **Headers.** `mcp_client` sends `x-litellm-api-key: Bearer <key>` for the gateway, and
   `x-mcp-runbooks-authorization: Bearer <token>` so the runbooks server still authenticates
   the call itself. The gateway fronts that boundary rather than replacing it.

`knowledge_allowed_hosts` needs no change: LiteLLM reaches the server as `runbooks:8080`,
which is already allow-listed.

`allowed_tools` in the gateway config pins the exposed set to exactly the two search tools.
This strengthens the existing invariant. Today the read-only guarantee rests entirely on
what `server.py` chooses to register; afterwards it is enforced at two independent layers,
and LiteLLM applies it at both `tools/list` and `tools/call`.

## Stub upstream

`with_structured_output` requires tool calling. LiteLLM's `mock_response` is documented only
for string content, with no documented support for returning `tool_calls`, so a mocked model
would likely fail the analyst's structured parsing for reasons unrelated to the gateway.

Instead a small OpenAI-compatible service implements `POST /v1/chat/completions` and returns
a `tool_calls` payload satisfying the requested schema, echoing back which upstream model
name it was called with. LiteLLM routes all three aliases to it.

This keeps test scaffolding out of production code. The alternative, passing
`method="json_mode"` into `with_structured_output`, would shape `llm.py` around a test.

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `LITELLM_BASE_URL` | `http://litellm:4000` | Proxy endpoint for model calls. |
| `LITELLM_API_KEY` | none | Gateway credential. Required whenever `KNOWLEDGE_ENABLED` is true or `MOCK_MODE` is false, validated the same way `KNOWLEDGE_MCP_TOKEN` already is. |
| `LLM_MODEL_TRIAGE` | falls back to `LLM_MODEL` | Alias for the triage role. |
| `LLM_MODEL_RCA` | falls back to `LLM_MODEL` | Alias for the investigation role. |
| `LLM_MODEL_PLAN` | falls back to `LLM_MODEL` | Alias for the planning role. |
| `KNOWLEDGE_MCP_URL` | `http://litellm:4000/mcp` | Changed from `http://runbooks:8080/mcp`. |
| `KNOWLEDGE_MCP_TOKEN` | unchanged | Now forwarded by the gateway to the runbooks server. |

## Failure handling

| Condition | Behavior |
|---|---|
| Gateway unreachable, MCP path | `ConnectionError` from the existing adapter translation, retried by the node's `RetryPolicy`. |
| Gateway unreachable, model path | Whatever `init_chat_model`'s client raises, subject to its own `max_retries=2`. Not unified with the MCP path, and not changed by this work. |
| Gateway rejects the key, MCP path | Should raise `PermissionError` via the adapter's existing 401/unauthorized check. Whether a LiteLLM rejection surfaces with a matching message is listed below as unverified. |
| Runbooks token rejected upstream | Surfaces through the gateway as an error result; the session adapter raises rather than reporting an empty corpus. |
| Redis unavailable | Rate limiting degrades; model and tool calls continue. |
| `litellm-postgres` unavailable | Proxy fails to start. It is a hard dependency of the gateway, not of retrieval. |

## Verification

1. `docker compose up --build -d` — every service healthy, including `litellm`.
2. A standalone script builds an `Analyst` from a non-mock config and calls `triage()`,
   `investigate()` and `plan()`, asserting each reached the stub under a different upstream
   model name. This is the per-role routing proof, and it runs without touching `mock_mode`.
3. `search_runbooks` through the gateway returns five fenced documents — the same assertion
   that passes today against the server directly.
4. The existing suites are unchanged: 67 unit tests, 20 integration tests, and the demo
   reaching `resolved` in mock mode.

## Known consequences

- Retrieval now requires LiteLLM running locally. There is no direct fallback.
- `LLM_MODEL` becomes a fallback default rather than the operative setting.
- `tests/test_knowledge_server.py` continues to test the server directly and is unaffected,
  but it no longer exercises the path the worker actually uses.

## To verify at implementation time

These are not yet confirmed against a running LiteLLM and must be settled before or during
the first task, the way the pgvector narrowing question was settled in the previous plan:

- The exact published image tag and its required environment variables.
- The config file keys for `mcp_servers`, `allowed_tools` and model aliases in the installed
  version, read from that version rather than from documentation.
- Whether the MCP client's `initialize` handshake succeeds through the proxy with the header
  scheme above, since the SDK sets its own headers on the transport.
- Whether a rejected gateway key surfaces through the MCP client with a message the
  adapter's existing 401/unauthorized check recognises. If not, that check needs widening
  rather than the error being left to burn the retry budget.

## Corrections from implementation (2026-09-22)

Three things the design asserted turned out to be wrong when run. Recorded here so the
design does not keep claiming them.

**The gateway holds the upstream credential; it does not forward the caller's.** The design
said `x-mcp-runbooks-authorization` would carry the runbooks token through per call. LiteLLM
connects to the upstream at its own startup to list tools, with no caller request in flight,
and got 401. The server entry now carries `auth_type: bearer_token` and an `os.environ/`
`authentication_token`. The security consequence is real and stated in `docs/operations.md`:
a leaked gateway key now reaches the corpus, which a pure pass-through would not.

**The MCP endpoint is `/mcp/`, with a trailing slash.** `/mcp` answers 307, and the MCP SDK
does not follow redirects on POST, so the handshake failed opaquely.

**`with_structured_output` does not need tool calls.** langchain-openai 1.6.4 defaults to
`method="json_schema"`, sending `response_format` and expecting JSON content. The stub
upstream is still justified, but for a different reason than the design gave: it records
which upstream model name each call arrived under, which is what makes per-role routing
observable rather than inferred. LiteLLM's `mock_response` could not show that.
