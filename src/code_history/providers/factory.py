from __future__ import annotations

import logging

from ..config import Config
from .base import Provider, ProviderError


log = logging.getLogger(__name__)


def build_provider(cfg: Config) -> Provider:
    name = cfg.llm.provider
    log.info("building provider: %s", name)
    if name == "cursor":
        from .cursor import CursorProvider
        return CursorProvider(cfg.llm.cursor)
    if name == "codex":
        from .codex import CodexProvider
        return CodexProvider(cfg.llm.codex)
    if name == "vertex":
        from .vertex import VertexProvider
        return VertexProvider(cfg.llm.vertex)
    raise ProviderError(f"Unknown provider: {name!r}")
