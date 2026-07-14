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

import re
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
    """Minimal Environment: env_vars() + the NAMES list, read from the
    user-procured names file (one per line) instead of hardcoded."""

    def __init__(self, names: list[str] | None = None,
                 greetings_dir: Path | None = None,
                 names_file: Path | None = None) -> None:
        # Empty names is a legal construction; precheck is where a run
        # refuses it with the exact fix (never a mid-pipeline surprise).
        self.names = names if names is not None else []
        self.greetings_dir = greetings_dir or (Path.cwd() / "greetings")
        self.names_file = names_file

    def env_vars(self) -> dict[str, str]:
        return {
            "NAMES": ",".join(self.names),
            "GREETINGS_DIR": str(self.greetings_dir),
        }

    # Recipes paste env_method:names_listing into their context: block.
    def names_listing(self) -> str:
        return "\n".join(f"- {n}" for n in self.names)


# A guest-list line must LOOK like a name: letters, digits, spaces,
# hyphens, apostrophes, periods — none of the punctuation injections
# lean on (brackets, colons, exclamation marks).
_NAME_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9][A-Za-zÀ-ÖØ-öø-ÿ0-9 '.\-]{0,59}")


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


def _skip_if_greeting_exists(params: dict) -> str | None:
    """Skip a greet invocation when its greeting is already on disk.

    Re-runs become idempotent: only names without a greeting hit the
    model, and the operator sees a one-line skip reason in the session
    log. Delete the file to force a re-greet. Same pattern as
    git_recap's _skip_if_recap_exists.
    """
    path = _greeting_path(params)
    if path is None:
        return None  # no name param; let the recipe fail naturally
    if path.exists() and path.stat().st_size > 0:
        return f"greeting already on disk: {path.name}"
    return None


class HelloEngine(Engine):
    """Reference engine: review picks names, body greets each, summary lists results."""

    def precheck(self, ctx) -> None:
        env = ctx.environment
        if isinstance(env, HelloEnvironment) and env.names:
            bad = [n for n in env.names if not _NAME_RE.fullmatch(n)]
            if bad:
                shown = ", ".join(repr(b[:40]) for b in bad[:3])
                raise RuntimeError(
                    f"hello-world: {len(bad)} line(s) in the names file do "
                    f"not look like names ({shown}). Names are letters, "
                    "digits, spaces, hyphens and apostrophes, max 60 chars. "
                    "This is a prompt-injection seatbelt: a guest list "
                    "is untrusted input, and a 'name' that reads like "
                    "instructions has no business reaching a prompt. A "
                    "seatbelt, not a guarantee."
                )
        if not isinstance(env, HelloEnvironment) or not env.names:
            where = getattr(env, "names_file", None)
            hint = (f"cp names.example.txt {where.name}" if where
                    else "cp names.example.txt names.txt")
            raise RuntimeError(
                "hello-world: no names to greet. Create the names file "
                f"({where or 'names.txt'}, one name per line):\n  {hint}\n"
                "or point [hello_world] names at your own file in gooseloop.toml."
            )

    @property
    def name(self) -> str:
        return "hello-world"

    def recipes_dir(self) -> str:
        return str(_HERE / "recipes")

    # output_path computes where one greet invocation writes; output_env
    # names the env var the framework injects that path under, so the
    # recipe's ${GREETING_FILE} and the success check share one source of
    # truth. The framework refuses the run if greet.yaml stops referencing
    # ${GREETING_FILE} (ADR 0011). skip_when makes re-runs idempotent: a
    # name whose greeting already exists is skipped before any model call.
    # The review still routes every name on purpose — the skip machinery
    # declining them is visible in the log and footer, which is the lesson.
    branch_policies = {
        "greet": BranchPolicy(
            output_path=_greeting_path,
            output_env="GREETING_FILE",
            skip_when=_skip_if_greeting_exists,
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
