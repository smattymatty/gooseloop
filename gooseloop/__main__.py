"""`gooseloop` CLI entry point.

Subcommands:
    run                          Run the configured engine, one pass.
        --review-only            Stop after the review phase.
        --review-overlay PATH    Stack a review-recipe overlay (repeatable).
        --summary-overlay PATH   Stack a summary-recipe overlay (repeatable).
        --no-save                Don't write a session folder.
        --no-validate            Skip engine.precheck().
        --model NAME             Override the configured model.

    recipe --resolve NAME        Print the fully-merged recipe NAME.

    engines                      Print the configured engine module.

Engine discovery: gooseloop.toml's [gooseloop] engine_module = "..." is
imported and its module-level `engine` attribute is instantiated. Engines
expose their class as a module-level callable in their __init__.py.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

from .config import LooperConfig
from .engine import Engine
from .environment import Environment
from .looper import GooseLooper
from .recipe_merge import load_layered_recipe, resolved_recipe_yaml


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gooseloop")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="run the configured engine, one pass")
    run.add_argument("-e", "--engine", default=None, metavar="MODULE",
                     help="override the engine module (default: from gooseloop.toml)")
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
    rec.add_argument("--overlay", action="append", default=[], metavar="PATH",
                     help="extra overlay path to include in the merge")

    sub.add_parser("engines", help="show the configured engine module")

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
    engine, environment = _load_engine_and_environment(config, engine_override=args.engine)
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
    result = looper.begin_loop()
    return 0 if result.get("review_status") != "error" else 1


def _cmd_recipe(args: argparse.Namespace) -> int:
    if not args.resolve:
        print("usage: gooseloop recipe --resolve NAME [--overlay PATH ...]",
              file=sys.stderr)
        return 2
    config = LooperConfig.load()
    base_path = (config.anchor / args.resolve).resolve()
    if not base_path.exists():
        # Try with .yaml suffix if user passed a bare name.
        candidate = base_path.with_suffix(".yaml")
        if candidate.exists():
            base_path = candidate
        else:
            print(f"recipe not found: {base_path}", file=sys.stderr)
            return 1
    local = base_path.with_name(base_path.stem + ".local" + base_path.suffix)
    overlay_paths = [Path(p) for p in args.overlay]
    merged = load_layered_recipe(
        base_path,
        local_path=local if local.exists() else None,
        overlay_paths=overlay_paths,
    )
    print(resolved_recipe_yaml(merged))
    return 0


def _cmd_engines(args: argparse.Namespace) -> int:
    config = LooperConfig.load()
    print(f"engine_module: {config.engine_module}")
    try:
        mod = importlib.import_module(config.engine_module)
        engine_cls = getattr(mod, "engine", None)
        if engine_cls is None:
            print("  (module has no `engine` attribute)", file=sys.stderr)
            return 1
        print(f"engine class:  {engine_cls.__module__}.{engine_cls.__name__}")
    except ImportError as e:
        print(f"  import failed: {e}", file=sys.stderr)
        return 1
    return 0


def _load_engine_and_environment(
    config: LooperConfig,
    *,
    engine_override: str | None = None,
) -> tuple[Engine, Environment | None]:
    """Import the engine module; instantiate its `engine` attribute.

    Convention: an engine package exposes a module-level `engine`
    callable (often the class itself) and optionally an `environment`
    callable. Both are instantiated with no arguments by default.
    Engines requiring constructor arguments should ship factory
    callables in those slots.

    `engine_override` (from `-e`/`--engine`) supersedes
    `config.engine_module` when provided.
    """
    module_name = engine_override or config.engine_module
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
