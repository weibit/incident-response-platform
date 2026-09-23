# Generic knowledge corpus

Status: approved design, 2026-09-22.

## Goal

Make retrieval work over arbitrary documents instead of one Markdown runbook format, without
changing what makes the current design safe: the agentic loop that decides when to retrieve,
the read-only MCP boundary, and replay-stable evidence identity.

Today `chunk_markdown` requires YAML frontmatter defining `id`, recognises only ATX headings,
and `load_corpus` globs `docs/runbooks/*.md`. Anything that is not a runbook in that exact
shape cannot be indexed at all.

## Non-goals

- No change to the agentic loop. The analyst still decides whether to retrieve; retrieval is
  never mandatory or up-front.
- No change to storage, fusion, snippet caps, or the hybrid lexical/dense split.
- No replacement of the chunk id recipe. It is load-bearing and stays exactly as it is.
- No reranking, query rewriting or multi-hop retrieval. Those are retrieval-quality work,
  and this is ingestion-breadth work. Keeping them separate keeps this change reviewable.
- No new MCP server, and no second retrieval path.

## Decisions

| Decision | Choice |
|---|---|
| Framework | `langchain-community` loaders + `langchain-text-splitters`. |
| Why not LlamaIndex | 40 net-new dependencies against 13, and a parallel stack to hold in mind. |
| Why not `unstructured` | 50 net-new, including spacy, numba and llvmlite into a slim image. |
| Corpus definition | A `corpus.yaml` manifest declaring sources, kinds and scoping. |
| `kind` | An open, corpus-defined label rather than the current two values. |
| Tool surface | One `knowledge` tool taking a `kind` argument. |
| Chunk identity | Unchanged: `sha256(f"{doc_id}:{heading}:{content}")[:32]`. |

### Dependency evidence

Resolved against the installed tree rather than estimated:

| Candidate | Total | Already present | Net new | Heavy additions |
|---|---|---|---|---|
| `langchain-community` | 49 | 36 | **13** | none |
| `llama-index-core` | 62 | 22 | 40 | networkx, nltk, pillow |
| `unstructured` | 71 | 21 | 50 | spacy, thinc, blis, numba, llvmlite |

LangChain reuses 36 because the project already depends on `langchain-core`, `langsmith`,
`pydantic`, `httpx`/`httpx2`, `pyyaml` and `requests`. `langchain-text-splitters` arrives
inside those 13, which covers format-aware splitting as well as loading.

The trade accepted: LangChain's loaders are thinner than LlamaHub's, and its PDF loaders
need a backend such as `pypdf`. That means paying one small dependency per format actually
needed, rather than importing an NLP toolchain on day one.

## Architecture

The format-specific surface today is two functions. Everything downstream is already generic,
and `ingest_resolved_incidents` proves it: that function builds `Document` and `Chunk`
directly from database rows with no Markdown involved. This design adds a third producer to
the same seam.

```
corpus.yaml ──> loaders/ ──> Document/Chunk ──> index.py ──> PostgreSQL ──> retrieve.py
                   ^                              ^
       LangChain loaders + splitters      unchanged, including the
       (new)                              chunk id recipe
```

### Loader layer

A new `knowledge/loaders/` package turns a declared source into `Document` objects:

- `markdown.py` keeps the existing parser unchanged. Runbooks with frontmatter behave
  exactly as they do today, including fence tracking and heading scoping.
- `generic.py` wraps a LangChain loader plus a text splitter for everything else, mapping
  the result onto `Chunk`.

Every loader returns the existing dataclasses. Nothing below the loader layer learns that
new formats exist.

### Corpus manifest

Frontmatter cannot be required of a PDF, so scoping metadata moves into a declaration:

```yaml
sources:
  - path: docs/runbooks/*.md
    kind: runbook
    loader: markdown          # frontmatter supplies id, title, service
  - path: docs/postmortems/*.pdf
    kind: postmortem
    service: checkout         # no frontmatter, so declared here
    doc_id: filename          # stem of the file
  - path: docs/architecture/*.md
    kind: architecture
    service: null             # global: eligible for every service
```

Rules:

- Frontmatter wins when present. A declaration supplies what the document cannot.
- `service: null` means global, matching the existing `d.service IS NULL` semantics.
- `doc_id: filename` uses the path stem. It must be unique across the corpus; ingestion
  already raises on duplicates and that check is extended to cover all sources.

`service` scoping is the one field that cannot be left to a default. A document indexed
with no service becomes eligible for every incident, so `service` is required in a source
declaration unless the loader can derive it or `null` is written explicitly. Silence is
rejected at load time rather than producing a corpus that quietly leaks across services.

## Tool surface

The eight read tools currently include `runbooks` and `similar_incidents` as distinct names.
They collapse into one:

```
knowledge(kind: str, query: str)
```

The available kinds are read from the manifest and written into the tool's description, so
the analyst can see what exists without the vocabulary being hardcoded.

`service` is deliberately absent from that signature. Today `read_tools` closes over the
incident's service and the model supplies only a query string, so the analyst cannot widen
its own scope. Collapsing to one tool must not become an excuse to let `service` in as an
argument: it stays injected from the incident, exactly as `tools.py:70-74` does now.

This is the option chosen over generating one tool per kind. Per-kind tools read better to
the model, but they would make `READ_TOOLS`, the `InvestigationStep.tool` literal and the
`OBSERVATIONAL` set all dynamic — three declarations that must agree, currently guaranteed
by a fixed partition. With a single `knowledge` tool the RCA rule stays a one-line truth:
`knowledge` is never observational, so retrieval alone still cannot establish a root cause.

Adding a corpus type then requires no code and no prompt change, only a manifest entry.

### This is a breaking change

Unlike the previous two features, this is not additive. Every place the old names appear:

| File | Change |
|---|---|
| `models.py:56-66` | `InvestigationStep.tool` literal drops both names, gains `knowledge` |
| `integrations/tools.py:16` | `READ_TOOLS` likewise |
| `integrations/tools.py:48` | Mock backend's simulated observations |
| `workflow/policy.py` | `OBSERVATIONAL` partition assertion follows |
| `integrations/knowledge_backend.py:10` | `TOOL_CALLS` maps one name, not two |
| `knowledge/server.py:49` | One MCP tool taking `kind`, not two tools |
| `integrations/llm.py:83` | The investigate prompt's tool list |
| `deploy/litellm/config.yaml:48` | `allowed_tools` lists one tool |

The mock investigator concludes on `topology`, `metrics` and `deployments`, so the graph
tests should not move. If they do, that is a finding: it would mean the mock path depends on
a retrieval tool name.

## Failure handling

| Condition | Behavior |
|---|---|
| A source matches no files | Fail ingestion loudly. A silently empty source is how a corpus withers unnoticed. |
| A loader cannot parse a file | Fail that document with its path named. Never skip silently. |
| A declaration omits `service` and the loader cannot derive one | Reject at load time. |
| Duplicate `doc_id` across sources | Reject, as the existing duplicate check already does within one source. |
| An unknown `kind` reaches the tool | Return an empty, untrusted result rather than erroring, matching how an empty search already behaves. |

## Verification

1. The existing runbook corpus indexes identically: same chunk count, same chunk ids. This
   is the proof that generalising did not disturb replay stability, and it is asserted
   against the ids already pinned by `test_chunk_id_recipe_is_pinned`.
2. A PDF and a frontmatter-less Markdown file index and retrieve.
3. Service scoping holds across formats: a document declared `service: checkout` does not
   surface for `payments`, and a `null` one surfaces for both.
4. `knowledge(kind="runbook")` returns what `runbooks` returned before.
5. An RCA citing only `knowledge` evidence is still rejected.
6. The demo reaches `resolved`, and the gateway verification still passes.

## Known consequences

- The analyst's tool vocabulary changes, so any saved prompt or checkpoint referring to
  `runbooks` or `similar_incidents` becomes stale. `evals/cases.jsonl` was checked and
  contains no references, so evals need no migration. In-flight LangGraph checkpoints
  written before the change may still carry the old names in their evidence.
- One tool with a `kind` argument gives the model a slightly weaker affordance than two
  named tools. The mitigation is the generated description listing available kinds.
- Per-format loader backends accumulate as dependencies over time. That is the accepted
  cost of not importing a single heavy toolchain up front.

## To verify at implementation time

- The `langchain-community` loader and splitter class names in the resolved version, read
  from the installed package rather than documentation.
- Whether `langchain-text-splitters`' Markdown splitter preserves fenced blocks well enough
  to replace nothing — the existing Markdown path is deliberately untouched, so this only
  matters for generic sources.
- That re-indexing the existing corpus produces byte-identical chunk ids, checked against
  the database before and after rather than assumed.
