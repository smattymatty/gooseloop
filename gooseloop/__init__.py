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
    RunLockHeldError   - raised when the loop root's run.lock is held
    RUN_LOCK_FILENAME  - "run.lock", the per-root lock file (PROTOCOL 13)
    BoundaryUnavailableError - .gooseignore present, bubblewrap missing
    GOOSEIGNORE_FILENAME - ".gooseignore", the boundary file (PROTOCOL 15)
    boundary           - THE BOUNDARY: bwrap masking around goose spawns
    guardrails         - egress tripwire: scan_and_redact secret-shaped output
    introspect         - env_method listing + context-source dry-run preview
    telemetry          - phases.jsonl wide-event reader (PROTOCOL 14)
    predicates         - success_predicate factories
    protocol           - ReviewOutput / OperatorAction / RoutingEntry types
    toolkit            - stdlib-only engine helpers (Source, fetch_url, state io)
    artifact           - versioned artifact contracts for engine composition
"""

from . import artifact, boundary, guardrails, introspect, predicates, protocol, telemetry, toolkit
from .boundary import GOOSEIGNORE_FILENAME, BoundaryUnavailableError
from .branch_policy import BranchPolicy
from .config import LooperConfig
from .engine import Engine
from .environment import Environment
from .looper import GooseLooper
from .phase import Context, Phase, Pipeline
from .runlock import RUN_LOCK_FILENAME, RunLockHeldError

__all__ = [
    "BoundaryUnavailableError",
    "BranchPolicy",
    "Context",
    "Engine",
    "Environment",
    "GOOSEIGNORE_FILENAME",
    "GooseLooper",
    "LooperConfig",
    "Phase",
    "Pipeline",
    "RUN_LOCK_FILENAME",
    "RunLockHeldError",
    "artifact",
    "boundary",
    "guardrails",
    "introspect",
    "predicates",
    "protocol",
    "telemetry",
    "toolkit",
]

__version__ = "0.1.1"
