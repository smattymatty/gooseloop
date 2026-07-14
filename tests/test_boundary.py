"""THE BOUNDARY (PROTOCOL section 15): bwrap masking around goose spawns.

Unit tests cover the .gooseignore parser, mask discovery, prefix shape,
and the refuse/nudge decision table. The live test (skipped where
bubblewrap is absent) proves the actual property the boundary sells:
inside the sandbox, a masked file reads as empty and a masked directory
lists as empty.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from gooseloop.boundary import (
    BUILTIN_DENY,
    GOOSEIGNORE_FILENAME,
    Boundary,
    BoundaryUnavailableError,
    bwrap_prefix,
    find_masks,
    load_gooseignore,
    resolve_sandbox,
)

HAVE_BWRAP = shutil.which("bwrap") is not None


# ---- .gooseignore parsing ---------------------------------------------


def test_missing_file_means_no_extra_patterns(tmp_path):
    assert load_gooseignore(tmp_path) == []


def test_comments_and_blanks_are_skipped(tmp_path):
    (tmp_path / GOOSEIGNORE_FILENAME).write_text(
        "# secrets\n\n*.sqlite\n  journal/private\n"
    )
    assert load_gooseignore(tmp_path) == ["*.sqlite", "journal/private"]


def test_negation_is_refused_loudly(tmp_path):
    (tmp_path / GOOSEIGNORE_FILENAME).write_text("*.pem\n!ok.pem\n")
    with pytest.raises(ValueError, match="negation"):
        load_gooseignore(tmp_path)


# ---- mask discovery ----------------------------------------------------


def test_basename_glob_masks_files_anywhere_under_scope(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / ".env").write_text("SECRET=x")
    (tmp_path / ".env.local").write_text("SECRET=y")
    (tmp_path / "safe.txt").write_text("fine")
    masks = find_masks([".env*"], [tmp_path])
    assert set(masks) == {tmp_path / "a" / ".env", tmp_path / ".env.local"}


def test_matched_directory_is_masked_whole_and_children_deduped(tmp_path):
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "id_rsa").write_text("key")
    masks = find_masks([".ssh", "id_rsa*"], [tmp_path])
    # Parent wins: one mount for the directory, no child entry.
    assert masks == [ssh]


def test_anchored_pattern_included_only_when_it_exists(tmp_path):
    target = tmp_path / "vault" / "creds"
    target.mkdir(parents=True)
    present = find_masks([f"{tmp_path}/vault/creds"], [])
    absent = find_masks([f"{tmp_path}/vault/nope"], [])
    assert present == [target]
    assert absent == []


def test_builtin_deny_catches_the_incident_shapes(tmp_path):
    """The floor must mask the exact shapes a hostile phase goes for."""
    (tmp_path / ".env").write_text("x")
    (tmp_path / "deploy.pem").write_text("x")
    (tmp_path / "credentials.json").write_text("x")
    masks = find_masks(list(BUILTIN_DENY), [tmp_path])
    names = {m.name for m in masks}
    assert {".env", "deploy.pem", "credentials.json"} <= names


# ---- prefix shape ------------------------------------------------------


def test_prefix_shape_dirs_tmpfs_files_empty_bind(tmp_path):
    d = tmp_path / "secrets"
    d.mkdir()
    f = tmp_path / ".env"
    f.write_text("x")
    prefix = bwrap_prefix([d, f])
    assert prefix[:4] == ["bwrap", "--die-with-parent", "--dev-bind", "/"]
    assert prefix[-1] == "--"
    joined = " ".join(prefix)
    assert f"--tmpfs {d}" in joined
    # File masks bind an empty regular file (NOT /dev/null: nodev mounts
    # answer device reads with EPERM, where the contract says "empty").
    i = prefix.index(str(f))
    assert prefix[i - 2] == "--ro-bind"
    source = Path(prefix[i - 1])
    assert source.is_file() and source.stat().st_size == 0


# ---- resolve decision table -------------------------------------------


def test_no_bwrap_no_file_nudges_and_returns_none(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("gooseloop.boundary.bwrap_available", lambda: False)
    assert resolve_sandbox(tmp_path, home=tmp_path) is None
    assert "bubblewrap not found" in capsys.readouterr().err


def test_no_bwrap_with_file_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr("gooseloop.boundary.bwrap_available", lambda: False)
    (tmp_path / GOOSEIGNORE_FILENAME).write_text("*.pem\n")
    with pytest.raises(BoundaryUnavailableError, match="bubblewrap"):
        resolve_sandbox(tmp_path, home=tmp_path)


def test_resolve_extends_builtin_floor_with_gooseignore(tmp_path, monkeypatch):
    monkeypatch.setattr("gooseloop.boundary.bwrap_available", lambda: True)
    (tmp_path / ".env").write_text("x")          # builtin floor
    (tmp_path / "extra.sqlite").write_text("x")  # gooseignore extension
    (tmp_path / GOOSEIGNORE_FILENAME).write_text("*.sqlite\n")
    b = resolve_sandbox(tmp_path, home=tmp_path)
    assert isinstance(b, Boundary)
    names = {m.name for m in b.masks}
    assert {".env", "extra.sqlite"} <= names


def test_anchor_outside_home_is_scanned_too(tmp_path, monkeypatch):
    monkeypatch.setattr("gooseloop.boundary.bwrap_available", lambda: True)
    home = tmp_path / "home"
    anchor = tmp_path / "elsewhere"
    home.mkdir()
    anchor.mkdir()
    (anchor / ".env").write_text("x")
    b = resolve_sandbox(anchor, home=home)
    assert b is not None
    assert anchor / ".env" in b.masks


# ---- looper wiring -----------------------------------------------------


def test_looper_threads_the_prefix_into_every_goose_spawn(tmp_path, monkeypatch):
    """One resolve per pass; every phase's goose call carries the prefix;
    the session log names the mask count."""
    import contextlib
    import json

    from gooseloop import Engine, Environment, GooseLooper, LooperConfig, Phase, Pipeline

    review_output = (
        "<<<DELIVERABLE_JSON>>>\n"
        + json.dumps({
            "protocol_version": "1.0", "status": "done", "summary": "s",
            "insights": [], "routing": [], "operator_actions": [],
        })
        + "\n<<<END_DELIVERABLE>>>\n"
    )

    class _Env(Environment):
        def env_vars(self):
            return {}

    class _E(Engine):
        @property
        def name(self):
            return "boundary-test"

        def pipeline(self, ctx):
            return Pipeline(
                review=Phase(name="review", recipe_path="review.yaml"),
                summary=Phase(name="summary", recipe_path="summary.yaml"),
            )

    fake = Boundary(prefix=["bwrap", "--dev-bind", "/", "/", "--"],
                    masks=[Path("/x/.env"), Path("/x/.ssh")])
    resolved: list[Path] = []

    def fake_resolve(anchor):
        resolved.append(anchor)
        return fake

    seen_sandboxes = []

    def fake_run(recipe_path, model, extra_env=None, *, sandbox=None, **kwargs):
        seen_sandboxes.append(sandbox)
        return review_output if "review" in recipe_path else "summary text"

    @contextlib.contextmanager
    def unprepared(recipe_path, extra_env=None, **kwargs):
        yield str(recipe_path)

    monkeypatch.setattr("gooseloop.looper.resolve_sandbox", fake_resolve)
    monkeypatch.setattr("gooseloop.looper.prepared_recipe", unprepared)
    monkeypatch.setattr("gooseloop.looper.run_goose_with_retry", fake_run)

    looper = GooseLooper(
        engine=_E(), environment=_Env(),
        config=LooperConfig.load(anchor=tmp_path, warn_on_missing=False),
        save=True,
    )
    result = looper.begin_loop()
    session_dir = Path(result["session_dir"])

    assert resolved == [tmp_path]  # once per pass
    # Both phases got the SAME prefix: the fake's masks plus the session's
    # own mask map, self-masked as the final entry (the map of where
    # secrets live is denied to the goose the same as what it maps).
    assert len(seen_sandboxes) == 2  # review + summary
    assert seen_sandboxes[0] == seen_sandboxes[1]
    prefix = seen_sandboxes[0]
    joined = " ".join(prefix)
    assert "/x/.env" in joined and "/x/.ssh" in joined
    artifact = session_dir / "boundary-masks.json"
    assert str(artifact) in joined  # the map itself is masked
    log = session_dir / "session.log"
    assert "boundary: 2 paths masked" in log.read_text()

    # The artifact records patterns + exact masks, ~-shortened, paths only.
    payload = json.loads(artifact.read_text())
    assert payload["enforced"] is True
    assert payload["mask_count"] == 2
    assert payload["masks"] == ["/x/.env", "/x/.ssh"]


# ---- the live proof ----------------------------------------------------


@pytest.mark.skipif(not HAVE_BWRAP, reason="bubblewrap not installed")
def test_inside_the_boundary_masked_paths_read_empty(tmp_path):
    secret = tmp_path / ".env"
    secret.write_text("SECRET=live-value\n")
    vault = tmp_path / "vault"          # dir masked whole (anchored pattern)
    vault.mkdir()
    (vault / "id_rsa").write_text("private\n")
    open_file = tmp_path / "open.txt"
    open_file.write_text("visible\n")

    # The floor masks .env (file -> reads empty); an anchored gooseignore
    # entry masks vault/ (dir -> tmpfs, lists empty).
    masks = find_masks(list(BUILTIN_DENY) + [str(vault)], [tmp_path])
    out = subprocess.run(
        bwrap_prefix(masks) + ["sh", "-c",
                               f"cat {secret}; ls -A {vault}; "
                               f"cat {vault}/id_rsa; cat {open_file}"],
        capture_output=True, text=True, timeout=30,
    )
    assert "SECRET" not in out.stdout    # masked file reads empty
    assert "id_rsa" not in out.stdout    # masked dir lists empty
    assert "private" not in out.stdout   # nothing inside it is reachable
    assert "visible" in out.stdout       # everything else untouched


# ---- the mask map (session artifact) ------------------------------------


def test_persist_masks_records_patterns_and_paths(tmp_path):
    from gooseloop.boundary import persist_masks

    b = Boundary(prefix=["bwrap", "--"],
                 masks=[Path.home() / ".ssh", Path("/p/.env")],
                 patterns=[".env*", "*.sqlite"])
    artifact = persist_masks(tmp_path, b)
    assert artifact == tmp_path / "boundary-masks.json"
    import json as _json
    payload = _json.loads(artifact.read_text())
    assert payload["enforced"] is True
    assert payload["patterns"] == [".env*", "*.sqlite"]
    assert "~/.ssh" in payload["masks"]      # home shortened
    assert "/p/.env" in payload["masks"]


def test_persist_masks_records_the_unsandboxed_truth(tmp_path):
    from gooseloop.boundary import persist_masks

    artifact = persist_masks(tmp_path, None)
    import json as _json
    payload = _json.loads(artifact.read_text())
    assert payload == {"enforced": False, "patterns": [],
                       "mask_count": 0, "masks": []}


def test_floor_masks_past_runs_mask_maps(tmp_path):
    """Yesterday's map is as much a secret landscape as today's: the
    artifact's basename is on the BUILTIN_DENY floor, so ordinary scans
    catch every previous session's copy."""
    old = tmp_path / "reviews" / "sessions" / "2026-07-13T00-00-00"
    old.mkdir(parents=True)
    (old / "boundary-masks.json").write_text("{}")
    masks = find_masks(list(BUILTIN_DENY), [tmp_path])
    assert old / "boundary-masks.json" in masks


@pytest.mark.skipif(not HAVE_BWRAP, reason="bubblewrap not installed")
def test_inside_the_boundary_the_mask_map_reads_empty(tmp_path):
    from gooseloop.boundary import persist_masks

    b = Boundary(prefix=[], masks=[], patterns=[])
    artifact = persist_masks(tmp_path, b)
    assert artifact is not None and artifact.stat().st_size > 0
    out = subprocess.run(
        bwrap_prefix([artifact]) + ["cat", str(artifact)],
        capture_output=True, text=True, timeout=30,
    )
    assert out.stdout == ""  # operator sees the map; the goose does not
