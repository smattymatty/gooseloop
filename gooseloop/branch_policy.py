"""BranchPolicy — per-recipe rules the framework applies when building body Phases.

Per ADR 0007 and PROTOCOL.md §5. The framework consults the engine's
branch_policies dict for each routing[] entry the review emitted, looking
up by recipe name. Recipes with no entry get BranchPolicy() defaults
(no skip, no path tracking, no extra predicate, intent unchecked).

Authors of an engine register policies like:

    class MyEngine(Engine):
        branch_policies = {
            "to-outreach": BranchPolicy(
                skip_when=lambda p: (outreach_dir / f"{p['slug']}.md").exists(),
                output_path=lambda p: outreach_dir / f"{p['slug']}.md",
                intent="produce",
            ),
        }
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Optional


Intent = Literal["produce", "edit", "edit-or-produce"]


@dataclass
class BranchPolicy:
    """Per-recipe rules. All fields optional; the default is the no-op policy.

    output_path: called with the routing entry's params dict to compute the
        deterministic file path the recipe must write. The framework injects
        the computed path into the phase's env under the name in
        `output_env`, so the recipe writes to ${OUTPUT_PATH} (or
        ${<output_env>}) verbatim. The same path derives the default success
        predicate (file_nonempty) and is recorded in the session ledger on
        success — the write target, the check, and the report can never
        disagree (ADR 0011).
    output_env: the env var name the computed output path is injected
        under. The framework verifies before any phase runs that the
        recipe's prompt references ${<output_env>}; a mismatch is a hard
        error, never a silent no-op (ADR 0011).
    skip_when: called with the routing entry's params dict.
        Truthy return skips the phase. A str return is used as the
        skip reason in the session log.
    predicate: explicit success predicate override. Takes the recipe's
        stdout. If unset and output_path is set, the framework derives a
        file_nonempty predicate from output_path.
    intent: declarative tag for the recipe's intent against output_path.
        Currently informational; reserved for future intent-reconciliation
        checks. None = unchecked.
    """
    skip_when: Optional[Callable[[dict[str, Any]], "bool | str | None"]] = None
    output_path: Optional[Callable[[dict[str, Any]], Optional[Path]]] = None
    predicate: Optional[Callable[[str], bool]] = None
    intent: Optional[Intent] = None
    output_env: str = "OUTPUT_PATH"
