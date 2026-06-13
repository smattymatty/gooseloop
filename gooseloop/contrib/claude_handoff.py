"""ClaudeHandoffEnvironment — shape contract for Claude design-handoff engines.

Domain vocabulary: handoff folder, target repo, dev-up probe, panel
inventory, screenshot baseline. A handoff engine receives a
claude-handoff.toml in the target repo, drives Claude through a
survey → implement-panel → screenshot-verify → status loop, and never
provisions the target's dev environment itself.

Per the feedback memory `feedback-handoff-engine-provisioning-contract`:
the engine NEVER auto-provisions the target's dev stack. `dev_up_probe()`
checks dev is up; if not, the engine fails loud and the operator owns
the lifecycle.
"""

from abc import abstractmethod
from pathlib import Path

from ..environment import Environment


class ClaudeHandoffEnvironment(Environment):
    """Environment contract for Claude design-handoff engines.

    Required:
        env_vars()         - inherited from Environment.
        handoff_dir()      - directory of handoff specs the engine consumes.
        target_repo()      - root of the repo the handoff applies to.
        dev_up_probe()     - shell command (str) checking dev is reachable.

    Optional:
        panel_inventory(), screenshot_baseline_dir(), handoff_toml_path().
    """

    @abstractmethod
    def handoff_dir(self) -> Path:
        """Directory containing handoff specs (markdown or toml) to consume."""

    @abstractmethod
    def target_repo(self) -> Path:
        """Root of the repository this handoff applies to."""

    @abstractmethod
    def dev_up_probe(self) -> str:
        """Shell command whose zero exit code means dev is reachable.

        Example: 'curl -sf http://localhost:8000/healthz'. The engine
        runs this in precheck; non-zero aborts with operator-action
        instructing dev to be brought up.
        """

    def panel_inventory(self) -> str:
        """Plain-text inventory of panels / UI components in scope.

        Default returns "" (no inventory enforced). Override to scope
        the engine to a specific subset of the target.
        """
        return ""

    def screenshot_baseline_dir(self) -> Path | None:
        """Directory of pre-handoff baseline screenshots for verification.

        Default None (no baseline; verifier runs without diffs). Override
        to enable visual-regression checks.
        """
        return None

    def handoff_toml_path(self) -> Path | None:
        """Path to the target repo's claude-handoff.toml, if any.

        Default None (the engine works from `handoff_dir()` alone).
        Override when the target codebase carries its own per-target
        config.
        """
        return None
