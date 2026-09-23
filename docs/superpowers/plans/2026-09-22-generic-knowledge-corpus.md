# Generic Knowledge Corpus Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Retrieve over arbitrary documents instead of one Markdown runbook format, without changing the agentic loop, the read-only MCP boundary, or replay-stable evidence identity.

**Architecture:** A `knowledge/loaders/` package turns sources declared in `corpus.yaml` into the existing `Document`/`Chunk` dataclasses. Markdown keeps its current parser untouched; other formats go through LangChain loaders and splitters. Everything below the loader layer is unchanged, including the chunk id recipe. The eight read tools collapse to seven as `runbooks` and `similar_incidents` become one `knowledge` tool taking a `kind`.

**Tech Stack:** `langchain-text-splitters`, `pypdf`, PostgreSQL with pgvector, `mcp` 2.2.0. `langchain-community` was evaluated and rejected in Task 1: it is sunset, and its loaders are not needed once `pypdf` is used directly.

**Design:** `docs/superpowers/specs/2026-09-22-generic-knowledge-corpus-design.md`

---

## File Structure

| File | Responsibility |
|---|---|
| `src/incident_platform/knowledge/corpus.py` | Parse and validate `corpus.yaml` into source declarations. Owns the scoping rules. |
| `src/incident_platform/knowledge/loaders/__init__.py` | Dispatch a source declaration to its loader; return `Document` objects. |
| `src/incident_platform/knowledge/loaders/markdown.py` | Thin wrapper over the existing `chunk_markdown`. No parsing changes. |
| `src/incident_platform/knowledge/loaders/generic.py` | LangChain loader + splitter, mapped onto `Document`/`Chunk` with the existing id recipe. |
| `src/incident_platform/knowledge/chunker.py` | Unchanged. Gains one exported helper for the id recipe so loaders cannot drift from it. |
| `src/incident_platform/knowledge/index.py` | `load_corpus` reads the manifest instead of globbing one directory. |
| `src/incident_platform/knowledge/retrieve.py` | Unchanged. |
| `src/incident_platform/knowledge/server.py` | One `search_knowledge(kind, service, query)` tool. |
| `src/incident_platform/models.py` | Tool literal drops two names, gains `knowledge`; `InvestigationStep` gains `kind`. |
| `src/incident_platform/integrations/tools.py` | `READ_TOOLS`, the protocol's `kind`, mock observations, and the knowledge tool's schema. |
| `src/incident_platform/integrations/knowledge_backend.py` | One entry in `TOOL_CALLS`; passes `kind` through. |
| `src/incident_platform/workflow/graph.py` | Tool payload carries `kind`. |
| `src/incident_platform/workflow/policy.py` | `OBSERVATIONAL` partition assertion follows the new vocabulary. |
| `corpus.yaml` | The manifest. |
| `deploy/litellm/config.yaml` | `allowed_tools` lists the one tool. |

Tests: `tests/test_corpus_manifest.py`, `tests/test_loaders.py`, plus edits to `tests/test_knowledge_retrieve.py`, `tests/test_knowledge_backend.py`, `tests/test_policy_rca.py`, `tests/test_workflow.py`.

---

## Task 1: Settle the LangChain loader surface

Nothing ships from this task but a recorded table. It exists because two earlier plans in this repository asserted an API from documentation and were wrong: `structuredContent` silently returned an empty corpus, and `mcp_servers` was specified as a list when it is a dict.

**Files:**
- Modify: `pyproject.toml`
- Modify: this plan file (Step 4)

- [x] **Step 1: Add the dependencies**

In `pyproject.toml`, extend the dependencies list:

```toml
  "langchain-openai>=1,<2", "langchain-text-splitters>=1,<2", "pypdf>=6,<7",
```

Then:

```bash
uv sync
```

Expected: resolution succeeds. If it fails on a `langchain-core` conflict, stop and report the conflicting pins rather than forcing a resolution.

- [x] **Step 2: Confirm the class names against the installed packages**

```bash
uv run python -c "
import importlib.metadata as md
for p in ('langchain-community','langchain-text-splitters','pypdf'):
    print(p, md.version(p))
from langchain_text_splitters import RecursiveCharacterTextSplitter
import inspect
print('splitter signature:', inspect.signature(RecursiveCharacterTextSplitter.__init__))
print('split_text:', hasattr(RecursiveCharacterTextSplitter, 'split_text'))
from langchain_community.document_loaders import PyPDFLoader, TextLoader
print('PyPDFLoader.load:', inspect.signature(PyPDFLoader.load))
print('TextLoader.load:', inspect.signature(TextLoader.load))
"
```

Record: the resolved versions, whether `RecursiveCharacterTextSplitter.split_text` returns `list[str]`, and what a loader's `load()` returns.

- [x] **Step 3: Confirm what a loaded document carries**

```bash
uv run python -c "
from langchain_community.document_loaders import TextLoader
import tempfile, pathlib
p = pathlib.Path(tempfile.mkdtemp()) / 'sample.txt'
p.write_text('First paragraph.\n\nSecond paragraph.')
docs = TextLoader(str(p)).load()
print('count:', len(docs))
print('type:', type(docs[0]).__name__)
print('attrs:', [a for a in ('page_content','metadata') if hasattr(docs[0], a)])
print('metadata keys:', list(docs[0].metadata))
"
```

Expected: one document with `page_content` and `metadata`. **If `page_content` is not the attribute name, stop and report** — every mapping in Task 4 depends on it.

- [x] **Step 4: Record the facts in this file**

Append a table to this task listing resolved versions, the splitter class and method, the loader classes, and the document attribute names. Later tasks read that table rather than re-deriving it.

**Verified facts** (measured against the installed packages on 2026-09-22; final footprint is **one** net-new package, `pypdf`, measured by diffing `uv.lock` against `main`. An earlier resolution that included `langchain-community` added six; that package was then removed as sunset. `langchain-core` stayed pinned at `1.6.4`, no existing package version changed):

| Fact | Value |
|---|---|
| `langchain-community` | **Rejected and removed.** `0.4.2` emits a `DeprecationWarning` on import: it is sunset and no longer actively maintained. Its loaders wrap work `pypdf` does directly, so nothing was lost. |
| `langchain-text-splitters` version | `1.1.2` |
| `pypdf` version | `6.19.0` |
| Splitter class | `langchain_text_splitters.RecursiveCharacterTextSplitter` |
| Splitter method used | `.split_text(text: str)` → confirmed by direct call to return `list[str]` (not just by signature) |
| Text reading | `Path.read_text()`. No loader class needed. |
| PDF reading | `pypdf.PdfReader(path).pages`, each page's `extract_text()`. Verified to work on a generated PDF. |
| Loader `.load()` return type | `list[Document]` where `Document` is `langchain_core.documents.base.Document` (confirmed for both `TextLoader` and `PyPDFLoader` by instantiating each and loading a real file) |
| Document attribute names | `page_content` (str) and `metadata` (dict) — both confirmed present via `hasattr` and by reading real values |
| `TextLoader` document count for a 2-paragraph file | 1 document; `page_content` holds the full file text verbatim; `metadata` = `{'source': '<abs path>'}` |
| `PyPDFLoader` document count for a 1-page blank PDF | 1 document (one `Document` per page); `metadata` keys observed: `producer`, `creator`, `creationdate`, `source`, `total_pages`, `page`, `page_label` |

- [x] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock docs/superpowers/plans/2026-09-22-generic-knowledge-corpus.md
git commit -m "build: add LangChain loaders and record the verified surface"
```

---

## Task 2: Share the chunk id recipe

Loaders must not each re-implement the hash. One function, one recipe, one test.

**Files:**
- Modify: `src/incident_platform/knowledge/chunker.py`
- Test: `tests/test_knowledge_chunker.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_knowledge_chunker.py`:

```python
def test_chunk_id_helper_matches_the_markdown_parser():
    # Every loader must produce ids the same way, or the same content indexed through two
    # paths would occupy two rows and evidence ids would stop being stable across a
    # re-index. One recipe, exported, used by all of them.
    from incident_platform.knowledge.chunker import chunk_id, chunk_markdown

    document = chunk_markdown(
        "---\nid: d1\ntitle: T\nservice: checkout\n---\n\n# Heading\n\nBody text.\n",
        source="docs/runbooks/d1.md",
    )
    only = document.chunks[0]
    assert chunk_id("d1", only.heading, only.body) == only.chunk_id
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_knowledge_chunker.py -k chunk_id_helper -v`
Expected: FAIL with `ImportError: cannot import name 'chunk_id'`.

- [ ] **Step 3: Write the implementation**

In `src/incident_platform/knowledge/chunker.py`, add above `chunk_markdown`:

```python
def chunk_id(doc_id: str, heading: str, content: str) -> str:
    """The one chunk id recipe.

    Hashes content and heading only. Not the file path, so renaming does not churn ids;
    not the position, so inserting a section above does not churn every id below it.
    Exported because every loader must use it: two recipes would mean the same content
    indexed twice, and evidence ids that move when they should not.
    """
    return hashlib.sha256(f"{doc_id}:{heading}:{content}".encode()).hexdigest()[:32]
```

Then in `chunk_markdown`, replace the inline hash with a call to it. The existing comment
above that line moves into the function above and is not duplicated.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_knowledge_chunker.py -v`
Expected: PASS, including `test_chunk_id_recipe_is_pinned`, which proves the refactor did
not change a single id.

- [ ] **Step 5: Commit**

```bash
git add src/incident_platform/knowledge/chunker.py tests/test_knowledge_chunker.py
git commit -m "refactor(knowledge): export the chunk id recipe"
```

---

## Task 3: Corpus manifest

**Files:**
- Create: `src/incident_platform/knowledge/corpus.py`
- Create: `corpus.yaml`
- Test: `tests/test_corpus_manifest.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_corpus_manifest.py`:

```python
import pytest

from incident_platform.knowledge.corpus import CorpusError, Source, load_manifest

MANIFEST = """
sources:
  - path: docs/runbooks/*.md
    kind: runbook
    loader: markdown
  - path: docs/postmortems/*.pdf
    kind: postmortem
    loader: generic
    service: checkout
  - path: docs/architecture/*.md
    kind: architecture
    loader: generic
    service: null
"""


def test_parses_every_declared_source(tmp_path):
    path = tmp_path / "corpus.yaml"
    path.write_text(MANIFEST)
    sources = load_manifest(path)
    assert [s.kind for s in sources] == ["runbook", "postmortem", "architecture"]
    assert isinstance(sources[0], Source)


def test_markdown_sources_may_omit_service_because_frontmatter_supplies_it(tmp_path):
    path = tmp_path / "corpus.yaml"
    path.write_text(MANIFEST)
    assert load_manifest(path)[0].service is None
    assert load_manifest(path)[0].loader == "markdown"


def test_an_explicit_null_service_means_global(tmp_path):
    # `scoped` records that the source SAID something, not what it said. An explicit
    # `service: null` is a declaration of global scope; an omitted key is silence, and
    # only silence is rejected.
    path = tmp_path / "corpus.yaml"
    path.write_text(MANIFEST)
    architecture = load_manifest(path)[2]
    assert architecture.service is None
    assert architecture.scoped is True
    assert load_manifest(path)[0].scoped is False  # markdown omitted it


def test_a_generic_source_must_declare_service(tmp_path):
    # Silence is rejected. A document indexed with no service is eligible for every
    # incident, so an omitted key must never quietly mean "global".
    path = tmp_path / "corpus.yaml"
    path.write_text("sources:\n  - path: docs/x/*.pdf\n    kind: postmortem\n    loader: generic\n")
    with pytest.raises(CorpusError, match="service"):
        load_manifest(path)


def test_an_unknown_loader_is_rejected(tmp_path):
    path = tmp_path / "corpus.yaml"
    path.write_text(
        "sources:\n  - path: x/*.md\n    kind: k\n    loader: telepathy\n    service: null\n"
    )
    with pytest.raises(CorpusError, match="telepathy"):
        load_manifest(path)


def test_an_empty_manifest_is_rejected(tmp_path):
    # A manifest that declares nothing would prune the entire index on the next run.
    path = tmp_path / "corpus.yaml"
    path.write_text("sources: []\n")
    with pytest.raises(CorpusError, match="no sources"):
        load_manifest(path)


def test_kinds_are_reported_for_the_tool_description():
    from incident_platform.knowledge.corpus import kinds

    assert kinds([Source("a/*.md", "runbook", "markdown", None, False)]) == ["runbook"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_corpus_manifest.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'incident_platform.knowledge.corpus'`.

- [ ] **Step 3: Write the implementation**

Create `src/incident_platform/knowledge/corpus.py`:

```python
from dataclasses import dataclass
from pathlib import Path

import yaml

LOADERS = frozenset({"markdown", "generic"})


class CorpusError(ValueError):
    """The manifest is malformed. Ingestion must fail loudly, never skip a source."""


@dataclass(frozen=True)
class Source:
    path: str
    kind: str
    loader: str
    service: str | None
    # True when the source names a service. Distinguishes "global on purpose" from
    # "service not declared", which the loader alone cannot tell from `service is None`.
    scoped: bool


def load_manifest(path: Path) -> list[Source]:
    document = yaml.safe_load(Path(path).read_text()) or {}
    declared = document.get("sources") or []
    if not declared:
        raise CorpusError(f"{path}: no sources declared; refusing to prune the whole index")
    sources = []
    for entry in declared:
        loader = entry.get("loader", "markdown")
        if loader not in LOADERS:
            raise CorpusError(f"{path}: unknown loader {loader!r}")
        # Markdown carries frontmatter, so it may resolve its own service per document.
        # Anything else must say, because an unscoped document is eligible everywhere.
        if loader != "markdown" and "service" not in entry:
            raise CorpusError(
                f"{path}: source {entry.get('path')!r} must declare service "
                "(use `service: null` for a global document)"
            )
        sources.append(
            Source(
                path=entry["path"],
                kind=entry["kind"],
                loader=loader,
                service=entry.get("service"),
                scoped="service" in entry,
            )
        )
    return sources


def kinds(sources: list[Source]) -> list[str]:
    """Distinct kinds in declaration order, for the knowledge tool's description."""
    seen: list[str] = []
    for source in sources:
        if source.kind not in seen:
            seen.append(source.kind)
    return seen
```

Create `corpus.yaml` at the repository root:

```yaml
# Every indexed source. `service` scopes retrieval; `service: null` is global and
# eligible for every incident, which is why a non-markdown source must say so explicitly
# rather than leaving the key out.
sources:
  - path: docs/runbooks/*.md
    kind: runbook
    loader: markdown
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_corpus_manifest.py -v`
Expected: PASS, 7 tests.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check .
uv run ruff format --check .
git add src/incident_platform/knowledge/corpus.py corpus.yaml tests/test_corpus_manifest.py
git commit -m "feat(knowledge): declare the corpus in a manifest"
```

---

## Task 4: Loaders

**Files:**
- Create: `src/incident_platform/knowledge/loaders/__init__.py`
- Create: `src/incident_platform/knowledge/loaders/markdown.py`
- Create: `src/incident_platform/knowledge/loaders/generic.py`
- Test: `tests/test_loaders.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_loaders.py`:

```python
import pytest

from incident_platform.knowledge.corpus import Source
from incident_platform.knowledge.loaders import load_source

RUNBOOK = "---\nid: rb1\ntitle: Roll back\nservice: checkout\n---\n\n# Steps\n\nRoll back.\n"


def _markdown_source(directory):
    return Source(str(directory / "*.md"), "runbook", "markdown", None, False)


def test_markdown_keeps_frontmatter_identity(tmp_path):
    (tmp_path / "rb1.md").write_text(RUNBOOK)
    documents = load_source(_markdown_source(tmp_path))
    assert [d.doc_id for d in documents] == ["rb1"]
    assert documents[0].service == "checkout"
    assert documents[0].chunks[0].heading == "Steps"


def test_generic_uses_the_declared_service_and_filename_id(tmp_path):
    (tmp_path / "postmortem-a.txt").write_text("First paragraph.\n\nSecond paragraph.\n")
    source = Source(str(tmp_path / "*.txt"), "postmortem", "generic", "checkout", True)
    documents = load_source(source)
    assert [d.doc_id for d in documents] == ["postmortem-a"]
    assert documents[0].service == "checkout"
    assert documents[0].chunks


def test_generic_honours_an_explicit_global_scope(tmp_path):
    (tmp_path / "arch.txt").write_text("Architecture notes.\n")
    source = Source(str(tmp_path / "*.txt"), "architecture", "generic", None, True)
    assert load_source(source)[0].service is None


def test_generic_chunks_use_the_shared_id_recipe(tmp_path):
    from incident_platform.knowledge.chunker import chunk_id

    (tmp_path / "doc.txt").write_text("Only body.\n")
    source = Source(str(tmp_path / "*.txt"), "postmortem", "generic", None, True)
    chunk = load_source(source)[0].chunks[0]
    assert chunk.chunk_id == chunk_id("doc", chunk.heading, chunk.body)


def test_a_source_matching_nothing_fails_loudly(tmp_path):
    # A silently empty source is how a corpus withers without anyone noticing, and the
    # next prune would delete everything of that kind.
    source = Source(str(tmp_path / "*.pdf"), "postmortem", "generic", None, True)
    with pytest.raises(FileNotFoundError, match="matched no files"):
        load_source(source)


def test_an_empty_document_fails_rather_than_indexing_nothing(tmp_path):
    (tmp_path / "blank.txt").write_text("   \n\n")
    source = Source(str(tmp_path / "*.txt"), "postmortem", "generic", None, True)
    with pytest.raises(ValueError, match="blank.txt"):
        load_source(source)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_loaders.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'incident_platform.knowledge.loaders'`.

- [ ] **Step 3: Write the implementations**

Create `src/incident_platform/knowledge/loaders/markdown.py`:

```python
from pathlib import Path

from incident_platform.knowledge.chunker import Document, chunk_markdown


def load(paths: list[Path], service: str | None, scoped: bool) -> list[Document]:
    """Runbooks, unchanged.

    Frontmatter owns identity and scope here, so a declared service is only a fallback
    for a document that does not name one.
    """
    documents = []
    for path in paths:
        document = chunk_markdown(path.read_text(), source=str(path))
        if document.service is None and scoped:
            document = Document(
                doc_id=document.doc_id,
                title=document.title,
                service=service,
                source=document.source,
                chunks=document.chunks,
            )
        documents.append(document)
    return documents
```

Create `src/incident_platform/knowledge/loaders/generic.py`:

```python
from pathlib import Path

from incident_platform.knowledge.chunker import Chunk, Document, chunk_id

# Matches the retrieval cap's granularity: a chunk larger than one snippet would be
# truncated at query time, which wastes index space and loses the tail silently.
CHUNK_CHARS = 1200
CHUNK_OVERLAP = 120


def _split(text: str) -> list[str]:
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    splitter = RecursiveCharacterTextSplitter(chunk_size=CHUNK_CHARS, chunk_overlap=CHUNK_OVERLAP)
    return [piece for piece in splitter.split_text(text) if piece.strip()]


def _read(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        # pypdf directly rather than a LangChain PDF loader. The loader wraps exactly this
        # and lives in langchain-community, which the package itself reports as sunset.
        from pypdf import PdfReader

        return "\n\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
    return path.read_text()


def load(paths: list[Path], service: str | None, scoped: bool) -> list[Document]:
    """Anything without frontmatter. Identity comes from the filename, scope from the
    manifest, because the document itself carries neither."""
    documents = []
    for path in paths:
        text = _read(path)
        pieces = _split(text)
        if not pieces:
            raise ValueError(f"{path}: document produced no content")
        doc_id = path.stem
        documents.append(
            Document(
                doc_id=doc_id,
                title=doc_id,
                service=service,
                source=str(path),
                chunks=[
                    # Heading is empty: a generic document has no heading structure to
                    # scope a chunk by. The id recipe still includes it, so it stays
                    # interchangeable with a Markdown chunk.
                    Chunk(chunk_id=chunk_id(doc_id, "", piece), heading="", body=piece, ordinal=i)
                    for i, piece in enumerate(pieces)
                ],
            )
        )
    return documents
```

Create `src/incident_platform/knowledge/loaders/__init__.py`:

```python
from glob import glob
from pathlib import Path

from incident_platform.knowledge.chunker import Document
from incident_platform.knowledge.corpus import Source
from incident_platform.knowledge.loaders import generic, markdown

LOADERS = {"markdown": markdown.load, "generic": generic.load}


def load_source(source: Source) -> list[Document]:
    paths = sorted(Path(p) for p in glob(source.path))
    if not paths:
        raise FileNotFoundError(f"{source.path}: matched no files")
    return LOADERS[source.loader](paths, source.service, source.scoped)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_loaders.py -v`
Expected: PASS, 6 tests.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check .
uv run ruff format --check .
git add src/incident_platform/knowledge/loaders tests/test_loaders.py
git commit -m "feat(knowledge): load any declared source into the same chunks"
```

---

## Task 5: Manifest-driven indexing

**Files:**
- Modify: `src/incident_platform/knowledge/index.py`
- Test: `tests/test_knowledge_retrieve.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_knowledge_retrieve.py`:

```python
async def test_reindexing_the_existing_corpus_keeps_every_chunk_id(pool):
    # The proof that generalising ingestion did not disturb replay stability. Evidence ids
    # hash the chunk ids they cite, so a churned id silently invalidates a checkpointed
    # citation.
    from incident_platform.knowledge.index import load_corpus

    await ingest_documents(pool, load_corpus(), kind="runbook")
    async with pool.connection() as conn:
        before = await (
            await conn.execute(
                "SELECT chunk_id FROM knowledge_chunk c JOIN knowledge_document d "
                "USING(doc_id) WHERE d.kind='runbook' ORDER BY chunk_id"
            )
        ).fetchall()
    await ingest_documents(pool, load_corpus(), kind="runbook")
    async with pool.connection() as conn:
        after = await (
            await conn.execute(
                "SELECT chunk_id FROM knowledge_chunk c JOIN knowledge_document d "
                "USING(doc_id) WHERE d.kind='runbook' ORDER BY chunk_id"
            )
        ).fetchall()
    assert [r["chunk_id"] for r in before] == [r["chunk_id"] for r in after]
    assert len(before) == 10
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TEST_DATABASE_URL=postgresql://incident:incident@localhost:5432/incident_test uv run pytest tests/test_knowledge_retrieve.py -m integration -k reindexing -v`

Expected: FAIL. `load_corpus()` still takes a directory and returns documents for one kind, so the call signature does not match. Note the exact error, then implement.

- [ ] **Step 3: Write the implementation**

In `src/incident_platform/knowledge/index.py`, replace `CORPUS` and `load_corpus`:

```python
MANIFEST = Path("corpus.yaml")


def load_corpus(manifest: Path = MANIFEST) -> list[Document]:
    """Every document declared in the manifest, across all sources.

    Duplicate ids are rejected across the whole corpus, not per source: two sources that
    both produce `checkout` would otherwise overwrite each other's chunks row by row.
    """
    documents: list[Document] = []
    seen: dict[str, str] = {}
    for source in load_manifest(manifest):
        for document in load_source(source):
            if document.doc_id in seen:
                raise ValueError(
                    f"Duplicate document id {document.doc_id!r} in {document.source} "
                    f"and {seen[document.doc_id]}"
                )
            seen[document.doc_id] = document.source
            documents.append(document)
    return documents


def by_kind(manifest: Path = MANIFEST) -> dict[str, list[Document]]:
    """Documents grouped by the kind their source declared."""
    grouped: dict[str, list[Document]] = {}
    for source in load_manifest(manifest):
        grouped.setdefault(source.kind, []).extend(load_source(source))
    return grouped
```

Add the imports at the top of the file:

```python
from incident_platform.knowledge.corpus import load_manifest
from incident_platform.knowledge.loaders import load_source
```

Then replace the body of `main()`'s `try` block so every kind is indexed and pruned:

```python
    try:
        from incident_platform.knowledge.embeddings import provider

        totals = []
        for kind, documents in by_kind().items():
            count = await ingest_documents(pool, documents, kind=kind)
            pruned = await prune_missing(pool, documents, kind=kind)
            totals.append(f"{kind}: {count} chunks, pruned {pruned}")
        incidents = await ingest_resolved_incidents(pool)
        embedded = await embed_corpus(pool, provider(cfg))
        print(
            f"indexed {'; '.join(totals)}; {incidents} incident chunks, embedded {embedded}",
            file=sys.stderr,
        )
    finally:
        await pool.close()
```

`prune_missing` already refuses an empty document list, so a source that matched nothing
cannot wipe its kind — and `load_source` raises before that anyway.

- [ ] **Step 4: Run test to verify it passes**

```bash
TEST_DATABASE_URL=postgresql://incident:incident@localhost:5432/incident_test \
  uv run pytest tests/test_knowledge_retrieve.py -m integration -v
```

Expected: PASS. The Task 3 and Task 6 corpus tests in that file must still pass unchanged —
that is the proof the manifest produces exactly what the glob produced.

- [ ] **Step 5: Verify against the live database**

```bash
docker compose up -d postgres
uv run python -m incident_platform.knowledge.index
```

Expected: `indexed runbook: 10 chunks, pruned 0; 5 incident chunks, embedded 0`. If the
chunk count is not 10, stop and report — the manifest is producing different documents.

- [ ] **Step 6: Commit**

```bash
git add src/incident_platform/knowledge/index.py tests/test_knowledge_retrieve.py
git commit -m "feat(knowledge): index every source the manifest declares"
```

---

## Task 6: Collapse the tool vocabulary

The breaking change. Do it in one commit so the repository is never in a state where the
tool names disagree across files.

**Files:**
- Modify: `src/incident_platform/models.py`
- Modify: `src/incident_platform/integrations/tools.py`
- Modify: `src/incident_platform/workflow/graph.py`
- Modify: `src/incident_platform/workflow/policy.py`
- Test: `tests/test_policy_rca.py` (append), `tests/test_workflow.py` (adjust)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_policy_rca.py`:

```python
def test_knowledge_is_not_observational():
    # The whole retrieval corpus is procedure and precedent. Collapsing two tool names
    # into one must not accidentally promote it to an observation of this incident.
    from incident_platform.workflow.policy import OBSERVATIONAL

    assert "knowledge" not in OBSERVATIONAL


def test_knowledge_only_citations_are_rejected():
    found = [evidence("a", "knowledge"), evidence("b", "knowledge")]
    assert valid_rca(rca("a", "b"), found, True) is False


def test_one_observational_plus_knowledge_is_valid():
    found = [evidence("a", "metrics"), evidence("b", "knowledge")]
    assert valid_rca(rca("a", "b"), found, True) is True


def test_the_tool_sets_still_partition_exactly():
    # Three declarations must agree: the analyst's vocabulary, the observational set, and
    # the retrieval mapping. Nothing enforces this at runtime, so it is asserted here.
    from incident_platform.integrations.knowledge_backend import TOOL_CALLS
    from incident_platform.integrations.tools import READ_TOOLS
    from incident_platform.workflow.policy import OBSERVATIONAL

    assert OBSERVATIONAL | set(TOOL_CALLS) == set(READ_TOOLS)
    assert not (OBSERVATIONAL & set(TOOL_CALLS))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_policy_rca.py -v`
Expected: FAIL. `test_knowledge_only_citations_are_rejected` fails because `knowledge` is
not yet a tool name, and the partition test fails because `TOOL_CALLS` still holds two keys.

- [ ] **Step 3: Write the implementation**

In `src/incident_platform/models.py`, replace the tool literal and add the kind field:

```python
class InvestigationStep(StrictModel):
    hypothesis: str
    tool: (
        Literal[
            "logs",
            "metrics",
            "kubernetes",
            "deployments",
            "config_diffs",
            "topology",
            "knowledge",
        ]
        | None
    ) = None
    # Which corpus to search. Only meaningful for the knowledge tool; ignored otherwise.
    kind: str = Field(default="", max_length=64)
    query: str = Field(default="", max_length=1000)
    conclusion: RCA | None = None
```

In `src/incident_platform/integrations/tools.py`, replace `READ_TOOLS`, widen the protocol,
and teach the mock and the tool factory about `kind`:

```python
READ_TOOLS = (
    "logs",
    "metrics",
    "kubernetes",
    "deployments",
    "config_diffs",
    "topology",
    "knowledge",
)
```

```python
class EvidenceBackend(Protocol):
    async def query(self, tool: str, service: str, query: str, kind: str = "") -> Evidence: ...
    async def verify(self, service: str, execution: dict) -> bool: ...
```

In `MockEvidenceBackend.query`, replace the signature and the two retrieval entries:

```python
    async def query(self, tool, service, query, kind=""):
        observations = {
            "metrics": "Simulated: checkout error rate 12%, latency p95 1800ms after release v2.",
            "logs": "Simulated: upstream timeout errors began after release v2.",
            "deployments": "Simulated: release v2 deployed 8 minutes before the alert; v1 healthy.",
            "config_diffs": "Simulated: release v2 changed upstream timeout from 5s to 100ms.",
            "kubernetes": "Simulated: pods ready; no OOMKilled events.",
            "topology": "Simulated: checkout -> payments; owner commerce-oncall.",
            "knowledge": f"Simulated {kind or 'knowledge'}: rollback v2 after approval; "
            "verify error rate <1%.",
        }
```

In `UnconfiguredEvidenceBackend`, widen the signature the same way:

```python
    async def query(self, tool, service, query, kind=""):
        raise NotImplementedError(f"Configure read-only {tool} adapter before live mode")
```

In `read_tools`, give the knowledge tool its extra argument:

```python
def read_tools(backend: EvidenceBackend, service: str, kinds: tuple[str, ...] = ()):
    def make(name):
        if name == "knowledge":

            async def query(query: str, kind: str = "") -> dict:
                evidence = await backend.query(name, service, query, kind)
                return evidence.model_dump(mode="json")

            available = ", ".join(kinds) if kinds else "any indexed corpus"
            description = (
                "Read-only retrieval over indexed documents for the scoped service. "
                f"Available kinds: {available}. Procedure and precedent, not observation."
            )
        else:

            async def query(query: str) -> dict:
                evidence = await backend.query(name, service, query)
                return evidence.model_dump(mode="json")

            description = f"Read-only {name} evidence for the scoped service, queried on demand."

        return StructuredTool.from_function(coroutine=query, name=name, description=description)

    return {name: make(name) for name in READ_TOOLS}
```

`service` stays closed over in both branches. It is never a tool argument, so the analyst
cannot widen its own scope.

In `src/incident_platform/workflow/graph.py`, pass the kind through at line 52:

```python
        if step.tool:
            tools = read_tools(backend, state["incident"]["service"])
            payload = {"query": step.query}
            if step.tool == "knowledge":
                payload["kind"] = step.kind
            evidence = await tools[step.tool].ainvoke(payload)
            update["evidence"] = [*state["evidence"], evidence]
```

In `src/incident_platform/workflow/policy.py`, `OBSERVATIONAL` is unchanged — it already
lists only the six observational tools, and `knowledge` is simply absent. Confirm that by
reading it; if `runbooks` or `similar_incidents` appear there, something is wrong.

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/test_policy_rca.py -v
uv run pytest -q
```

Expected: the policy tests pass. The full suite will still fail in
`tests/test_knowledge_backend.py` until Task 7 maps the new name — that is expected and is
why these two tasks land close together. Every other file must pass.

- [ ] **Step 5: Update the analyst prompt**

In `src/incident_platform/integrations/llm.py`, the investigate task string lists the tools.
Replace the tool list so it matches `READ_TOOLS`:

```python
        return await self.structured(
            "rca",
            InvestigationStep,
            "Choose one read tool or conclude RCA. Tools: logs, metrics, kubernetes, "
            "deployments, config_diffs, topology, knowledge. Set kind when using "
            "knowledge. Retrieval is optional. Conclude only with corroborating evidence.",
            state,
        )
```

- [ ] **Step 6: Commit**

```bash
git add src/incident_platform/models.py src/incident_platform/integrations/tools.py \
        src/incident_platform/workflow/graph.py src/incident_platform/integrations/llm.py \
        tests/test_policy_rca.py
git commit -m "feat(tools): collapse retrieval into one knowledge tool"
```

---

## Task 7: Retrieval backend and MCP server

**Files:**
- Modify: `src/incident_platform/integrations/knowledge_backend.py`
- Modify: `src/incident_platform/knowledge/server.py`
- Modify: `deploy/litellm/config.yaml`
- Test: `tests/test_knowledge_backend.py` (adjust)

- [ ] **Step 1: Update the failing assertions**

In `tests/test_knowledge_backend.py`, replace the two tool-name tests:

```python
async def test_maps_snippets_to_one_evidence(cfg):
    client = FakeClient([SNIPPET])
    evidence = await backend(client, cfg).query("knowledge", "checkout", "rollback", "runbook")
    assert client.calls == [
        (
            "runbooks-search_knowledge",
            {
                "service": "checkout",
                "query": "rollback",
                "kind": "runbook",
                "limit": cfg.knowledge_top_k,
            },
        )
    ]
    assert evidence.tool == "knowledge"
    assert evidence.trusted is True
    assert "Roll the deployment back" in evidence.observation


async def test_an_unknown_kind_returns_an_untrusted_empty_result(cfg):
    # An unknown kind is a model mistake, not an outage. It must read as "nothing found",
    # which cannot corroborate an RCA, rather than failing the node.
    evidence = await backend(FakeClient([]), cfg).query("knowledge", "checkout", "q", "nonsense")
    assert evidence.trusted is False
```

Delete `test_similar_incidents_uses_its_own_tool`; the name no longer exists. Update every
other call in the file from `query("runbooks", ...)` to `query("knowledge", ..., "runbook")`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_knowledge_backend.py -v`
Expected: FAIL on the call name and on the extra `kind` argument.

- [ ] **Step 3: Write the implementation**

In `src/incident_platform/integrations/knowledge_backend.py`:

```python
MCP_SERVER = "runbooks"
TOOL_CALLS = {"knowledge": f"{MCP_SERVER}-search_knowledge"}
```

and change `query` to accept and forward the kind:

```python
    async def query(self, tool, service, query, kind="") -> Evidence:
        call = TOOL_CALLS.get(tool)
        if call is None:
            return await self.inner.query(tool, service, query)

        try:
            snippets = await self.client.call_tool(
                call,
                {
                    "service": service,
                    "query": query,
                    "kind": kind,
                    "limit": self.cfg.knowledge_top_k,
                },
            )
```

The identifier and the empty-result branch already read `tool`; include `kind` in the hash
so the same query against two corpora produces two evidence ids:

```python
        identifier = uuid5(NAMESPACE_URL, f"{service}:{tool}:{kind}:{query}:{'|'.join(chunk_ids)}")
```

In `src/incident_platform/knowledge/server.py`, replace the two tools with one:

```python
@mcp.tool()
async def search_knowledge(service: str, query: str, kind: str = "", limit: int = 5) -> list[dict]:
    """Search indexed documents scoped to a service. Read-only.

    An empty kind searches every corpus; an unknown one matches nothing, which the caller
    reports as an untrusted empty result rather than an error.
    """
    found = await search(
        pool, kind=kind or None, service=service, query=query, limit=limit, embedder=embedder
    )
    return [asdict(snippet) for snippet in found]
```

In `src/incident_platform/knowledge/retrieve.py`, make `kind` optional in both queries by
replacing the `WHERE d.kind = %(kind)s` clause in `LEXICAL_SQL` and `DENSE_SQL` with:

```sql
WHERE (%(kind)s::text IS NULL OR d.kind = %(kind)s)
```

In `deploy/litellm/config.yaml`, replace the `allowed_tools` list:

```yaml
    allowed_tools:
      - search_knowledge
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest -q
TEST_DATABASE_URL=postgresql://incident:incident@localhost:5432/incident_test \
  uv run pytest -m integration -q
```

Expected: both suites pass. The retrieval integration tests exercise `kind="runbook"`, and
the new optional-kind clause must not change their results.

- [ ] **Step 5: Commit**

```bash
git add src/incident_platform/integrations/knowledge_backend.py \
        src/incident_platform/knowledge/server.py \
        src/incident_platform/knowledge/retrieve.py \
        deploy/litellm/config.yaml tests/test_knowledge_backend.py
git commit -m "feat(knowledge): serve one knowledge tool over any corpus"
```

---

## Task 8: Prove it with a second format

**Files:**
- Create: `docs/architecture/checkout-topology.md`
- Modify: `corpus.yaml`

- [ ] **Step 1: Add a frontmatter-less document**

Create `docs/architecture/checkout-topology.md` with NO frontmatter:

```markdown
# Checkout service topology

Checkout calls payments synchronously with a 5 second upstream timeout. Payments calls the
ledger asynchronously. A timeout budget change in checkout therefore surfaces as payments
errors before it surfaces as ledger errors.

Ownership sits with commerce-oncall. The service is deployed from the commerce monorepo and
shares a connection pool with the cart service, so pool exhaustion in either appears in both.
```

- [ ] **Step 2: Declare it**

Append to `corpus.yaml`:

```yaml
  - path: docs/architecture/*.md
    kind: architecture
    loader: generic
    service: null
```

`service: null` is written explicitly. It means global, and the manifest rejects a generic
source that says nothing at all.

- [ ] **Step 3: Make it available inside the image**

In `Dockerfile`, extend the corpus copy:

```dockerfile
COPY docs/runbooks ./docs/runbooks
COPY docs/architecture ./docs/architecture
COPY corpus.yaml ./corpus.yaml
```

- [ ] **Step 4: Index and verify**

```bash
docker compose up -d postgres
uv run python -m incident_platform.knowledge.index
```

Expected output names both kinds, for example:
`indexed runbook: 10 chunks, pruned 0; architecture: 2 chunks, pruned 0; 5 incident chunks, embedded 0`

Then check scoping and retrieval directly:

```bash
uv run python -c "
import asyncio
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from incident_platform.config import settings
from incident_platform.knowledge.retrieve import search

async def main():
    pool = AsyncConnectionPool(settings().database_url, open=False, min_size=1,
                               kwargs={'row_factory': dict_row}, timeout=3)
    await pool.open(wait=True)
    try:
        arch = await search(pool, kind='architecture', service='payments',
                            query='timeout budget upstream')
        print('architecture is global:', [s.doc_id for s in arch])
        every = await search(pool, kind=None, service='checkout', query='timeout rollback')
        print('all kinds:', sorted({s.doc_id for s in every}))
    finally:
        await pool.close()

asyncio.run(main())
"
```

Expected: the architecture document appears for `payments` even though it was never scoped
to it, proving `service: null` is global; and the all-kinds search returns documents from
more than one corpus.

- [ ] **Step 5: Commit**

```bash
git add docs/architecture corpus.yaml Dockerfile
git commit -m "feat(knowledge): add an architecture corpus in a second format"
```

---

## Task 9: Regression gate

**Files:** none modified.

- [ ] **Step 1: Run the full verification sequence**

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
TEST_DATABASE_URL=postgresql://incident:incident@localhost:5432/incident_test uv run pytest -m integration -q
docker compose down && docker compose up --build -d && docker compose ps
docker compose exec -T worker python -m incident_platform.devtools.verify_gateway
uv run python scripts/demo.py | tail -8
```

Expected: lint clean, both suites pass, every service healthy, the gateway verifier prints
its three lines, and the demo reaches `resolved`.

The gateway verifier calls `backend.query("runbooks", ...)`. That name no longer exists, so
update `src/incident_platform/devtools/verify_gateway.py` to
`backend.query("knowledge", "checkout", "rollback error rate", "runbook")` and keep the
five-document assertion.

- [ ] **Step 2: Confirm the RCA gate did not weaken**

```bash
uv run pytest tests/test_policy_rca.py -v
```

Expected: PASS, including that knowledge-only citations are rejected and that the three tool
declarations still partition exactly.

- [ ] **Step 3: Commit anything outstanding**

```bash
git status --short
```

Expected: clean, apart from the verifier change, which commits with:

```bash
git add src/incident_platform/devtools/verify_gateway.py
git commit -m "fix(devtools): verify retrieval through the knowledge tool"
```

---

## Task 10: Documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/operations.md`
- Modify: `AGENTS.md`

- [ ] **Step 1: Update the README**

Replace the retrieval paragraph's first sentence, which names the old tools, with:

```markdown
Retrieval is one read tool, `knowledge`, taking the corpus `kind` to search. What corpora
exist is declared in `corpus.yaml`, not in code: a source names a path, a kind, a loader and
a service, and adding a document type needs no code change and no prompt change. Markdown
with frontmatter keeps its existing parser; anything else goes through a LangChain loader
and splitter. Every loader produces the same chunk id recipe, so evidence ids stay stable
across a re-index.
```

Add to the "Layout" block after the `knowledge/` line:

```text
  knowledge/loaders/ per-format document loading into the shared chunk shape
```

Add to the "Deliberate boundaries" list:

```markdown
- A document's `service` scopes every retrieval of it, and a generic source must declare one
  even if that declaration is `service: null` for a global document. An omitted service is
  rejected rather than defaulted, because an unscoped document is eligible for every
  incident and would be cited as precedent for services it never described.
```

- [ ] **Step 2: Update operations documentation**

Append to `docs/operations.md`:

```markdown
## Corpus

`corpus.yaml` declares every indexed source: a glob, a `kind`, a `loader` and a `service`.
Adding a document type is a manifest entry, not a code change, and the available kinds reach
the analyst through the knowledge tool's generated description.

`service` is the scoping boundary. `service: null` means global and eligible for every
incident; an omitted `service` on a non-Markdown source is an error rather than a default,
because the failure mode is silent and serious: an unscoped document is retrieved for every
service and can be cited as precedent for one it never described. Markdown may omit it
because frontmatter carries it per document.

A source matching no files fails ingestion. That is deliberate: a glob that quietly matches
nothing looks identical to a healthy run, and the next prune would remove that kind entirely.

Re-indexing must not change chunk ids. They hash document id, heading and content, and
evidence ids hash the chunk ids they cite, so a churned id silently invalidates a
checkpointed citation. Any loader change should be checked against
`test_reindexing_the_existing_corpus_keeps_every_chunk_id` before it ships.
```

- [ ] **Step 3: Update AGENTS.md invariants**

Append to the invariants list:

```markdown
- Every loader uses `chunker.chunk_id`. A second id recipe silently splits one document
  across two rows and destabilises evidence ids.
- A retrieval tool never takes `service` as an argument. It is injected from the incident so
  the analyst cannot widen its own scope.
```

- [ ] **Step 4: Final verification**

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
TEST_DATABASE_URL=postgresql://incident:incident@localhost:5432/incident_test uv run pytest -m integration -q
```

- [ ] **Step 5: Commit**

```bash
git add README.md docs/operations.md AGENTS.md
git commit -m "docs(knowledge): document the corpus manifest and its scoping rule"
```

---

## Done

Report at the end:

- The resolved `langchain-text-splitters` and `pypdf` versions, and
  any class name that differed from Task 1's recorded table.
- Whether re-indexing the runbook corpus produced identical chunk ids, quoted.
- Whether any graph test moved when the tool vocabulary changed. If one did, say which: it
  would mean the mock path depended on a retrieval tool name.
- The final output of the four verification commands, quoted, not summarized.
