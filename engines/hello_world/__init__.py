"""Hello-world reference engine.

A minimal Engine + Environment pair that demonstrates the framework
contract end-to-end without depending on any domain (no prospects, no
panels, no project-specific files).

Exposed at module level:
    engine        - HelloEngine class. The CLI instantiates this.
    environment   - HelloEnvironment class. The CLI instantiates this.

Recipes ship under recipes/:
    review.example.yaml   - the bookend; emits a routing entry per name in NAMES.
    greet.yaml            - body recipe; greets one name.
    summary.example.yaml  - the bookend; renders the ledger.

Copy review.example.yaml -> review.yaml and summary.example.yaml ->
summary.yaml in a consuming project, then run `gooseloop run`.
"""

from .engine import HelloEngine, HelloEnvironment

engine = HelloEngine
environment = HelloEnvironment(names=["Canada", "Goose", "Canadian Goose"])

__all__ = ["HelloEngine", "HelloEnvironment", "engine", "environment"]
