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
