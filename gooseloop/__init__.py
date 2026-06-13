"""gooseloop — an execution shell for goose-recipe pipelines.

Public surface:

    GooseLooper        - the execution shell
    Engine             - abstract base for engines
    Environment        - abstract base for environments (just env_vars)
    Phase              - one recipe invocation in a Pipeline
    Pipeline           - named-slot dataclass: review + body + summary
    Context            - passed to phase callables; typed ledger methods
    BranchPolicy       - per-recipe rules for routing[] -> Phase building
    LooperConfig       - resolved gooseloop.toml as a value object
    predicates         - success_predicate factories
    protocol           - ReviewOutput / OperatorAction / RoutingEntry types
"""

from . import predicates, protocol
from .branch_policy import BranchPolicy
from .config import LooperConfig
from .engine import Engine
from .environment import Environment
from .looper import GooseLooper
from .phase import Context, Phase, Pipeline

__all__ = [
    "BranchPolicy",
    "Context",
    "Engine",
    "Environment",
    "GooseLooper",
    "LooperConfig",
    "Phase",
    "Pipeline",
    "predicates",
    "protocol",
]

__version__ = "0.1.0"
