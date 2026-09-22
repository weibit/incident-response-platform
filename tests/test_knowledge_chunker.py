import pytest

from incident_platform.knowledge.chunker import CorpusError, Document, chunk_markdown

SAMPLE = """---
id: checkout-rollback
title: Roll back a bad checkout release
service: checkout
tags: [rollback, deployment]
updated: 2026-09-01
---

Intro paragraph about rollbacks.

## Preconditions

Confirm the release is the cause.

## Steps

Run the rollback after approval.
"""


def test_parses_frontmatter():
    doc = chunk_markdown(SAMPLE, source="docs/runbooks/checkout-rollback.md")
    assert isinstance(doc, Document)
    assert doc.doc_id == "checkout-rollback"
    assert doc.title == "Roll back a bad checkout release"
    assert doc.service == "checkout"
    assert doc.source == "docs/runbooks/checkout-rollback.md"


def test_splits_on_headings_and_keeps_the_preamble():
    doc = chunk_markdown(SAMPLE, source="docs/runbooks/checkout-rollback.md")
    headings = [c.heading for c in doc.chunks]
    assert headings == ["", "Preconditions", "Steps"]
    assert doc.chunks[0].body.strip() == "Intro paragraph about rollbacks."
    assert [c.ordinal for c in doc.chunks] == [0, 1, 2]


def test_chunk_ids_are_deterministic_and_unique():
    first = chunk_markdown(SAMPLE, source="a.md")
    second = chunk_markdown(SAMPLE, source="a.md")
    assert [c.chunk_id for c in first.chunks] == [c.chunk_id for c in second.chunks]
    assert len({c.chunk_id for c in first.chunks}) == len(first.chunks)


def test_chunk_id_is_independent_of_source_path():
    renamed = chunk_markdown(SAMPLE, source="moved/elsewhere.md")
    original = chunk_markdown(SAMPLE, source="a.md")
    assert [c.chunk_id for c in renamed.chunks] == [c.chunk_id for c in original.chunks]


def test_missing_id_is_rejected():
    with pytest.raises(CorpusError, match="id"):
        chunk_markdown("---\ntitle: No id\n---\n\nBody.\n", source="a.md")


def test_empty_body_is_rejected():
    body = "---\nid: x\ntitle: T\n---\n\n\n"
    with pytest.raises(CorpusError, match="empty"):
        chunk_markdown(body, source="a.md")


# Built rather than written literally so this test file can itself live inside a
# fenced block in the plan document.
BACKTICKS = "`" * 3


def fenced(marker: str) -> str:
    return (
        "---\nid: fenced\ntitle: Fenced\n---\n\n"
        "## Steps\n\nRun this:\n\n"
        f"{marker}bash\n# restart the service\nsystemctl restart checkout\n{marker}\n\n"
        "Then confirm it came back.\n"
    )


@pytest.mark.parametrize("marker", [BACKTICKS, "~~~"])
def test_a_comment_inside_a_code_fence_is_not_a_heading(marker):
    # Runbooks are mostly shell snippets. Splitting on a shell comment would tear a
    # command away from the context that explains it.
    doc = chunk_markdown(fenced(marker), source="a.md")
    assert [c.heading for c in doc.chunks] == ["Steps"]
    assert "systemctl restart checkout" in doc.chunks[0].body
    assert "Then confirm it came back." in doc.chunks[0].body


def test_inserting_a_section_does_not_churn_the_ids_below_it():
    before = "---\nid: x\ntitle: T\n---\n\n## A\n\nAlpha.\n\n## B\n\nBravo.\n"
    after = "---\nid: x\ntitle: T\n---\n\n## New\n\nNovel.\n\n## A\n\nAlpha.\n\n## B\n\nBravo.\n"
    original = {c.heading: c.chunk_id for c in chunk_markdown(before, source="a.md").chunks}
    edited = {c.heading: c.chunk_id for c in chunk_markdown(after, source="a.md").chunks}
    assert original["A"] == edited["A"]
    assert original["B"] == edited["B"]


def test_identical_sections_collapse_to_one_chunk():
    text = "---\nid: x\ntitle: T\n---\n\n## A\n\nSame.\n\n## A\n\nSame.\n"
    doc = chunk_markdown(text, source="a.md")
    assert len(doc.chunks) == 1
    assert [c.ordinal for c in doc.chunks] == [0]
