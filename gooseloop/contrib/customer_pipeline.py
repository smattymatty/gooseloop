"""CustomerPipelineEnvironment — shape contract for customer-acquisition pipelines.

Domain vocabulary: prospects, lifecycles, outreach, research, broadcast,
discovery questions, founder journal. Concrete environments (Storm's
StormEnvironment, future BetaCo-style users) subclass this to inherit
the contract and implement each abstractmethod.

Recipes call these by name via env_method:<name> in their context:
blocks.
"""

from abc import abstractmethod
from pathlib import Path

from ..environment import Environment


class CustomerPipelineEnvironment(Environment):
    """Environment contract for customer-acquisition engines.

    Required:
        env_vars()           - inherited from Environment.
        core_dir()           - root of project's non-runtime data.
        lifecycle_dirs()     - canonical ordered list of (name, path) pairs.
        lifecycle_dir(name)  - one lifecycle dir by name.
        output_dir(name)     - one output dir by name (outreach, research, broadcast).
        build_digest()       - compact text summary of every routable prospect.
        journal_text()       - operator's working journal, or "" if absent.
        manifest_text()      - project-level static context (brand voice etc.).

    Optional:
        questions_dir(), insight_dir(), repo_activity(), questions_listing().
        Default to empty / not-found sentinels; recipes that need them
        should declare the source non-optional so a missing implementation
        fails the run loud.
    """

    # ---- paths -------------------------------------------------------

    @abstractmethod
    def core_dir(self) -> Path:
        """Root of the project's non-runtime data (foundation docs, journal, inputs)."""

    @abstractmethod
    def lifecycle_dirs(self) -> list[tuple[str, Path]]:
        """All lifecycle dirs in canonical order, including non-routable ones."""

    @abstractmethod
    def lifecycle_dir(self, name: str) -> Path:
        """One lifecycle dir by canonical name (e.g. 'potential', 'active')."""

    @abstractmethod
    def output_dir(self, name: str) -> Path:
        """One output dir by canonical name (outreach, research, broadcast)."""

    # ---- content loaders --------------------------------------------

    @abstractmethod
    def build_digest(self) -> str:
        """Compact text summary of every routable prospect.

        The pre-computed form that recipes paste in via env_method:.
        Schema is environment-defined; engines treat the result as opaque text.
        """

    @abstractmethod
    def journal_text(self) -> str:
        """Operator's working journal as text, or "" if the file is missing."""

    @abstractmethod
    def manifest_text(self) -> str:
        """Project-level static context (brand voice, framing, posture)."""

    # ---- optional ---------------------------------------------------

    def questions_dir(self) -> Path | None:
        """Living-doc workspace for discovery question files. None = unused."""
        return None

    def insight_dir(self) -> Path | None:
        """Workspace for cadence-triggered watcher outputs. None = unused."""
        return None

    def repo_activity(self) -> str:
        """Recent commit activity across the project's tracked repos.

        Default returns "" (no repo activity tracked). Override to wire
        the activity-watch recipe to your repos.
        """
        return ""

    def questions_listing(self) -> str:
        """Plain-text listing of the discovery questions workspace.

        Default returns "" (workspace absent). Override to enumerate
        files for the questions-due recipe.
        """
        return ""
