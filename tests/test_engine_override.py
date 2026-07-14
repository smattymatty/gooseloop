"""Engine selection: positional/-e override vs gooseloop.toml's default_engine,
and short-name resolution (ADR 0009)."""

import sys
from pathlib import Path

import pytest

from gooseloop.__main__ import _load_engine_and_environment
from gooseloop.config import LooperConfig, resolve_engine_module


def _config(tmp_path: Path, default_engine: str = "engines.hello_world") -> LooperConfig:
    return LooperConfig(
        default_model="x",
        sessions_dir=tmp_path / "sessions",
        default_engine=default_engine,
        environment_config=None,
        max_queue_depth=50,
        review_recipe="review.yaml",
        summary_recipe="summary.yaml",
        anchor=Path.cwd(),
    )


def test_no_override_uses_config_default_engine(tmp_path):
    cfg = _config(tmp_path, default_engine="engines.hello_world")
    engine, environment, module = _load_engine_and_environment(cfg)
    assert engine.name == "hello-world"
    assert module == "engines.hello_world"


def test_override_supersedes_config_default_engine(tmp_path):
    cfg = _config(tmp_path, default_engine="engines.hello_world")
    engine, environment, module = _load_engine_and_environment(cfg, engine_override="engines.git_recap")
    assert engine.name == "git-recap"
    assert module == "engines.git_recap"


def test_short_name_override_resolves_by_convention(tmp_path):
    """`gooseloop run doc_drift` — the short name resolves to
    engines.doc_drift via the loop root's top-level package scan."""
    cfg = _config(tmp_path)  # anchor is this repo's root: engines/ exists
    engine, environment, module = _load_engine_and_environment(cfg, engine_override="git_recap")
    assert engine.name == "git-recap"
    assert module == "engines.git_recap", "lock/meta must record the RESOLVED module, not the short name"


def test_short_default_engine_in_config_also_resolves(tmp_path):
    """default_engine itself may be a short name."""
    cfg = _config(tmp_path, default_engine="doc_drift")
    engine, environment, module = _load_engine_and_environment(cfg)
    assert engine.name == "doc-drift"
    assert module == "engines.doc_drift"


def test_bad_override_fails_with_module_name_in_error(tmp_path):
    cfg = _config(tmp_path)
    with pytest.raises(SystemExit, match="engines.does_not_exist"):
        _load_engine_and_environment(cfg, engine_override="engines.does_not_exist")


def test_unknown_short_name_fails_with_actionable_message(tmp_path):
    cfg = _config(tmp_path)
    with pytest.raises(SystemExit, match="no engine named 'nope_engine'"):
        _load_engine_and_environment(cfg, engine_override="nope_engine")


# ---- resolve_engine_module unit tests -------------------------------


def test_resolver_passes_dotted_names_through(tmp_path):
    assert resolve_engine_module(tmp_path, "a.b.c") == "a.b.c"


def test_resolver_finds_top_level_module_and_package(tmp_path):
    (tmp_path / "solo.py").write_text("")
    assert resolve_engine_module(tmp_path, "solo") == "solo"
    pkg = tmp_path / "pkg_engine"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    assert resolve_engine_module(tmp_path, "pkg_engine") == "pkg_engine"


def test_resolver_scans_subpackages_of_top_level_packages(tmp_path):
    (tmp_path / "engines").mkdir()
    (tmp_path / "engines" / "__init__.py").write_text("")
    (tmp_path / "engines" / "drift").mkdir()
    (tmp_path / "engines" / "drift" / "__init__.py").write_text("")
    assert resolve_engine_module(tmp_path, "drift") == "engines.drift"


def test_resolver_refuses_ambiguity(tmp_path):
    for parent in ("engines", "more_engines"):
        d = tmp_path / parent / "drift"
        d.mkdir(parents=True)
        (tmp_path / parent / "__init__.py").write_text("")
        (d / "__init__.py").write_text("")
    with pytest.raises(LookupError, match="ambiguous"):
        resolve_engine_module(tmp_path, "drift")


def test_resolver_missing_name_raises_lookup_error(tmp_path):
    with pytest.raises(LookupError, match="no engine named"):
        resolve_engine_module(tmp_path, "ghost")
