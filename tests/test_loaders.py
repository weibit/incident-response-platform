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
