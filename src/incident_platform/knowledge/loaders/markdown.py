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
