import structlog

log = structlog.get_logger()


def _init(model: str):
    """Isolated so tests can substitute it without importing a provider package.

    Anthropic publishes no embeddings endpoint, so a dense leg always means a third
    party. That is why it is optional and why lexical search must stand alone.
    """
    from langchain.embeddings import init_embeddings

    return init_embeddings(model)


class Embedder:
    def __init__(self, model, dim: int):
        self.model, self.dim = model, dim

    async def embed(self, texts: list[str]) -> list[list[float]] | None:
        try:
            return await self.model.aembed_documents(texts)
        except Exception as exc:
            # Retrieval degrades to lexical; it never fails because of embeddings.
            log.warning("knowledge.embedding_failed", error_type=type(exc).__name__)
            return None

    async def embed_query(self, text: str) -> list[float] | None:
        result = await self.embed([text])
        return result[0] if result else None


def provider(cfg) -> Embedder | None:
    if not cfg.embedding_model:
        return None
    return Embedder(_init(cfg.embedding_model), cfg.embedding_dim)
