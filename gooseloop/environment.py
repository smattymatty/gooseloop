"""Environment abstract base class.

Per ADR 0005 (superseded by ADR 0017) the framework ABC has exactly one
abstract method: env_vars(). Shape-specific contracts (paths, loaders, domain
vocabulary) are not part of the framework. A consuming project defines its own
base ABCs for its domain, and its concrete environments subclass those.

Engines pull paths and project-data via ctx.environment, calling whatever
methods the concrete instance exposes. Recipes paste content via the
env_method:<name> source kind in their context: block.
"""

from abc import ABC, abstractmethod


class Environment(ABC):
    """Minimum the framework requires from an Environment.

    A concrete environment must return the env vars the looper should merge
    into every recipe call. Everything else (paths, loaders, project data)
    is shape-specific and lives on the concrete class (or a base ABC the
    consuming project defines), not in the framework.
    """

    @abstractmethod
    def env_vars(self) -> dict[str, str]:
        """Env vars merged into every recipe call.

        Conventionally includes ${VAR} interpolations the engine's recipes
        reference (e.g. POTENTIAL_DIR, OUTREACH_DIR, CORE_DIR for a customer
        pipeline; HANDOFF_DIR, TARGET_REPO for a handoff engine).
        """
