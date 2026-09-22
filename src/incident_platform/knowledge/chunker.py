import hashlib
import re
from dataclasses import dataclass, field

HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
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
    chunks: list[Chunk] = field(default_factory=list)


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
    """Split one Markdown document into heading-scoped chunks. Pure; no I/O."""
    fields, body = _frontmatter(text, source)
    doc_id = fields.get("id", "")
    if not doc_id:
        raise CorpusError(f"{source}: frontmatter must define a nonempty id")
    title = fields.get("title") or doc_id
    service = fields.get("service") or None

    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in body.splitlines():
        heading = HEADING.match(line)
        if heading:
            sections.append((heading.group(2).strip(), []))
        else:
            sections[-1][1].append(line)

    chunks: list[Chunk] = []
    for heading, lines in sections:
        content = "\n".join(lines).strip()
        if not content:
            continue
        ordinal = len(chunks)
        # Hash content, not the file path, so renaming a file does not churn every id.
        digest = hashlib.sha256(f"{doc_id}:{ordinal}:{heading}:{content}".encode()).hexdigest()
        chunks.append(Chunk(chunk_id=digest[:32], heading=heading, body=content, ordinal=ordinal))

    if not chunks:
        raise CorpusError(f"{source}: document body is empty")
    return Document(doc_id=doc_id, title=title, service=service, source=source, chunks=chunks)
