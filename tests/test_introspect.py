"""gooseloop.introspect: env_method listing + context-source dry-run.

The tooling half of PROTOCOL §7. The load-bearing promises: previews
never read file bodies and never call environment methods, and the
qualification rule for env_methods mirrors what render time accepts.
"""

from __future__ import annotations

from gooseloop.environment import Environment
from gooseloop.introspect import (
    list_env_methods,
    preview_recipe_context,
    preview_source,
)


class _RichEnv(Environment):
    def env_vars(self) -> dict[str, str]:
        return {"DATA_DIR": "/tmp/nowhere"}

    def journal_text(self) -> str:
        """Founder journal, most recent first."""
        return "journal"

    def undocumented(self):
        return "ok"

    def needs_an_arg(self, x: str) -> str:
        return x

    def returns_wrong_type(self) -> int:
        return 42

    def _private_helper(self) -> str:
        return "hidden"


# ---- list_env_methods ---------------------------------------------

def test_list_env_methods_applies_the_qualification_rule():
    names = [m.name for m in list_env_methods(_RichEnv())]
    assert "journal_text" in names       # documented, zero-arg, -> str
    assert "undocumented" in names       # unannotated: can't know, include
    assert "needs_an_arg" not in names   # requires an argument
    assert "returns_wrong_type" not in names  # annotated non-str return
    assert "_private_helper" not in names     # underscore-private
    assert "env_vars" not in names       # the ABC's own contract


def test_list_env_methods_surfaces_first_doc_line():
    methods = {m.name: m.doc for m in list_env_methods(_RichEnv())}
    assert methods["journal_text"] == "Founder journal, most recent first."
    assert methods["undocumented"] == ""


def test_list_env_methods_none_environment_is_empty():
    assert list_env_methods(None) == []


# ---- preview_source: files and globs -------------------------------

def test_preview_file_reports_size_without_reading(tmp_path):
    f = tmp_path / "notes.md"
    f.write_text("12345")
    p = preview_source(f"file:{f}", {})
    assert p.ok and p.kind == "file"
    assert p.matches[0].size == 5
    assert "5 bytes" in p.detail


def test_preview_file_substitutes_env_vars(tmp_path):
    f = tmp_path / "j.md"
    f.write_text("x")
    p = preview_source("file:${ROOT}/j.md", {"ROOT": str(tmp_path)})
    assert p.ok
    assert p.resolved == str(f)


def test_preview_missing_file_fails_with_reason(tmp_path):
    p = preview_source(f"file:{tmp_path}/nope.md", {})
    assert not p.ok
    assert "does not exist" in p.detail


def test_preview_env_file_unset_var_fails():
    p = preview_source("env_file:NOT_SET_ANYWHERE", {})
    assert not p.ok
    assert "unset" in p.detail
    assert p.resolved == "NOT_SET_ANYWHERE"


def test_preview_env_file_set_and_present(tmp_path):
    f = tmp_path / "review.json"
    f.write_text("{}")
    p = preview_source("env_file:REVIEW_JSON_PATH", {"REVIEW_JSON_PATH": str(f)})
    assert p.ok
    assert p.matches[0].path == str(f)


def test_preview_glob_lists_sorted_matches_with_sizes(tmp_path):
    (tmp_path / "b.txt").write_text("bb")
    (tmp_path / "a.txt").write_text("a")
    p = preview_source("glob:${DIR}/*.txt", {"DIR": str(tmp_path)})
    assert p.ok
    assert [m.path for m in p.matches] == [str(tmp_path / "a.txt"), str(tmp_path / "b.txt")]
    assert [m.size for m in p.matches] == [1, 2]
    assert "2 file(s), 3 bytes" in p.detail


def test_preview_glob_no_matches_is_not_ok(tmp_path):
    p = preview_source(f"glob:{tmp_path}/*.nope", {})
    assert not p.ok
    assert p.matches == ()
    assert "no files matched" in p.detail


# ---- preview_source: env_method -------------------------------------

def test_preview_env_method_never_calls_the_method():
    class _Tripwire(Environment):
        called = False

        def env_vars(self) -> dict[str, str]:
            return {}

        def expensive_digest(self) -> str:
            _Tripwire.called = True
            return "digest"

    env = _Tripwire()
    p = preview_source("env_method:expensive_digest", {}, environment=env)
    assert p.ok
    assert "not called" in p.detail
    assert _Tripwire.called is False


def test_preview_env_method_without_environment_fails():
    p = preview_source("env_method:anything", {}, environment=None)
    assert not p.ok
    assert "no Environment" in p.detail


def test_preview_env_method_unknown_name_fails():
    p = preview_source("env_method:nope", {}, environment=_RichEnv())
    assert not p.ok
    assert "no callable" in p.detail


# ---- preview_source: malformed sources -------------------------------

def test_preview_unknown_kind_fails():
    p = preview_source("ftp:whatever", {})
    assert not p.ok
    assert "unknown source kind" in p.detail


def test_preview_missing_kind_prefix_fails():
    p = preview_source("just-a-path.md", {})
    assert not p.ok
    assert "missing 'kind:' prefix" in p.detail


# ---- preview_recipe_context ------------------------------------------

def test_preview_recipe_context_maps_labels_and_optional(tmp_path):
    f = tmp_path / "data.txt"
    f.write_text("x")
    recipe = {
        "prompt": "...",
        "context": [
            {"label": "DATA", "source": f"file:{f}"},
            {"label": "MAYBE", "source": "env_file:UNSET_VAR", "optional": True},
        ],
    }
    previews = preview_recipe_context(recipe, {})
    assert [p.label for p in previews] == ["DATA", "MAYBE"]
    assert previews[0].preview.ok and not previews[0].optional
    assert not previews[1].preview.ok and previews[1].optional


def test_preview_recipe_context_no_context_block_is_empty():
    assert preview_recipe_context({"prompt": "hi"}, {}) == []


def test_env_file_ok_when_engine_declares_injection():
    """An env_file var the engine injects at phase-build time (declared
    via Engine.injected_env) previews OK — not a false 'unset' failure.
    The doc_drift CONTEXT_FILE chip was the motivating red herring."""
    from gooseloop.introspect import preview_source

    declared = {"CONTEXT_FILE": "per-pair bundle the engine writes"}
    p = preview_source("env_file:CONTEXT_FILE", {}, injected_env=declared)
    assert p.ok is True
    assert "injected per phase" in p.detail

    undeclared = preview_source("env_file:CONTEXT_FILE", {})
    assert undeclared.ok is False
    assert "unset" in undeclared.detail
