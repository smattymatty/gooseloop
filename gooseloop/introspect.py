"""Introspection and dry-run preview for recipe context sources.

The read-only half of PROTOCOL §7, built for tooling (the CLI's
`recipe --sources`, dashboards): enumerate what an Environment offers
as `env_method:` sources, and report whether each context source WOULD
resolve — without reading file bodies or calling environment methods.
Render-time semantics stay in context_prepend; this module never
mutates anything and never pastes content.

The preview is deliberately cheaper than the render: `glob:` and
`file:` sources are stat'd (paths and sizes, no reads), and
`env_method:` sources are checked for existence only — calling one is
real work (a journal digest, a URL fetch) and belongs to render time
or an explicit "preview content" action in the calling tool.
"""

from __future__ import annotations

import glob as _glob
import inspect
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .context_prepend import SOURCE_KINDS, split_source, substitute_env


@dataclass(frozen=True)
class EnvMethodInfo:
    """One environment method usable as an `env_method:` source."""
    name: str
    doc: str  # first docstring line; "" when undocumented


@dataclass(frozen=True)
class MatchedFile:
    path: str
    size: int


@dataclass(frozen=True)
class SourcePreview:
    """Dry-run outcome for one context source."""
    source: str    # as written in the recipe
    kind: str      # one of SOURCE_KINDS, or "" for a malformed source
    resolved: str  # env-substituted arg (path, pattern, var, method name)
    ok: bool
    detail: str    # human-readable: what resolved or why it didn't
    matches: tuple[MatchedFile, ...] = ()


@dataclass(frozen=True)
class ContextPreview:
    """One recipe context entry plus its source's dry-run outcome."""
    label: str
    optional: bool
    preview: SourcePreview


def list_env_methods(environment: Any) -> list[EnvMethodInfo]:
    """Enumerate methods usable as `env_method:` sources, per PROTOCOL §7.

    Qualification mirrors what render time accepts: public (no leading
    underscore), callable, invocable with zero arguments. `env_vars` is
    excluded — it is the Environment ABC's own contract, not a context
    source. Methods whose return annotation names a non-str type are
    excluded (render would refuse their value); unannotated methods are
    included since their return type cannot be known without calling.
    """
    if environment is None:
        return []
    out: list[EnvMethodInfo] = []
    for name in dir(environment):
        if name.startswith("_") or name == "env_vars":
            continue
        member = getattr(environment, name, None)
        if not callable(member):
            continue
        try:
            sig = inspect.signature(member)
        except (TypeError, ValueError):
            continue
        required = [
            p for p in sig.parameters.values()
            if p.default is inspect.Parameter.empty
            and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
        ]
        if required:
            continue
        if sig.return_annotation not in (inspect.Signature.empty, str, "str"):
            continue
        doc_lines = (inspect.getdoc(member) or "").splitlines()
        out.append(EnvMethodInfo(name=name, doc=doc_lines[0] if doc_lines else ""))
    return sorted(out, key=lambda m: m.name)


def preview_source(source: str, env: dict[str, str], *,
                   environment: Any = None,
                   injected_env: dict[str, str] | None = None) -> SourcePreview:
    """Dry-run one context source. Never reads bodies, never calls methods.

    `injected_env` is the engine's declaration of vars it injects at
    phase-build time (Engine.injected_env) — absent from the static scope
    by nature, so an env_file naming one is OK-by-declaration, not a
    failure."""
    kind, arg = split_source(source)
    if kind not in SOURCE_KINDS:
        detail = (
            f"missing 'kind:' prefix (expected one of {', '.join(SOURCE_KINDS)})"
            if not kind
            else f"unknown source kind {kind!r} (expected one of {', '.join(SOURCE_KINDS)})"
        )
        return SourcePreview(source, kind, arg.strip(), False, detail)

    if kind == "env_file":
        var_name = arg.strip()
        path_str = env.get(var_name)
        if not path_str:
            if injected_env and var_name in injected_env:
                desc = injected_env[var_name] or "engine-assembled at run time"
                return SourcePreview(
                    source, kind, var_name, True,
                    f"injected per phase by the engine — {desc}")
            return SourcePreview(source, kind, var_name, False,
                                 f"env var {var_name} is unset or empty")
        return _preview_path(source, kind, path_str)

    if kind == "file":
        path_str = substitute_env(arg.strip(), env)
        if not path_str:
            return SourcePreview(source, kind, "", False,
                                 f"path resolved empty for {arg.strip()!r}")
        return _preview_path(source, kind, path_str)

    if kind == "glob":
        pattern = substitute_env(arg.strip(), env)
        if not pattern:
            return SourcePreview(source, kind, "", False,
                                 f"pattern resolved empty for {arg.strip()!r}")
        matched = tuple(
            MatchedFile(path=p, size=_size_of(p))
            for p in sorted(_glob.glob(pattern))
        )
        if not matched:
            return SourcePreview(source, kind, pattern, False,
                                 "no files matched pattern", matched)
        total = sum(m.size for m in matched)
        return SourcePreview(source, kind, pattern, True,
                             f"{len(matched)} file(s), {total} bytes", matched)

    # env_method
    method_name = arg.strip()
    if environment is None:
        return SourcePreview(source, kind, method_name, False,
                             "no Environment instance to call it on")
    method = getattr(environment, method_name, None)
    if method is None or not callable(method):
        return SourcePreview(
            source, kind, method_name, False,
            f"{type(environment).__name__} has no callable named {method_name!r}",
        )
    return SourcePreview(source, kind, method_name, True,
                         f"{type(environment).__name__}.{method_name}() (not called in preview)")


def preview_recipe_context(recipe: dict[str, Any], env: dict[str, str], *,
                           environment: Any = None,
                           injected_env: dict[str, str] | None = None) -> list[ContextPreview]:
    """Dry-run every entry of a recipe's context: block, in declared order."""
    out: list[ContextPreview] = []
    for entry in recipe.get("context") or []:
        if not isinstance(entry, dict):
            continue
        out.append(ContextPreview(
            label=str(entry.get("label", "(unnamed)")),
            optional=bool(entry.get("optional", False)),
            preview=preview_source(
                str(entry.get("source", "")), env, environment=environment,
                injected_env=injected_env,
            ),
        ))
    return out


def _preview_path(source: str, kind: str, path_str: str) -> SourcePreview:
    path = Path(path_str)
    if not path.exists():
        return SourcePreview(source, kind, path_str, False,
                             "file does not exist")
    size = _size_of(path_str)
    return SourcePreview(source, kind, path_str, True, f"{size} bytes",
                         (MatchedFile(path=path_str, size=size),))


def _size_of(path_str: str) -> int:
    try:
        return os.stat(path_str).st_size
    except OSError:
        return 0
