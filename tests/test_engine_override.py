"""-e / --engine flag overrides gooseloop.toml's engine_module."""

import sys
from pathlib import Path

import pytest

from gooseloop.__main__ import _load_engine_and_environment
from gooseloop.config import LooperConfig


def _config(tmp_path: Path, engine_module: str = "engines.hello_world") -> LooperConfig:
    return LooperConfig(
        default_model="x",
        sessions_dir=tmp_path / "sessions",
        engine_module=engine_module,
        environment_config=None,
        max_queue_depth=50,
        review_recipe="review.yaml",
        summary_recipe="summary.yaml",
        anchor=Path.cwd(),
    )


def test_no_override_uses_config_engine_module(tmp_path):
    cfg = _config(tmp_path, engine_module="engines.hello_world")
    engine, environment = _load_engine_and_environment(cfg)
    assert engine.name == "hello-world"


def test_override_supersedes_config_engine_module(tmp_path):
    cfg = _config(tmp_path, engine_module="engines.hello_world")
    engine, environment = _load_engine_and_environment(cfg, engine_override="engines.git_recap")
    assert engine.name == "git-recap"


def test_bad_override_fails_with_module_name_in_error(tmp_path):
    cfg = _config(tmp_path)
    with pytest.raises(SystemExit, match="engines.does_not_exist"):
        _load_engine_and_environment(cfg, engine_override="engines.does_not_exist")
