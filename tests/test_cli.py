"""CLI smoke tests: `gooseloop` is the front door of the published package.

Covers the argparse wiring, the recipe --resolve merge pipeline, engine
discovery from gooseloop.toml, and the run subcommand's exit-code
contract (0 on a clean pass, 1 when the review errors). goose itself is
never invoked — the looper's two seams are patched as in
test_branch_policy_and_looper.py.
"""

from __future__ import annotations

import contextlib
import json
import textwrap

import pytest
import yaml

from gooseloop.__main__ import main


# ---- harness ------------------------------------------------------

@contextlib.contextmanager
def _unprepared(recipe_path, extra_env=None, **kwargs):
    yield str(recipe_path)


def _canned(mapping):
    def run(recipe_path, model, extra_env=None, *, max_retries=6,
            base_delay=5, success_predicate=None, label=None):
        for stem, output in mapping.items():
            if stem in recipe_path:
                return output
        return ""
    return run


def _patch_goose(monkeypatch, mapping) -> None:
    monkeypatch.setattr("gooseloop.looper.prepared_recipe", _unprepared)
    monkeypatch.setattr("gooseloop.looper.run_goose_with_retry", _canned(mapping))


def _project(tmp_path, monkeypatch, *, engine_module: str) -> None:
    """A minimal consuming project: cwd + gooseloop.toml."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "gooseloop.toml").write_text(
        f'[gooseloop]\nengine_module = "{engine_module}"\n'
    )


_ENGINE_SRC = textwrap.dedent("""
    from gooseloop import Engine, Phase, Pipeline

    class CliEngine(Engine):
        @property
        def name(self):
            return "cli-test"

        def pipeline(self, ctx):
            return Pipeline(
                review=Phase(name="review", recipe_path="review.yaml"),
                body=[],
                summary=None,
            )

    engine = CliEngine
""")


def _review(status: str) -> str:
    return (
        "<<<DELIVERABLE_JSON>>>\n"
        + json.dumps({
            "protocol_version": "1.0",
            "status": status,
            "summary": "cli smoke",
            "insights": [],
            "routing": [],
            "operator_actions": [],
        })
        + "\n<<<END_DELIVERABLE>>>\n"
    )


# ---- top-level parser ---------------------------------------------

def test_help_exits_zero_and_names_the_subcommands(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    for cmd in ("run", "recipe", "engines"):
        assert cmd in out


def test_no_subcommand_is_a_usage_error():
    with pytest.raises(SystemExit) as exc_info:
        main([])
    assert exc_info.value.code == 2


# ---- recipe --resolve ---------------------------------------------

def test_recipe_resolve_prints_the_layered_merge(tmp_path, monkeypatch, capsys):
    _project(tmp_path, monkeypatch, engine_module="unused")
    (tmp_path / "review.yaml").write_text(yaml.safe_dump(
        {"prompt": "base prompt", "settings": {"max_turns": 4, "temperature": 0.2}},
    ))
    (tmp_path / "review.local.yaml").write_text(yaml.safe_dump(
        {"settings": {"max_turns": 8}},
    ))
    (tmp_path / "exp.yaml").write_text(yaml.safe_dump(
        {"prompt": "experimental prompt"},
    ))

    # Bare name: --resolve review finds review.yaml.
    rc = main(["recipe", "--resolve", "review", "--overlay", "exp.yaml"])
    assert rc == 0
    merged = yaml.safe_load(capsys.readouterr().out)
    assert merged["prompt"] == "experimental prompt"     # CLI overlay wins
    assert merged["settings"]["max_turns"] == 8          # local overrode base
    assert merged["settings"]["temperature"] == 0.2      # base survives


def test_recipe_resolve_missing_recipe_fails_with_message(tmp_path, monkeypatch, capsys):
    _project(tmp_path, monkeypatch, engine_module="unused")
    rc = main(["recipe", "--resolve", "nope"])
    assert rc == 1
    assert "recipe not found" in capsys.readouterr().err


def test_recipe_without_resolve_is_a_usage_error(tmp_path, monkeypatch, capsys):
    _project(tmp_path, monkeypatch, engine_module="unused")
    rc = main(["recipe"])
    assert rc == 2
    assert "usage" in capsys.readouterr().err


# ---- engines ------------------------------------------------------

def test_engines_reports_the_configured_engine_class(tmp_path, monkeypatch, capsys):
    _project(tmp_path, monkeypatch, engine_module="cli_engine_show")
    (tmp_path / "cli_engine_show.py").write_text(_ENGINE_SRC)
    monkeypatch.syspath_prepend(str(tmp_path))
    rc = main(["engines"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "cli_engine_show" in out
    assert "CliEngine" in out


def test_engines_unimportable_module_fails(tmp_path, monkeypatch, capsys):
    _project(tmp_path, monkeypatch, engine_module="definitely_not_a_module")
    rc = main(["engines"])
    assert rc == 1
    assert "import failed" in capsys.readouterr().err


# ---- run: exit-code contract --------------------------------------

def test_run_clean_pass_exits_zero(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch, engine_module="cli_engine_ok")
    (tmp_path / "cli_engine_ok.py").write_text(_ENGINE_SRC)
    _patch_goose(monkeypatch, {"review.yaml": _review("done")})
    rc = main(["run", "--no-save"])
    assert rc == 0


def test_run_review_error_exits_one(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch, engine_module="cli_engine_err")
    (tmp_path / "cli_engine_err.py").write_text(_ENGINE_SRC)
    _patch_goose(monkeypatch, {"review.yaml": "no sentinels at all\n"})
    rc = main(["run", "--no-save"])
    assert rc == 1


def test_run_engine_module_without_engine_attr_aborts(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch, engine_module="cli_engine_empty")
    (tmp_path / "cli_engine_empty.py").write_text("x = 1\n")
    with pytest.raises(SystemExit, match="no `engine` attribute"):
        main(["run", "--no-save"])
