"""Core data types: Phase, Pipeline, Context.

See ADRs 0001, 0006, 0007 and gooseloop/PROTOCOL.md for the contracts these
types implement. The Pipeline is the bookend dataclass with named review /
body / summary slots; Context carries typed methods for body phases to
extend the session ledger.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from .protocol import OperatorAction, RoutingEntry

if TYPE_CHECKING:
    from .environment import Environment


@dataclass
class Context:
    """Carried through a single begin_loop() pass.

    `session_dir` is None when the Looper was constructed with save=False.
    `artifacts` is the engine's scratchpad; engines store extension data
    here and read each other's contributions across phases. The framework
    reserves a few well-known keys:

        artifacts["review_output"] -> dict (the parsed ReviewOutput payload)
        artifacts["operator_actions"] -> list[OperatorAction]
        artifacts["outputs_written"] -> list[str]

    Body phases should mutate these via the typed methods below
    (add_operator_action / record_output / session_log) rather than poking
    artifacts directly; the methods enforce the schema.
    """
    model: str
    session_dir: Optional[Path]
    base_env: dict[str, str]
    artifacts: dict[str, Any] = field(default_factory=dict)
    environment: Optional["Environment"] = None

    def add_operator_action(self, action: str, why: str = "", **extras: Any) -> None:
        """Append an operator action to the session ledger.

        Dedup is by (action, why); calling twice with the same pair is a
        no-op so cadence phases that re-detect the same condition do not
        flood the ledger. Extras are stored alongside.

        `action` must be a non-empty string. `why` is optional and may
        be empty — some actions don't need a stated reason; the action
        itself is the operator-facing artifact.
        """
        if not isinstance(action, str) or not action:
            raise TypeError("add_operator_action: 'action' must be a non-empty str")
        if not isinstance(why, str):
            raise TypeError("add_operator_action: 'why' must be a str (may be empty)")
        ledger: list[OperatorAction] = self.artifacts.setdefault("operator_actions", [])
        for existing in ledger:
            if existing.get("action") == action and existing.get("why") == why:
                return
        entry: OperatorAction = {"action": action, "why": why}
        entry.update(extras)  # type: ignore[typeddict-item]
        ledger.append(entry)

    @property
    def operator_actions(self) -> list[OperatorAction]:
        """Read-only view of the current ledger. Mutate via add_operator_action."""
        return list(self.artifacts.get("operator_actions", []))

    def record_output(self, path: Path | str) -> None:
        """Track a file body phases produced. Summary and footer render these."""
        outputs: list[str] = self.artifacts.setdefault("outputs_written", [])
        as_str = str(path)
        if as_str not in outputs:
            outputs.append(as_str)

    def session_log(self, message: str) -> None:
        """Append a timestamped line to the session log, if a session is open."""
        if self.session_dir is None:
            return
        from .session import log_step
        log_step(self.session_dir, message)

    @property
    def review_routing(self) -> list[RoutingEntry]:
        """Routing entries the review emitted (frozen at bookend)."""
        return list(self.artifacts.get("review_routing", []))


# Callable type aliases for Phase fields.
BuildEnv = Callable[[Context], dict[str, str]]
SuccessPredicate = Callable[[str], bool]
PostProcess = Callable[[str, Context], Optional[list["Phase"]]]
# SkipIf returns falsy to run, True for a generic skip, or a str carrying
# the reason. The reason lands in the session log.
SkipIf = Callable[[Context], "bool | str | None"]


def _empty_env(_ctx: Context) -> dict[str, str]:
    return {}


@dataclass
class Phase:
    """A single recipe invocation, with optional pre/post hooks.

    Attributes:
        name: shown in banners and logs.
        recipe_path: relative path to the recipe yaml.
        build_env: returns env vars merged with base_env for this call.
        success_predicate: optional override for "did this attempt succeed?".
            None falls through to the looper's transient-error check.
        post_process: called after a successful run. May return a list of
            child Phases for the Looper to enqueue.
        skip_if: called before the recipe runs. Truthy return skips the
            Phase. A string return is used as the skip reason in the log.
        label: optional override for the per-call footer label. Useful when
            multiple phases reuse the same recipe (review-spawned branches).
    """
    name: str
    recipe_path: str
    build_env: BuildEnv = _empty_env
    success_predicate: Optional[SuccessPredicate] = None
    post_process: Optional[PostProcess] = None
    skip_if: Optional[SkipIf] = None
    label: Optional[str] = None


@dataclass
class Pipeline:
    """Named-slot pipeline. Framework owns review-first / summary-last ordering.

    Engines return this from Engine.pipeline(). Per ADR 0006:

        - review runs first; its post_process and the framework parse the
          review output and spawn child phases (via the BranchPolicy
          registry) that go to the HEAD of the body queue.
        - body runs after, in queue order. May be empty.
        - summary runs last, with access to the final operator_actions
          ledger and outputs list.

    `--review-only` runs review and stops; body and summary skip.
    """
    review: Phase
    body: list[Phase] = field(default_factory=list)
    summary: Optional[Phase] = None
