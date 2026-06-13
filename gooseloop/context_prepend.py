"""Pre-render recipe `context:` blocks into the prompt before goose sees it.

Per PROTOCOL.md §7. Recipes declare load-bearing inputs under a top-level
`context:` block. Each entry is {label, source, optional?}. Before invoking
goose, the looper resolves each source to literal text and prepends a
fenced block to the recipe's `prompt`. The model cannot skip the step
because the text is in the prompt, not behind a bash call.

Sources:
    env_file:VAR     read the file whose path is in env var VAR
    file:PATH        read PATH directly (env-substituted)
    glob:PATTERN     glob (env-substituted), concat sorted matches
    env_method:NAME  call environment.NAME() and paste its return value

Failure is loud by default. `optional: true` softens to a sentinel
placeholder when a source is unresolvable.

If GOOSER_KEEP_RENDERED=1 the rendered recipe is left on disk for
inspection. Default is delete-after-run (cleanup is the caller's
responsibility).
"""

import glob as _glob
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Optional

import yaml


_BLOCK_OPEN = "<<<CONTEXT: {label}>>>"
_BLOCK_CLOSE = "<<<END CONTEXT>>>"

_PREAMBLE = (
    "# Pre-loaded input\n"
    "#\n"
    "# The looper has already resolved the input files this recipe\n"
    "# requires and pasted their contents below. You do NOT need to\n"
    "# `cat` any of these paths yourself - the literal contents are\n"
    "# already in your context. Read the block, then follow the\n"
    "# instructions that come after it.\n"
)


_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")

# A MiniJinja raw-block control token in any accepted spelling: {% raw %} /
# {% endraw %}, with optional `-` trim markers and arbitrary inner whitespace
# ({%- endraw -%}, {%raw%}, {%  endraw  %}). These are the ONE thing a raw
# block can't safely contain, so _raw_wrap defuses them.
_RAW_CTRL_RE = re.compile(r"\{%-?\s*(?:end)?raw\s*-?%\}")

# Zero-width space. Inserted between the `{` and `%` of a raw-control token so
# MiniJinja no longer sees a `{%` tag opener. The character is invisible, so
# the model reading the pasted text sees essentially the original; only the
# ephemeral rendered prompt is touched (the on-disk recap is never modified).
_ZWSP = "​"


def _raw_wrap(text: str) -> str:
    """Wrap arbitrary text so goose's MiniJinja templater treats it as literal.

    Goose renders each recipe `prompt` through MiniJinja (to resolve its own
    `{{ param }}` placeholders) AFTER the looper has pasted context into the
    prompt. Any `{{`, `{%`, or `{#` sitting in the pasted data is then parsed
    as a Jinja tag: a recap that mentioned a Svelte `{#if}` opened a comment
    that never closed and goose died with "unexpected end of comment". Wrapping
    the data in {% raw %}…{% endraw %} neutralises every delimiter at once.

    The one sequence a raw block cannot itself contain is a raw-control token
    (`{% raw %}` / `{% endraw %}`) — a recap *of this very function* carries
    them in its prose. Rather than try to re-emit those tokens (which needs a
    `{{ "…" }}` expression whose safety depends on the exact MiniJinja version
    goose ships — and goose's build rejects it), we break the `{%` opener with
    a zero-width space so goose never recognises a tag. The result is exactly
    one clean raw block per body: no nesting, no stray `{% endraw %}`, valid in
    any MiniJinja that supports raw at all. Empty text is returned unwrapped."""
    if not text:
        return text
    defused = _RAW_CTRL_RE.sub(lambda m: "{" + _ZWSP + m.group(0)[1:], text)
    return "{% raw %}" + defused + "{% endraw %}"


def _prompt_needs_substitution(prompt: str) -> bool:
    return isinstance(prompt, str) and bool(_ENV_VAR_RE.search(prompt))


def _substitute_env(template: str, env: dict[str, str]) -> str:
    def repl(m: re.Match) -> str:
        name = m.group(1) or m.group(2)
        return env.get(name, "")
    return _ENV_VAR_RE.sub(repl, template)


def _resolve_env_file(arg: str, env: dict[str, str], *, optional: bool) -> str:
    var_name = arg.strip()
    path_str = env.get(var_name)
    if not path_str:
        if optional:
            return f"(env var {var_name} is unset; skipped)"
        raise RuntimeError(
            f"context source 'env_file:{var_name}' failed: "
            f"env var {var_name} is unset or empty"
        )
    path = Path(path_str)
    if not path.exists():
        if optional:
            return f"(file not present: {path})"
        raise RuntimeError(
            f"context source 'env_file:{var_name}' failed: "
            f"file does not exist: {path}"
        )
    return path.read_text()


def _resolve_file(arg: str, env: dict[str, str], *, optional: bool) -> str:
    path_str = _substitute_env(arg.strip(), env)
    if not path_str:
        if optional:
            return f"(file path resolved empty for '{arg}'; skipped)"
        raise RuntimeError(
            f"context source 'file:{arg}' resolved to an empty path"
        )
    path = Path(path_str)
    if not path.exists():
        if optional:
            return f"(file not present: {path})"
        raise RuntimeError(
            f"context source 'file:{arg}' failed: file does not exist: {path}"
        )
    return path.read_text()


def _resolve_glob(arg: str, env: dict[str, str], *, optional: bool) -> str:
    pattern = _substitute_env(arg.strip(), env)
    if not pattern:
        if optional:
            return f"(glob pattern resolved empty for '{arg}'; skipped)"
        raise RuntimeError(
            f"context source 'glob:{arg}' resolved to an empty pattern"
        )
    matches = sorted(_glob.glob(pattern))
    if not matches:
        if optional:
            return f"(no files matched pattern: {pattern})"
        raise RuntimeError(
            f"context source 'glob:{arg}' matched no files (pattern: {pattern})"
        )
    chunks = []
    for path_str in matches:
        path = Path(path_str)
        chunks.append(f"=== {path.name} ===\n{path.read_text()}")
    return "\n\n".join(chunks)


def _resolve_env_method(arg: str, environment: Any) -> str:
    method_name = arg.strip()
    if environment is None:
        raise RuntimeError(
            f"context source 'env_method:{method_name}' requires an "
            f"Environment instance; the Looper was constructed without one"
        )
    method = getattr(environment, method_name, None)
    if method is None or not callable(method):
        raise RuntimeError(
            f"context source 'env_method:{method_name}' failed: "
            f"{type(environment).__name__} has no callable named {method_name!r}"
        )
    result = method()
    if not isinstance(result, str):
        raise RuntimeError(
            f"context source 'env_method:{method_name}' must return str, "
            f"got {type(result).__name__}"
        )
    return result


def substitute_env_in_prompt(doc: dict, env: dict[str, str]) -> dict:
    """Return a copy of `doc` with `${VAR}` / `$VAR` substituted in the prompt.

    Goose itself does NOT shell-expand env vars inside recipe prompt prose
    (its own templating uses `{{ name }}` parameters declared at the top
    of the recipe). The looper substitutes here so recipes can reference
    ${POTENTIAL_DIR}, ${NAME}, etc. uniformly with the env vars the
    Environment / engine inject. Substitution is non-destructive: an
    unknown variable becomes an empty string and the next step (file read,
    glob, etc.) raises a clear error there.
    """
    prompt = doc.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        return doc
    return {**doc, "prompt": _substitute_env(prompt, env)}


def _resolve_source(source: str, env: dict[str, str], environment: Any, *,
                    optional: bool) -> str:
    if ":" not in source:
        raise RuntimeError(
            f"context source {source!r} missing 'kind:' prefix "
            f"(expected one of env_file, file, glob, env_method)"
        )
    kind, _, arg = source.partition(":")
    kind = kind.strip()
    if kind == "env_file":
        return _resolve_env_file(arg, env, optional=optional)
    if kind == "file":
        return _resolve_file(arg, env, optional=optional)
    if kind == "glob":
        return _resolve_glob(arg, env, optional=optional)
    if kind == "env_method":
        return _resolve_env_method(arg, environment)
    raise RuntimeError(
        f"context source kind {kind!r} is not supported "
        f"(expected env_file, file, glob, env_method)"
    )


def render_recipe_with_context(
    recipe: dict | str | Path,
    extra_env: dict[str, str],
    *,
    environment: Any = None,
) -> Optional[str]:
    """Resolve a recipe's context: block; write a rendered temp file.

    Returns the path to the temp file (str) if a context: block was
    present and rendered, or None if the recipe has no context: block
    (caller can use the original recipe path unchanged).

    `recipe` may be a parsed dict (e.g. the merged result of recipe_merge),
    a path to a yaml file, or a Path object.
    """
    if isinstance(recipe, (str, Path)):
        path = Path(recipe)
        with open(path, "r") as f:
            doc = yaml.safe_load(f) or {}
    else:
        doc = recipe

    env = {**os.environ, **(extra_env or {})}
    context_block = doc.get("context")

    if context_block:
        rendered_chunks: list[str] = [_PREAMBLE]
        for entry in context_block:
            label = entry.get("label", "(unnamed)")
            source = entry.get("source", "")
            optional = bool(entry.get("optional", False))
            body = _resolve_source(source, env, environment, optional=optional)
            rendered_chunks.append(
                _BLOCK_OPEN.format(label=label) + "\n"
                + _raw_wrap(body.rstrip()) + "\n"
                + _BLOCK_CLOSE
            )
        block_text = "\n\n".join(rendered_chunks)
        # Env-substitute the recipe's OWN prompt prose, but never the pasted
        # context: that text is data, and `${VAR}`/`$VAR` sequences in it must
        # survive verbatim (a recap can legitimately mention $HOME or
        # ${WINDOW_DAYS}). The context is also already raw-wrapped, so goose's
        # own templater leaves it alone.
        substituted_prompt = _substitute_env(doc.get("prompt", ""), env)
        doc = {**doc, "prompt": block_text + "\n\n" + substituted_prompt}
        doc.pop("context", None)  # consumed; goose doesn't need to see it
    else:
        if not _prompt_needs_substitution(doc.get("prompt", "")):
            # Nothing to render, nothing to substitute — caller can use the
            # original recipe file unchanged.
            return None
        doc = substitute_env_in_prompt(doc, env)

    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".rendered.yaml",
        delete=False,
    )
    yaml.safe_dump(doc, tmp, sort_keys=False, default_flow_style=False)
    tmp.close()
    return tmp.name
