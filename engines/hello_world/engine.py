"""Hello-world reference engine.

Demonstrates every gooseloop contract:

    - Engine returns Pipeline(review, body, summary) (ADR 0006).
    - Engine declares branch_policies for its routing entries (ADR 0007).
    - Engine ships *.example.yaml recipes users cp into their project (ADR 0008).
    - Environment subclasses bare gooseloop.Environment (no contrib mixin
      needed for trivial cases) — illustrates the "one method, no
      obligations" path opened by ADR 0005.

The review reads a list of NAMES from the environment and emits one
routing[] entry per name. Each body invocation of `greet` says hello to
one name. Summary renders the ledger.
"""

from __future__ import annotations

from pathlib import Path

from gooseloop import (
    BranchPolicy,
    Context,
    Engine,
    Environment,
    Phase,
    Pipeline,
)


_HERE = Path(__file__).resolve().parent


class HelloEnvironment(Environment):
    """Minimal Environment: just env_vars() + a NAMES list for the review."""

    def __init__(self, names: list[str] | None = None,
                 greetings_dir: Path | None = None) -> None:
        self.names = names or ["world", "operator", "goose"]
        self.greetings_dir = greetings_dir or (Path.cwd() / "greetings")

    def env_vars(self) -> dict[str, str]:
        return {
            "NAMES": ",".join(self.names),
            "GREETINGS_DIR": str(self.greetings_dir),
        }

    # Recipes paste env_method:names_listing into their context: block.
    def names_listing(self) -> str:
        return "\n".join(f"- {n}" for n in self.names)


def _greeting_path(params: dict) -> Path | None:
    """Compute the output path for one greet invocation.

    The framework injects the path this returns into the greet phase's env
    under the policy's output_env name (GREETING_FILE below); greet.yaml
    writes to ${GREETING_FILE} verbatim. The same path drives the phase's
    success check and its ledger entry, so all three always agree
    (ADR 0011, PROTOCOL §5).

    Reads GREETINGS_DIR from the env vars the looper builds at run time,
    not at engine-construction time, because the directory is environment-
    owned. Returns None if no `name` param was provided.
    """
    name = params.get("name")
    if not name:
        return None
    import os
    base = Path(os.environ.get("GREETINGS_DIR", "greetings"))
    return base / f"{name}.txt"


class HelloEngine(Engine):
    """Reference engine: review picks names, body greets each, summary lists results."""

    @property
    def name(self) -> str:
        return "hello-world"

    def recipes_dir(self) -> str:
        return str(_HERE / "recipes")

    # output_path computes where one greet invocation writes; output_env
    # names the env var the framework injects that path under, so the
    # recipe's ${GREETING_FILE} and the success check share one source of
    # truth. The framework refuses the run if greet.yaml stops referencing
    # ${GREETING_FILE} (ADR 0011).
    branch_policies = {
        "greet": BranchPolicy(
            output_path=_greeting_path,
            output_env="GREETING_FILE",
        ),
    }

    def pipeline(self, ctx: Context) -> Pipeline:
        recipes = _HERE / "recipes"
        return Pipeline(
            review=Phase(
                name="review",
                recipe_path=str(recipes / "review.example.yaml"),
            ),
            body=[],  # all body work comes from the review's routing[]
            summary=Phase(
                name="summary",
                recipe_path=str(recipes / "summary.example.yaml"),
            ),
        )
