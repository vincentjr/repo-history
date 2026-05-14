"""Vertex AI provider. Uses google-cloud-aiplatform if installed."""
from __future__ import annotations

import logging

from ..config import VertexConfig
from .base import Provider, ProviderError


log = logging.getLogger(__name__)


class VertexProvider(Provider):
    name = "vertex"

    def __init__(self, cfg: VertexConfig) -> None:
        if not cfg.project_id:
            raise ProviderError("Vertex provider requires llm.vertex.project_id")
        self.cfg = cfg
        try:
            import vertexai
            from vertexai.generative_models import GenerativeModel, GenerationConfig
        except ImportError as e:
            raise ProviderError(
                "Vertex provider needs `google-cloud-aiplatform`. "
                "Install via `pip install code-history[vertex]`."
            ) from e
        vertexai.init(project=cfg.project_id, location=cfg.location)
        self._model = GenerativeModel(cfg.model)
        self._gen_config = GenerationConfig(response_mime_type="application/json")

    def _generate(self, prompt: str) -> str:
        log.info("invoking vertex model=%s", self.cfg.model)
        try:
            resp = self._model.generate_content(
                prompt,
                generation_config=self._gen_config,
            )
        except Exception as e:
            raise ProviderError(f"vertex generate_content failed: {e}") from e
        text = getattr(resp, "text", None)
        if not text:
            raise ProviderError("vertex returned empty response")
        return text
