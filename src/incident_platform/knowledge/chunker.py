import hashlib
import re
from dataclasses import dataclass

HEADING = re.compile(r"^#{1,6}\s+(.*)$")
# Per CommonMark a fence indented four or more spaces is itself code, so cap at three.
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)


class CorpusError(ValueError):
    """A corpus document is malformed. Ingestion must fail loudly, never skip."""


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    heading: str
    body: str
    ordinal: int


@dataclass(frozen=True)
class Document:
    doc_id: str
    title: str
    service: str | None
    source: str
    # No default: chunk_markdown raises rather than returning a chunkless Document, so
    # an empty list is unrepresentable by design. Keep it structurally unrepresentable.
    chunks: list[Chunk]


def _frontmatter(text: str, source: str) -> tuple[dict[str, str], str]:
    match = FRONTMATTER.match(text)
    if not match:
        raise CorpusError(f"{source}: missing YAML frontmatter block")
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition(":")
        if not separator:
            raise CorpusError(f"{source}: malformed frontmatter line {line!r}")
        fields[key.strip()] = value.strip()
    return fields, text[match.end() :]


def chunk_markdown(text: str, source: str) -> Document:
    """Split one Markdown document into heading-scoped chunks. ATX headings only.

    Pure; no I/O. Setext headings are not recognised.
    """
    fields, body = _frontmatter(text, source)
    doc_id = fields.get("id", "")
    if not doc_id:
        raise CorpusError(f"{source}: frontmatter must define a nonempty id")
    title = fields.get("title") or doc_id
    service = fields.get("service") or None

    sections: list[tuple[str, list[str]]] = [("", [])]
    fence: str | None = None
    for line in body.splitlines():
        match = FENCE.match(line)
        if match:
            marker, rest = match.group(1), match.group(2)
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence) and not rest.strip():
                # A closing fence matches the opening marker, is at least as long, and
                # carries no info string. A tilde divider inside a backtick block is not
                # a fence, and treating it as one would tear the block apart.
                fence = None
        # A shell comment inside a fenced block is not a heading. Runbooks are mostly
        # shell snippets, so splitting on one would tear a command from its context.
        heading = None if fence else HEADING.match(line)
        if heading:
            sections.append((heading.group(1).strip(), []))
        else:
            sections[-1][1].append(line)
    if fence is not None:
        raise CorpusError(f"{source}: unclosed code fence")

    chunks: list[Chunk] = []
    seen: set[str] = set()
    for heading, lines in sections:
        # Trim blank padding only. Stripping the joined text would eat the leading
        # indentation of the first line and ruin an indented code block.
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        if not lines:
            continue
        content = "\n".join(lines)
        # Hash content and heading only. Not the file path, so renaming does not churn
        # ids; not the position, so inserting a section above does not churn every id
        # below it. Identical sections collapse to one chunk, which is what they are.
        chunk_id = hashlib.sha256(f"{doc_id}:{heading}:{content}".encode()).hexdigest()[:32]
        if chunk_id in seen:
            continue
        seen.add(chunk_id)
        chunks.append(Chunk(chunk_id=chunk_id, heading=heading, body=content, ordinal=len(chunks)))

    if not chunks:
        raise CorpusError(f"{source}: document body is empty")
    return Document(doc_id=doc_id, title=title, service=service, source=source, chunks=chunks)
