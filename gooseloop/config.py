"""LooperConfig — value object for gooseloop.toml.

Loaded once at the top of a run via LooperConfig.load() and passed
explicitly. No module-level singleton, no clear-cache shims, no global
state. Tests construct LooperConfig directly with overrides.
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
        "engine_module": "engines.hello_world",
        "environment_config": "",
        "max_queue_depth": 50,
        "review_recipe": "review.yaml",
        "summary_recipe": "summary.yaml",
        "retry": {
            "max_retries": 6,
            "base_delay": 5,
        },
    },
}


@dataclass
class RetrySettings:
    max_retries: int = 6
    base_delay: int = 5


@dataclass
class LooperConfig:
    """Resolved gooseloop.toml. Construct via LooperConfig.load(); never global."""
    default_model: str = "openrouter/owl-alpha"
    sessions_dir: Path = field(default_factory=lambda: Path("reviews/sessions"))
    engine_module: str = "engines.hello_world"
    environment_config: Path | None = None
    max_queue_depth: int = 50
    review_recipe: str = "review.yaml"
    summary_recipe: str = "summary.yaml"
    retry: RetrySettings = field(default_factory=RetrySettings)
    anchor: Path = field(default_factory=Path.cwd)

    @classmethod
    def load(cls, anchor: Path | None = None, *, warn_on_missing: bool = True) -> "LooperConfig":
        """Load gooseloop.toml from `anchor` (default: cwd). Missing file = defaults."""
        anchor = (anchor or Path.cwd()).resolve()
        path = anchor / CONFIG_FILENAME
        if path.exists():
            with open(path, "rb") as f:
                raw = tomllib.load(f)
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
            engine_module=section["engine_module"],
            environment_config=_resolve(env_cfg, anchor) if env_cfg else None,
            max_queue_depth=int(section["max_queue_depth"]),
            review_recipe=section.get("review_recipe", "review.yaml"),
            summary_recipe=section.get("summary_recipe", "summary.yaml"),
            retry=RetrySettings(
                max_retries=int(section["retry"]["max_retries"]),
                base_delay=int(section["retry"]["base_delay"]),
            ),
            anchor=anchor,
        )


def _deep_merge(base: dict, override: dict) -> dict:
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
