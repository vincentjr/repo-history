"""Provider interface and shared retry helper."""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import Callable

from ..distill import Record, build_prompt, parse_record


log = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    pass


class Provider(ABC):
    name: str = "base"
    max_attempts: int = 3
    backoff_seconds: float = 2.0

    @abstractmethod
    def _generate(self, prompt: str) -> str:
        """Return raw model output. Implementations handle their own transport and timeout."""

    def distill(self, diff: str, description: str, linked_issues: list[str]) -> Record:
        prompt = build_prompt(diff, description, linked_issues)
        log.info("distill via %s (prompt %d chars)", self.name, len(prompt))
        return _retry(lambda: parse_record(self._generate(prompt)), self.max_attempts, self.backoff_seconds)


def _retry(fn: Callable[[], Record], attempts: int, backoff: float) -> Record:
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            last = e
            if i < attempts - 1:
                sleep_for = backoff * (2**i)
                log.warning("distill attempt %d/%d failed (%s); retrying in %.1fs", i + 1, attempts, e, sleep_for)
                time.sleep(sleep_for)
    raise ProviderError(f"distill failed after {attempts} attempts: {last}") from last
