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
