"""LooperConfig — value object for gooseloop.toml.

Loaded once at the top of a run via LooperConfig.load() and passed
explicitly. No module-level singleton, no clear-cache shims, no global
state. Tests construct LooperConfig directly with overrides.

`default_engine` (renamed from `engine_module`, 2026-07-13, ADR 0009) is
exactly what its name says: the engine a bare `gooseloop run` runs. It is
NOT a claim that a project has one engine — one gooseloop.toml routinely
serves several (each with its own [section]), and `gooseloop run <name>`
selects any of them by short name via resolve_engine_module().
"""

import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


CONFIG_FILENAME = "gooseloop.toml"


DEFAULTS: dict[str, Any] = {
    "gooseloop": {
        "default_model": "openrouter/owl-alpha",
        "sessions_dir": "reviews/sessions",
        "default_engine": "engines.hello_world",
        "environment_config": "",
        "max_queue_depth": 50,
        "review_recipe": "review.yaml",
        "summary_recipe": "summary.yaml",
        "retry": {
            "max_retries": 6,
            "base_delay": 5,
            "review_repair_attempts": 1,
        },
    },
}


@dataclass
class RetrySettings:
    max_retries: int = 6
    base_delay: int = 5
    # Extra review attempts when the output fails to parse or fails the schema,
    # each re-prompted with the exact rejection reason (the validate-and-repair
    # loop). 0 disables repair (one shot, then abort). Weak models routinely
    # need one repair to correct a bad sentinel or an invented schema.
    review_repair_attempts: int = 1


@dataclass
class LooperConfig:
    """Resolved gooseloop.toml. Construct via LooperConfig.load(); never global."""
    default_model: str = "openrouter/owl-alpha"
    sessions_dir: Path = field(default_factory=lambda: Path("reviews/sessions"))
    default_engine: str = "engines.hello_world"
    environment_config: Path | None = None
    max_queue_depth: int = 50
    review_recipe: str = "review.yaml"
    summary_recipe: str = "summary.yaml"
    retry: RetrySettings = field(default_factory=RetrySettings)
    anchor: Path = field(default_factory=Path.cwd)

    @property
    def engine_module(self) -> str:
        """Deprecated alias for default_engine, kept so code written against
        the 0.1.x attribute keeps working. Prefer default_engine."""
        return self.default_engine

    @classmethod
    def load(cls, anchor: Path | None = None, *, warn_on_missing: bool = True) -> "LooperConfig":
        """Load gooseloop.toml from `anchor` (default: cwd). Missing file = defaults."""
        anchor = (anchor or Path.cwd()).resolve()
        path = anchor / CONFIG_FILENAME
        if path.exists():
            with open(path, "rb") as f:
                raw = tomllib.load(f)
            section = raw.get("gooseloop", {})
            if "engine_module" in section:
                if "default_engine" not in section:
                    section["default_engine"] = section["engine_module"]
                print(
                    f"[gooseloop] {CONFIG_FILENAME}: `engine_module` is deprecated; "
                    f"rename it to `default_engine` (same value, clearer meaning — "
                    f"it is the engine a bare `gooseloop run` runs, not the only one).",
                    file=sys.stderr,
                )
            merged = _deep_merge(DEFAULTS, raw)
        else:
            if warn_on_missing:
                print(
                    f"[gooseloop] {CONFIG_FILENAME} not found in {anchor}; using defaults.",
                    file=sys.stderr,
                )
            merged = DEFAULTS
        return cls._from_merged(merged, anchor)

    @classmethod
    def _from_merged(cls, merged: dict[str, Any], anchor: Path) -> "LooperConfig":
        section = merged["gooseloop"]
        env_cfg = section.get("environment_config", "") or ""
        return cls(
            default_model=section["default_model"],
            sessions_dir=_resolve(section["sessions_dir"], anchor),
            default_engine=section["default_engine"],
            environment_config=_resolve(env_cfg, anchor) if env_cfg else None,
            max_queue_depth=int(section["max_queue_depth"]),
            review_recipe=section.get("review_recipe", "review.yaml"),
            summary_recipe=section.get("summary_recipe", "summary.yaml"),
            retry=RetrySettings(
                max_retries=int(section["retry"]["max_retries"]),
                base_delay=int(section["retry"]["base_delay"]),
                review_repair_attempts=int(section["retry"].get("review_repair_attempts", 1)),
            ),
            anchor=anchor,
        )


def resolve_engine_module(anchor: Path, name: str) -> str:
    """Resolve an engine name to a dotted module path (ADR 0009).

    A dotted name is already a module path and passes through untouched. A
    short name (`doc_drift`) is resolved by the same convention every real
    loop root follows: engines live as subpackages of a top-level package
    in the loop root (`engines/doc_drift/`). The scan checks every
    top-level package in `anchor` plus the bare name itself.

    Raises LookupError with an operator-actionable message when the name
    matches nothing or is ambiguous — never guesses between candidates.
    """
    if "." in name:
        return name

    candidates: list[str] = []
    # A top-level module (myengine.py) or package (myengine/) wins outright:
    # the name IS the module path, no scan needed.
    if (anchor / f"{name}.py").exists() or (anchor / name / "__init__.py").exists():
        return name
    for pkg_dir in sorted(anchor.iterdir()):
        if not pkg_dir.is_dir() or not (pkg_dir / "__init__.py").exists():
            continue
        if (pkg_dir / name / "__init__.py").exists():
            candidates.append(f"{pkg_dir.name}.{name}")

    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise LookupError(
            f"no engine named {name!r} found under {anchor} "
            f"(looked for {name}.py, {name}/, or <top-level-package>/{name}/); "
            f"pass a dotted module path to skip resolution"
        )
    raise LookupError(
        f"engine name {name!r} is ambiguous under {anchor}: "
        f"{', '.join(candidates)} — pass the dotted module path instead"
    )


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _resolve(path_str: str, anchor: Path) -> Path:
    p = Path(path_str)
    return p if p.is_absolute() else (anchor / p).resolve()
