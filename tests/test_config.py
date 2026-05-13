import os
import pytest
from code_history.config import ConfigError, load_config, write_skeleton


def test_skeleton_then_load(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.toml"
    write_skeleton(cfg_path)
    monkeypatch.setenv("GITHUB_TOKEN", "tkn")
    cfg = load_config(cfg_path)
    assert cfg.github.token == "tkn"
    assert cfg.llm.provider == "cursor"  # default per project decision
    assert cfg.llm.cursor.binary == "cursor-agent"


def test_missing_token_raises(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.toml"
    write_skeleton(cfg_path)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(ConfigError):
        load_config(cfg_path)


def test_skeleton_refuses_overwrite(tmp_path):
    cfg_path = tmp_path / "config.toml"
    write_skeleton(cfg_path)
    with pytest.raises(ConfigError):
        write_skeleton(cfg_path)


def test_unknown_provider_rejected(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("""
[github]
token = "x"
repositories = []

[llm]
provider = "totally-fake"
""")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(ConfigError):
        load_config(cfg_path)
