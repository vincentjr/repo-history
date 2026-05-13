from code_history.providers.base import Provider, ProviderError


class FlakyProvider(Provider):
    backoff_seconds = 0.0

    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0

    def _generate(self, prompt: str) -> str:
        self.calls += 1
        return self._outputs.pop(0)


def test_retries_until_valid():
    p = FlakyProvider([
        "garbage no json here",
        '{"summary": "ok"}',
    ])
    rec = p.distill("d", "desc", [])
    assert p.calls == 2
    assert rec.summary == "ok"


def test_gives_up_after_max_attempts():
    p = FlakyProvider(["nope"] * 3)
    try:
        p.distill("d", "desc", [])
    except ProviderError:
        assert p.calls == 3
    else:
        raise AssertionError("expected ProviderError")
