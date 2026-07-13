"""`gooseloop` CLI entry point.

Subcommands:
    run [ENGINE]                 Run an engine, one pass. ENGINE is a short
                                 name (`doc_drift`) or dotted module path;
                                 omitted = gooseloop.toml's default_engine.
        --review-only            Stop after the review phase.
        --review-overlay PATH    Stack a review-recipe overlay (repeatable).
        --summary-overlay PATH   Stack a summary-recipe overlay (repeatable).
        --no-save                Don't write a session folder.
        --no-validate            Skip engine.precheck().
        --model NAME             Override the configured model.

    recipe --resolve NAME        Print the fully-merged recipe NAME.
    recipe --sources NAME        Dry-run the recipe's context sources and
                                 list the env_methods / env vars in scope.
        --json                   Machine-readable output (for dashboards).
        -e MODULE                Engine whose Environment supplies the scope.

    engines                      List every engine in the loop root.

Engine loading: a name resolves to a dotted module path per ADR 0009
(short names scan the loop root's top-level packages), the module is
imported, and its module-level `engine` attribute is instantiated. Engines
expose their class as a module-level callable in their __init__.py.
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from .config import LooperConfig, resolve_engine_module
from .engine import Engine
from .environment import Environment
from .introspect import list_env_methods, preview_recipe_context
from .looper import GooseLooper
from .runlock import EXIT_LOCKED, RunLockHeldError
from .recipe_merge import load_layered_recipe, resolved_recipe_yaml
from .text import Color, colored


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gooseloop")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="run an engine, one pass")
    run.add_argument("engine_name", nargs="?", default=None, metavar="ENGINE",
                     help="engine to run: a short name (doc_drift) or dotted "
                          "module path (default: gooseloop.toml's default_engine)")
    run.add_argument("-e", "--engine", default=None, metavar="MODULE",
                     help="same as the positional ENGINE (kept for compatibility)")
    run.add_argument("--review-only", action="store_true",
                     help="stop after the review phase")
    run.add_argument("--review-overlay", action="append", default=[],
                     metavar="PATH", help="stack a review-recipe overlay")
    run.add_argument("--summary-overlay", action="append", default=[],
                     metavar="PATH", help="stack a summary-recipe overlay")
    run.add_argument("--no-save", action="store_true",
                     help="do not write a session folder")
    run.add_argument("--no-validate", action="store_true",
                     help="skip engine.precheck()")
    run.add_argument("--model", default=None, help="override the configured model")

    rec = sub.add_parser("recipe", help="recipe utilities")
    rec.add_argument("--resolve", metavar="NAME",
                     help="print the fully-merged recipe NAME")
    rec.add_argument("--sources", metavar="NAME",
                     help="dry-run NAME's context sources; list env_methods "
                          "and env vars in scope")
    rec.add_argument("--json", action="store_true",
                     help="machine-readable --sources output")
    rec.add_argument("-e", "--engine", default=None, metavar="MODULE",
                     help="engine whose Environment supplies the --sources "
                          "scope (default: gooseloop.toml's default_engine)")
    rec.add_argument("--overlay", action="append", default=[], metavar="PATH",
                     help="extra overlay path to include in the merge")

    sub.add_parser("engines", help="list every engine in the loop root")

    args = parser.parse_args(argv)

    if args.cmd == "run":
        return _cmd_run(args)
    if args.cmd == "recipe":
        return _cmd_recipe(args)
    if args.cmd == "engines":
        return _cmd_engines(args)
    parser.error(f"unknown command: {args.cmd}")
    return 2  # unreachable


def _cmd_run(args: argparse.Namespace) -> int:
    config = LooperConfig.load()
    requested = args.engine_name or args.engine
    engine, environment = _load_engine_and_environment(config, engine_override=requested)
    looper = GooseLooper(
        engine=engine,
        environment=environment,
        config=config,
        model=args.model,
        save=not args.no_save,
        validate=not args.no_validate,
        review_only=args.review_only,
        review_overlays=[Path(p) for p in args.review_overlay],
        summary_overlays=[Path(p) for p in args.summary_overlay],
    )
    try:
        result = looper.begin_loop()
    except RunLockHeldError as e:
        # Exit 3 = "busy", distinct from 1 (run error) and 2 (usage), so
        # a supervisor can tell a held lock from a failed run.
        print(f"gooseloop: {e}", file=sys.stderr)
        return EXIT_LOCKED
    return 0 if result.get("review_status") != "error" else 1


def _cmd_recipe(args: argparse.Namespace) -> int:
    if args.sources:
        return _cmd_recipe_sources(args)
    if not args.resolve:
        print("usage: gooseloop recipe --resolve NAME | --sources NAME "
              "[--json] [--overlay PATH ...]",
              file=sys.stderr)
        return 2
    config = LooperConfig.load()
    loaded = _load_merged_recipe(config, args.resolve, args.overlay)
    if loaded is None:
        return 1
    _, merged = loaded
    print(resolved_recipe_yaml(merged))
    return 0


def _load_merged_recipe(
    config: LooperConfig, name: str, overlays: list[str],
) -> tuple[Path, dict[str, Any]] | None:
    """Resolve NAME to a recipe file and merge base + local + overlays.

    Accepts a bare name ("review") or a path with suffix. Prints the
    not-found error itself; None means the caller exits 1.
    """
    base_path = (config.anchor / name).resolve()
    if not base_path.exists():
        candidate = base_path.with_suffix(".yaml")
        if candidate.exists():
            base_path = candidate
        else:
            print(f"recipe not found: {base_path}", file=sys.stderr)
            return None
    local = base_path.with_name(base_path.stem + ".local" + base_path.suffix)
    merged = load_layered_recipe(
        base_path,
        local_path=local if local.exists() else None,
        overlay_paths=[Path(p) for p in overlays],
    )
    return base_path, merged


def _cmd_recipe_sources(args: argparse.Namespace) -> int:
    """Dry-run a recipe's context sources; list the env scope (PROTOCOL §7).

    Exit code 0 when every required source resolves (optional failures
    are reported but tolerated, matching render-time strictness); 1 when
    a required source would fail the render.
    """
    config = LooperConfig.load()
    engine, environment = _load_engine_and_environment(
        config, engine_override=args.engine,
    )
    loaded = _load_merged_recipe(config, args.sources, args.overlay)
    if loaded is None:
        return 1
    base_path, merged = loaded

    scope = {
        **(environment.env_vars() if environment else {}),
        **engine.base_env(),
    }
    env = {**os.environ, **scope}
    previews = preview_recipe_context(merged, env, environment=environment)
    methods = list_env_methods(environment)
    failures = [p for p in previews if not p.preview.ok and not p.optional]

    if args.json:
        payload = {
            "recipe": str(base_path),
            "context": [
                {"label": p.label, "optional": p.optional,
                 **dataclasses.asdict(p.preview)}
                for p in previews
            ],
            "env_methods": [dataclasses.asdict(m) for m in methods],
            "env_vars": sorted(scope),
            "ok": not failures,
        }
        print(json.dumps(payload, indent=2))
        return 0 if not failures else 1

    print(f"{base_path.name} · {len(previews)} context source(s)")
    for p in previews:
        mark = (colored("ok  ", Color.GREEN) if p.preview.ok
                else colored("FAIL", Color.RED))
        opt = "  [optional]" if p.optional else ""
        print(f"  {mark} {p.label:<20} {p.preview.source}{opt}")
        print(f"       -> {p.preview.resolved or '(unresolved)'}: {p.preview.detail}")
        for m in p.preview.matches[:8]:
            print(f"          {m.path}  ({m.size} bytes)")
        if len(p.preview.matches) > 8:
            print(f"          ... and {len(p.preview.matches) - 8} more")
    if methods:
        print("env_method sources available:")
        for method in methods:
            doc = f" — {method.doc}" if method.doc else ""
            print(f"  {method.name}{doc}")
    if scope:
        print(f"env vars in scope: {', '.join(sorted(scope))}")
    return 0 if not failures else 1


def _cmd_engines(args: argparse.Namespace) -> int:
    """List every engine in the loop root, not just the default — one
    gooseloop.toml routinely serves several engines (ADR 0009)."""
    config = LooperConfig.load()
    try:
        default_module = resolve_engine_module(config.anchor, config.default_engine)
    except LookupError as e:
        print(f"gooseloop: {e}", file=sys.stderr)
        return 1

    sys.path.insert(0, str(config.anchor))
    try:
        found = 0
        for module_name in _candidate_engine_modules(config.anchor, default_module):
            try:
                mod = importlib.import_module(module_name)
            except ImportError:
                continue
            engine_obj = getattr(mod, "engine", None)
            if engine_obj is None:
                continue
            found += 1
            short = module_name.rsplit(".", 1)[-1]
            marker = "  (default)" if module_name == default_module else ""
            print(f"{short:20s} {module_name}{marker}")
        if found == 0:
            print(f"no engines found under {config.anchor}", file=sys.stderr)
            return 1
    finally:
        sys.path.remove(str(config.anchor))
    return 0


def _candidate_engine_modules(anchor: Path, default_module: str) -> list[str]:
    """Sibling packages of the default engine's parent package — the same
    convention resolve_engine_module scans, from the other direction."""
    if "." not in default_module:
        return [default_module]
    parent_pkg = default_module.rsplit(".", 1)[0]
    parent_dir = anchor / Path(*parent_pkg.split("."))
    if not parent_dir.is_dir():
        return [default_module]
    out = []
    for child in sorted(parent_dir.iterdir()):
        if child.is_dir() and (child / "__init__.py").exists():
            out.append(f"{parent_pkg}.{child.name}")
    return out or [default_module]


def _load_engine_and_environment(
    config: LooperConfig,
    *,
    engine_override: str | None = None,
) -> tuple[Engine, Environment | None]:
    """Resolve an engine name, import it, instantiate its `engine` attribute.

    Convention: an engine package exposes a module-level `engine`
    callable (often the class itself) and optionally an `environment`
    callable. Both are instantiated with no arguments by default.
    Engines requiring constructor arguments should ship factory
    callables in those slots.

    `engine_override` (the positional ENGINE or `-e`) supersedes
    `config.default_engine` when provided. Either may be a short name
    (resolved per ADR 0009) or a dotted module path.
    """
    requested = engine_override or config.default_engine
    try:
        module_name = resolve_engine_module(config.anchor, requested)
    except LookupError as e:
        raise SystemExit(f"gooseloop: {e}")
    sys.path.insert(0, str(config.anchor))
    try:
        mod = importlib.import_module(module_name)
    except ImportError as e:
        raise SystemExit(
            f"gooseloop: cannot import engine module {module_name!r}: {e}"
        )
    engine_obj = getattr(mod, "engine", None)
    if engine_obj is None:
        raise SystemExit(
            f"gooseloop: engine module {module_name!r} has no `engine` attribute"
        )
    engine = engine_obj() if callable(engine_obj) and not isinstance(engine_obj, Engine) else engine_obj
    if not isinstance(engine, Engine):
        raise SystemExit(
            f"gooseloop: {module_name}.engine did not yield an Engine instance"
        )

    env_obj = getattr(mod, "environment", None)
    environment: Environment | None = None
    if env_obj is not None:
        environment = env_obj() if callable(env_obj) and not isinstance(env_obj, Environment) else env_obj
        if not isinstance(environment, Environment):
            raise SystemExit(
                f"gooseloop: {module_name}.environment did not yield an Environment instance"
            )
    return engine, environment


if __name__ == "__main__":
    sys.exit(main())
