"""Cursor CLI provider. Shells out to `cursor-agent` in a tempdir."""
from __future__ import annotations

import logging
import subprocess
import tempfile

from ..config import CursorConfig
from .base import Provider, ProviderError


log = logging.getLogger(__name__)


class CursorProvider(Provider):
    name = "cursor"

    def __init__(self, cfg: CursorConfig) -> None:
        self.cfg = cfg

    def _generate(self, prompt: str) -> str:
        cmd = [
            self.cfg.binary,
            "--print",
            "--output-format", "text",
            "--model", self.cfg.model,
            "--trust",
            prompt,
        ]
        log.info("invoking cursor-agent model=%s timeout=%ds", self.cfg.model, self.cfg.timeout_seconds)
        with tempfile.TemporaryDirectory() as cwd:
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    timeout=self.cfg.timeout_seconds,
                    check=False,
                )
            except FileNotFoundError as e:
                raise ProviderError(f"cursor-agent binary not found: {self.cfg.binary}") from e
            except subprocess.TimeoutExpired as e:
                raise ProviderError(f"cursor-agent timed out after {self.cfg.timeout_seconds}s") from e
        if proc.returncode != 0:
            raise ProviderError(
                f"cursor-agent exited {proc.returncode}: {proc.stderr.strip()[:300] or proc.stdout.strip()[:300]}"
            )
        return proc.stdout
