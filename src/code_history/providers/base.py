"""Provider interface and shared retry helper."""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Callable

from ..distill import Record, build_prompt, parse_record


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
        return _retry(lambda: parse_record(self._generate(prompt)), self.max_attempts, self.backoff_seconds)


def _retry(fn: Callable[[], Record], attempts: int, backoff: float) -> Record:
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            last = e
            if i < attempts - 1:
                time.sleep(backoff * (2**i))
    raise ProviderError(f"distill failed after {attempts} attempts: {last}") from last
