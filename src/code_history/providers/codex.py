"""OpenAI Codex CLI provider. Shells out to `codex`."""
from __future__ import annotations

import subprocess
import tempfile

from ..config import CodexConfig
from .base import Provider, ProviderError


class CodexProvider(Provider):
    name = "codex"

    def __init__(self, cfg: CodexConfig) -> None:
        self.cfg = cfg

    def _generate(self, prompt: str) -> str:
        cmd = [
            self.cfg.binary,
            "exec",
            "--model", self.cfg.model,
            prompt,
        ]
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
                raise ProviderError(f"codex binary not found: {self.cfg.binary}") from e
            except subprocess.TimeoutExpired as e:
                raise ProviderError(f"codex timed out after {self.cfg.timeout_seconds}s") from e
        if proc.returncode != 0:
            raise ProviderError(
                f"codex exited {proc.returncode}: {proc.stderr.strip()[:300] or proc.stdout.strip()[:300]}"
            )
        return proc.stdout
