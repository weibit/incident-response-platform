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
