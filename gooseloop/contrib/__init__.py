"""Shape-specific Environment contracts.

Per ADR 0005 the framework Environment ABC has one abstract method
(env_vars). Shape-specific contracts live here as separate ABCs that
inherit from Environment and add their domain vocabulary:

    CustomerPipelineEnvironment - for customer-acquisition pipelines.
    ClaudeHandoffEnvironment    - for Claude design-handoff engines.

A concrete environment subclasses whichever mixin matches its domain
(or bare Environment if no mixin fits). Recipes call env_method:<name>
against the live instance regardless of mixin lineage.
"""

from .claude_handoff import ClaudeHandoffEnvironment
from .customer_pipeline import CustomerPipelineEnvironment

__all__ = ["ClaudeHandoffEnvironment", "CustomerPipelineEnvironment"]
