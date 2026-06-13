"""Engine abstract base class.

Per ADRs 0001 (engine returns Pipeline), 0006 (named-slot Pipeline), 0007
(BranchPolicy registry). The framework runs the Pipeline; the engine owns
what's inside it.

There is no decorator-based engine registry in v1.0. The CLI imports the
engine module declared in gooseloop.toml and reads the engine class from
that module's `engine` attribute (set by the engine's __init__.py). This
keeps gooseloop.toml as the single source of truth for "which engine"
without runtime registration side effects.
"""

from abc import ABC, abstractmethod

from .branch_policy import BranchPolicy
from .phase import Context, Pipeline


class Engine(ABC):
    """Implement this to plug into GooseLooper.

    Required:
        name        — short slug for logs, footers, config.
        pipeline()  — returns the Pipeline (review + body + summary).

    Optional:
        branch_policies — dict of recipe-name → BranchPolicy for routing[].
            May be overridden at the class level (static policies) or as
            an instance attribute / @property (policies that close over
            engine state like an output directory).
        base_env()      — env vars injected into every recipe call.
        precheck()      — run before the pipeline; raise to abort.
        recipes_dir()   — where engine-bundled recipes live.
        default_model() — engine-recommended model.
    """

    branch_policies: dict[str, BranchPolicy] = {}

    @property
    @abstractmethod
    def name(self) -> str:
        """Short slug. Conventionally lowercase, hyphenated."""

    @abstractmethod
    def pipeline(self, ctx: Context) -> Pipeline:
        """The Pipeline for one begin_loop() pass.

        Free to do pre-pipeline work (snapshot state, build a digest) and
        bake the results into the first Phase's build_env or ctx.artifacts.
        Must return a Pipeline with review and summary phases set; body
        may be empty.
        """

    def base_env(self) -> dict[str, str]:
        """Engine-only env additions. Environment.env_vars() covers paths."""
        return {}

    def precheck(self, ctx: Context) -> None:
        """Run once before the pipeline. Raise to abort the pass."""

    def recipes_dir(self) -> str:
        """Engine-bundled recipes location, relative to the engine module."""
        return "recipes"

    def default_model(self) -> str | None:
        """Engine-suggested default model. None = use whatever the Looper has."""
        return None
