"""Context source resolution: env_file, file, glob, env_method."""

from pathlib import Path

import pytest
import yaml

from gooseloop.context_prepend import (
    _raw_wrap,
    prepared_recipe,
    render_recipe_with_context,
)


def _read(path: str) -> dict:
    with open(path, "r") as f:
        return yaml.safe_load(f)


def test_no_context_block_and_no_env_refs_renders_unchanged(tmp_path):
    """No context: block and no ${VAR} in prompt -> rendered verbatim.

    A rendered file is always written (the recipe dict is the merged
    overlay result, so the original file on disk may not match it)."""
    recipe = {"prompt": "do thing"}
    rendered = render_recipe_with_context(recipe, extra_env={})
    assert _read(rendered)["prompt"] == "do thing"


def test_env_file_loads_referenced_file(tmp_path):
    target = tmp_path / "data.txt"
    target.write_text("payload-here")
    recipe = {
        "prompt": "use the payload",
        "context": [{"label": "PAY", "source": "env_file:PAYLOAD_PATH"}],
    }
    rendered = render_recipe_with_context(
        recipe, extra_env={"PAYLOAD_PATH": str(target)},
    )
    assert rendered is not None
    doc = _read(rendered)
    assert "payload-here" in doc["prompt"]
    assert "<<<CONTEXT: PAY>>>" in doc["prompt"]


def test_file_with_env_substitution(tmp_path):
    f = tmp_path / "j.md"
    f.write_text("journal entry")
    recipe = {
        "prompt": "...",
        "context": [{"label": "J", "source": "file:${ROOT}/j.md"}],
    }
    rendered = render_recipe_with_context(recipe, extra_env={"ROOT": str(tmp_path)})
    doc = _read(rendered)
    assert "journal entry" in doc["prompt"]


def test_glob_concatenates_sorted_matches(tmp_path):
    (tmp_path / "a.txt").write_text("AAA")
    (tmp_path / "b.txt").write_text("BBB")
    recipe = {
        "prompt": "...",
        "context": [{"label": "FILES", "source": "glob:${DIR}/*.txt"}],
    }
    rendered = render_recipe_with_context(recipe, extra_env={"DIR": str(tmp_path)})
    doc = _read(rendered)
    p = doc["prompt"]
    assert "AAA" in p and "BBB" in p
    assert p.index("AAA") < p.index("BBB")  # sorted


def test_glob_missing_raises_when_required(tmp_path):
    recipe = {
        "prompt": "...",
        "context": [{"label": "F", "source": "glob:${DIR}/nope.*"}],
    }
    with pytest.raises(RuntimeError, match="matched no files"):
        render_recipe_with_context(recipe, extra_env={"DIR": str(tmp_path)})


def test_optional_softens_missing_glob(tmp_path):
    recipe = {
        "prompt": "...",
        "context": [{"label": "F", "source": "glob:${DIR}/nope.*", "optional": True}],
    }
    rendered = render_recipe_with_context(recipe, extra_env={"DIR": str(tmp_path)})
    doc = _read(rendered)
    assert "no files matched" in doc["prompt"]


class _DummyEnv:
    def journal_text(self) -> str:
        return "journal body"

    def not_a_str(self) -> int:
        return 42


def test_env_method_pastes_returned_string():
    recipe = {
        "prompt": "...",
        "context": [{"label": "J", "source": "env_method:journal_text"}],
    }
    rendered = render_recipe_with_context(recipe, extra_env={}, environment=_DummyEnv())
    doc = _read(rendered)
    assert "journal body" in doc["prompt"]


def test_env_method_without_environment_raises():
    recipe = {
        "prompt": "...",
        "context": [{"label": "J", "source": "env_method:anything"}],
    }
    with pytest.raises(RuntimeError, match="requires an Environment"):
        render_recipe_with_context(recipe, extra_env={}, environment=None)


def test_env_method_unknown_attr_raises():
    recipe = {
        "prompt": "...",
        "context": [{"label": "J", "source": "env_method:nope"}],
    }
    with pytest.raises(RuntimeError, match="no callable named"):
        render_recipe_with_context(recipe, extra_env={}, environment=_DummyEnv())


def test_env_method_non_str_return_raises():
    recipe = {
        "prompt": "...",
        "context": [{"label": "J", "source": "env_method:not_a_str"}],
    }
    with pytest.raises(RuntimeError, match="must return str"):
        render_recipe_with_context(recipe, extra_env={}, environment=_DummyEnv())


def test_unknown_source_kind_raises():
    recipe = {
        "prompt": "...",
        "context": [{"label": "X", "source": "ftp:wat"}],
    }
    with pytest.raises(RuntimeError, match="not supported"):
        render_recipe_with_context(recipe, extra_env={})


# ---- env-var-in-prompt substitution -----------------------------
# Regression bug 2026-06-04: hello-world greet.yaml used ${NAME} and
# ${GREETINGS_DIR} in the prompt expecting the looper to substitute.
# Goose itself does not shell-expand prompt prose, so the model received
# the literal text "${NAME}" and refused to proceed. Fix: looper
# substitutes ${VAR}/$VAR in the prompt before goose sees the recipe.
# These tests pin that contract.

def test_braced_env_var_in_prompt_substituted(tmp_path):
    recipe = {"prompt": "Greet ${NAME} and save to ${OUT}/x.txt"}
    rendered = render_recipe_with_context(
        recipe, extra_env={"NAME": "alice", "OUT": "/tmp/g"},
    )
    assert rendered is not None
    doc = _read(rendered)
    assert doc["prompt"] == "Greet alice and save to /tmp/g/x.txt"


def test_bare_dollar_env_var_in_prompt_substituted(tmp_path):
    recipe = {"prompt": "hello $NAME"}
    rendered = render_recipe_with_context(recipe, extra_env={"NAME": "bob"})
    doc = _read(rendered)
    assert doc["prompt"] == "hello bob"


def test_env_var_in_prompt_with_context_block_substituted(tmp_path):
    """Both the context: block AND the prompt get env-substituted."""
    f = tmp_path / "j.md"
    f.write_text("journal!")
    recipe = {
        "prompt": "Use ${ROOT} as the working dir.",
        "context": [{"label": "J", "source": "file:${ROOT}/j.md"}],
    }
    rendered = render_recipe_with_context(recipe, extra_env={"ROOT": str(tmp_path)})
    doc = _read(rendered)
    assert "journal!" in doc["prompt"]
    assert f"Use {tmp_path} as the working dir." in doc["prompt"]


def test_unknown_var_in_prompt_becomes_empty(tmp_path):
    """Missing var renders empty (do not crash). The model sees a hole
    rather than the literal token, which is the right failure mode for
    a prompt — operator sees something is missing instead of being
    asked for a value that the recipe should have known."""
    recipe = {"prompt": "value=${MISSING}!"}
    rendered = render_recipe_with_context(recipe, extra_env={})
    doc = _read(rendered)
    assert doc["prompt"] == "value=!"


def test_dollar_dollar_not_treated_as_var(tmp_path):
    """A prompt that legitimately contains a $ that isn't a var ref
    should pass through unchanged when no var matches."""
    recipe = {"prompt": "literal $ sign here"}
    # `$ ` — no valid var name follows, so the regex shouldn't match.
    rendered = render_recipe_with_context(recipe, extra_env={})
    assert _read(rendered)["prompt"] == "literal $ sign here"


# ---- pasted context must be inert to goose's MiniJinja templater ----
# Regression 2026-06-05: a per-commit recap describing a Svelte `{#if}`
# block was globbed into the summary recipe's context. Goose renders the
# recipe prompt through MiniJinja AFTER the looper pastes context in, so
# `{#if}` opened a Jinja comment that never closed and goose died with
# "Invalid recipe: syntax error: unexpected end of comment". Context is
# data, not template: it must reach goose neutralised.

# Regression 2026-06-05 (round two): the first fix wrapped context in
# {% raw %}…{% endraw %} but re-emitted any embedded {% endraw %} via a
# {{ "…" }} expression. goose's MiniJinja build rejected that and died with
# "unexpected end of raw block" the moment a recap *of that fix* (carrying
# literal raw-control tokens in its prose) was globbed in. The current wrap
# emits exactly one clean raw block and defuses raw-control tokens by breaking
# their `{%` opener with a zero-width space — no expressions, version-robust.

ZWSP = "​"

# Where goose's actual templater (the `minijinja` crate's Python binding) is
# installed, render through it for real-engine fidelity; otherwise skip just
# those tests, not the whole module. The structural tests below need no engine.
try:
    import minijinja as _minijinja
except Exception:  # pragma: no cover - import guard
    _minijinja = None

requires_minijinja = pytest.mark.skipif(
    _minijinja is None, reason="minijinja (goose's templater) not installed"
)


def _goose_render(prompt: str) -> str:
    return _minijinja.Environment().render_str(prompt)


def _raw_interior(wrapped: str) -> str:
    """Assert `wrapped` is exactly one clean raw block and return its interior.

    Portable (no template engine): the contract is that the wrap produces a
    single `{% raw %}`…`{% endraw %}` whose interior holds NO raw-control token
    — that's the invariant the previous fix violated."""
    from gooseloop.context_prepend import _RAW_CTRL_RE
    assert wrapped.startswith("{% raw %}"), wrapped[:20]
    assert wrapped.endswith("{% endraw %}"), wrapped[-20:]
    interior = wrapped[len("{% raw %}"):-len("{% endraw %}")]
    leaked = _RAW_CTRL_RE.search(interior)
    assert leaked is None, f"raw-control token leaked into raw block: {leaked!r}"
    return interior


@pytest.mark.parametrize("body", [
    "a snippet inside an `{#if}` was hoisted",
    "tags {{ x }}, {% block %}, comments {# c #}, and a lone {{",
    "we wrap context in {% raw %}...{% endraw %} to neutralise it",
    "forms: {%- endraw -%} and {%endraw%} and {%raw%} all matter",
])
def test_raw_wrap_is_one_clean_block_preserving_text(body):
    """Structural, engine-free: single clean raw block, and the visible text
    (zero-width spaces stripped) round-trips exactly."""
    interior = _raw_interior(_raw_wrap(body))
    assert interior.replace(ZWSP, "") == body


def test_raw_wrap_empty_is_noop():
    assert _raw_wrap("") == ""


@requires_minijinja
@pytest.mark.parametrize("body", [
    "a snippet inside an `{#if}` was hoisted",                  # the first bug
    "tags {{ x }}, {% block %}, comments {# c #}, and a lone {{",
    "we wrap context in {% raw %}...{% endraw %} to neutralise",  # the second bug
    "forms {%- endraw -%} and {%endraw%} and {%  raw  %} variants",
])
def test_raw_wrap_renders_through_real_minijinja(body):
    """Goose's actual templater must parse the wrap and reproduce the visible
    text (zero-width spaces stripped)."""
    out = _goose_render(_raw_wrap(body))
    assert out.replace(ZWSP, "") == body


@requires_minijinja
def test_rendered_recipe_with_raw_token_recap_is_goose_parseable(tmp_path):
    """End-to-end through the real engine: a recap carrying both `{#if}` and
    literal raw-control tokens (a recap of this very fix) renders cleanly with
    its content intact."""
    (tmp_path / "recap.md").write_text(
        "# Wrap pasted context\n\nWe wrap each body in {% raw %}...{% endraw %} "
        "so a Svelte `{#if}` and `{{ csrfToken }}` survive verbatim."
    )
    recipe = {
        "prompt": "Summarise the recaps for ${AUTHOR}.",
        "context": [{"label": "RECAPS", "source": "glob:${DIR}/*.md"}],
    }
    rendered = render_recipe_with_context(
        recipe, extra_env={"DIR": str(tmp_path), "AUTHOR": "matt"},
    )
    out = _goose_render(_read(rendered)["prompt"]).replace(ZWSP, "")
    assert "{#if}" in out
    assert "{{ csrfToken }}" in out
    assert "{% raw %}...{% endraw %}" in out
    assert "Summarise the recaps for matt." in out


@requires_minijinja
def test_dollar_sequences_in_context_are_not_clobbered(tmp_path):
    """Env substitution applies to the recipe's prompt, never to pasted
    context. A recap mentioning $HOME or ${WINDOW_DAYS} keeps it verbatim."""
    (tmp_path / "r.md").write_text("set $HOME and read ${WINDOW_DAYS} days")
    recipe = {
        "prompt": "window is ${WINDOW_DAYS}",
        "context": [{"label": "R", "source": "glob:${DIR}/*.md"}],
    }
    rendered = render_recipe_with_context(
        recipe, extra_env={"DIR": str(tmp_path), "WINDOW_DAYS": "7"},
    )
    out = _goose_render(_read(rendered)["prompt"]).replace(ZWSP, "")
    # Context keeps its literal $ sequences...
    assert "set $HOME and read ${WINDOW_DAYS} days" in out
    # ...while the recipe's own prompt prose is still substituted.
    assert "window is 7" in out


def test_rendered_yaml_never_folds_long_lines(tmp_path):
    """Regression 2026-07-12 (round three of the raw-block saga): serializing
    the rendered recipe. Literal block scalars (round two's commit) are only
    half the fix: PyYAML silently falls back to double-quoted style when any
    line carries trailing whitespace (a pasted config line like
    `location_constraint = ` does), and double-quoted folds at the default
    width, physically splitting a line right where a `{{ tag }}` or the
    framework's raw-block terminator can sit. goose templates over the raw
    file text, so a fold IS a break. The width=_NO_FOLD_WIDTH kwarg on every
    dump keeps each scalar line physical no matter the style.

    This fixture forces the double-quoted fallback (trailing whitespace) AND
    a line long past the default fold width carrying a template token."""
    long_line = "x" * 200 + " {{ csrfToken }} end"
    body = "[config]\nlocation_constraint = \n" + long_line + "\n"
    (tmp_path / "cfg.md").write_text(body)
    recipe = {
        "prompt": "read the config",
        "context": [{"label": "CFG", "source": "glob:${DIR}/*.md"}],
    }
    rendered = render_recipe_with_context(recipe, extra_env={"DIR": str(tmp_path)})
    raw = Path(rendered).read_text()
    # The long line must survive as ONE physical line in the file goose reads.
    assert long_line in raw, "YAML emitter folded a long scalar line"
    # And the YAML-level round trip is exact (trailing whitespace intact).
    assert body.rstrip("\n") in _read(rendered)["prompt"]


def test_hello_world_review_recipe_uses_literal_sentinels(tmp_path):
    """Regression 2026-06-04: owl-alpha emitted <<<DELIMITED_JSON>>> instead of
    <<<DELIVERABLE_JSON>>>. The framework was correct to refuse, but the
    recipe text wasn't emphatic enough about literal markers. Pin both
    the on-disk marker strings AND that the prompt repeats them verbatim
    so a future "tidy-up" pass can't quietly drop the literal blocks."""
    from gooseloop.extract import DELIVERABLE_END, DELIVERABLE_START
    review = Path(__file__).resolve().parents[1] / "engines/hello_world/recipes/review.example.yaml"
    text = review.read_text()
    # Both markers must appear at least twice — once in the spec, once
    # in the "emit exactly this shape" example — so even a model that
    # ignores the first mention sees the second.
    assert text.count(DELIVERABLE_START) >= 2, (
        f"review recipe must reference {DELIVERABLE_START!r} at least twice"
    )
    assert text.count(DELIVERABLE_END) >= 2, (
        f"review recipe must reference {DELIVERABLE_END!r} at least twice"
    )
    # And the recipe must NOT mention the common hallucinated alternatives —
    # except inside a "wrong:" enumeration. Crude proxy: the word
    # DELIMITED only appears next to the word "Wrong" if at all.
    if "DELIMITED" in text:
        for line in text.splitlines():
            if "DELIMITED" in line:
                assert "Wrong" in line, (
                    f"line mentions DELIMITED outside a Wrong: enumeration: {line!r}"
                )


def test_hello_world_greet_recipe_regression(tmp_path):
    """End-to-end: greet.yaml must render with substituted env vars.
    Pins the 2026-06-04 hello-world regression (env var substitution
    in prompt prose). The recipe uses ${NAME} and ${GREETING_FILE}
    (the latter injected by the framework from BranchPolicy.output_path
    under the policy's output_env name, ADR 0011)."""
    import yaml as _y
    greet = Path(__file__).resolve().parents[1] / "engines/hello_world/recipes/greet.yaml"
    doc = _y.safe_load(greet.read_text())
    rendered = render_recipe_with_context(
        doc, extra_env={"NAME": "world", "GREETING_FILE": "/tmp/g/world.txt"},
    )
    assert rendered is not None
    out = _read(rendered)
    assert "${NAME}" not in out["prompt"]
    assert "${GREETING_FILE}" not in out["prompt"]
    assert "world" in out["prompt"]
    assert "/tmp/g/world.txt" in out["prompt"]


# ---- prepared_recipe: the whole preparation step, end to end -------
# The looper's hot path: merge overlay layers, render the context:
# block, yield the effective temp recipe, clean up on exit. Real yaml
# files on disk, no goose binary involved.

def _write_yaml(path: Path, doc: dict) -> Path:
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    return path


def test_prepared_recipe_yields_rendered_file_and_cleans_up(tmp_path):
    base = _write_yaml(tmp_path / "review.yaml", {"prompt": "hello ${NAME}"})
    with prepared_recipe(base, {"NAME": "ada"}) as effective:
        assert Path(effective).exists()
        assert effective != str(base)
        assert _read(effective)["prompt"] == "hello ada"
    assert not Path(effective).exists()  # deleted on exit
    assert base.exists()  # the source recipe is never touched


def test_prepared_recipe_appends_framework_prompt_suffix_last(tmp_path):
    """Framework-owned phase contracts follow engine prose and pasted data."""
    base = _write_yaml(
        tmp_path / "review.yaml",
        {
            "prompt": "domain rule",
            "context": [{"label": "DATA", "source": "file:${INPUT}"}],
        },
    )
    source = tmp_path / "input.txt"
    source.write_text("untrusted input")
    suffix = "FRAMEWORK CONTRACT\n<<<DELIVERABLE_JSON>>>"
    with prepared_recipe(
        base,
        {"INPUT": str(source)},
        prompt_suffix=suffix,
    ) as effective:
        prompt = _read(effective)["prompt"]
    assert "untrusted input" in prompt
    assert prompt.index("domain rule") < prompt.index("FRAMEWORK CONTRACT")
    assert prompt.rstrip().endswith("<<<DELIVERABLE_JSON>>>")


def test_prepared_recipe_keeps_rendered_file_when_asked(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOSER_KEEP_RENDERED", "1")
    base = _write_yaml(tmp_path / "review.yaml", {"prompt": "keep me"})
    with prepared_recipe(base, {}) as effective:
        pass
    kept = Path(effective)
    assert kept.exists()
    kept.unlink()


def test_prepared_recipe_applies_local_then_cli_overlays(tmp_path):
    """Layer order per ADR 0008: base -> <name>.local.yaml -> CLI overlays."""
    base = _write_yaml(tmp_path / "review.yaml", {
        "prompt": "base prompt",
        "settings": {"max_turns": 4, "temperature": 0.2},
    })
    local = _write_yaml(tmp_path / "review.local.yaml", {
        "settings": {"max_turns": 8},
    })
    cli = _write_yaml(tmp_path / "experiment.yaml", {
        "prompt": "experimental prompt",
    })
    with prepared_recipe(base, {}, local_path=local, overlay_paths=[cli]) as effective:
        doc = _read(effective)
    assert doc["prompt"] == "experimental prompt"       # CLI layer wins
    assert doc["settings"]["max_turns"] == 8            # local overrode base
    assert doc["settings"]["temperature"] == 0.2        # base survives deep-merge


def test_prepared_recipe_resolves_context_block(tmp_path):
    data = tmp_path / "notes.md"
    data.write_text("the load-bearing notes")
    base = _write_yaml(tmp_path / "review.yaml", {
        "prompt": "read the notes",
        "context": [{"label": "NOTES", "source": f"file:{data}"}],
    })
    with prepared_recipe(base, {}) as effective:
        doc = _read(effective)
    assert "the load-bearing notes" in doc["prompt"]
    assert "context" not in doc  # consumed; goose never sees it


def test_prepared_recipe_cleans_up_even_when_body_raises(tmp_path):
    base = _write_yaml(tmp_path / "review.yaml", {"prompt": "boom"})
    with pytest.raises(RuntimeError, match="phase exploded"):
        with prepared_recipe(base, {}) as effective:
            raise RuntimeError("phase exploded")
    assert not Path(effective).exists()
