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
