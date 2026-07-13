# ADR 0011 — `output_env`: author-named output env var, verified against the recipe

**Status:** Accepted (2026-07-13)
**Context:** the `output_path -> ${OUTPUT_PATH}` wire was invisible to engine
authors; a first-time author reading hello_world could not connect the
BranchPolicy field to the variable the recipe writes to

## Context

`BranchPolicy.output_path` does three jobs from one computed path: the looper
injects it into the phase's env, derives the default `file_nonempty` success
predicate from it, and records it in the ledger on success. The env var name
was hardcoded to `OUTPUT_PATH` in `looper.py` and appeared nowhere an author
reads: not in BranchPolicy's docstring, not in PROTOCOL §5, not in the
hello_world teaching example. The fixed name existed for a real reason: when
the recipe and the predicate each derived their own filename, every
successful write looked like a failure and retried to exhaustion (the bug
recorded at the injection site in `looper.py`). But the fix for that bug
traded away discoverability, and the trade was never written down.

## Decision

1. **`BranchPolicy` gains `output_env: str = "OUTPUT_PATH"`.** The author
   names the env var the computed path is injected under; the recipe
   references `${<output_env>}`. The default keeps every existing engine and
   recipe working untouched (additive per PROTOCOL §9's "fields may be added
   with sensible defaults").
2. **The contract is verified, not trusted.** When a routing entry's policy
   computes an output path, the framework checks the prepared recipe text
   (after overlay merge and context rendering) for the literal
   `${<output_env>}`. A miss is a hard error before the phase spends a model
   call. A name mismatch is always a bug, never a style choice, so there is
   nothing to warn-and-continue about. This is what makes a configurable name
   safe: the silent-divergence failure the fixed name was built to kill now
   fails loud instead of being impossible.
3. **PROTOCOL §5 documents the full chain:** `output_path(params)` -> env var
   named `output_env` -> default `file_nonempty` predicate -> `record_output`
   on success. BranchPolicy's docstring leads with the injection, since it is
   the field's most author-visible effect.
4. **hello_world teaches the wire explicitly:** `output_env="GREETING_FILE"`
   in the engine, `${GREETING_FILE}` in greet.yaml, so the policy-to-recipe
   connection is visible in the first two files an author reads. git_recap
   and doc_drift keep the default, showing both modes across the teaching
   set.
5. **`intent` leaves hello_world.** It is reserved and unenforced
   ("informational; reserved for future intent-reconciliation checks"), and a
   no-op field in the simplest example reads as load-bearing. It stays on the
   dataclass, in PROTOCOL §5 marked reserved, and in the advanced examples.

## Considered options

- **Fixed name, made visible (docs only).** Rejected: a live authoring
  session proved the docs gap was the symptom, not the disease; the recipe
  reads better when the variable names its content (`${GREETING_FILE}` vs
  `${OUTPUT_PATH}`), and the divergence risk that justified the fixed name is
  handled better by verification than by rigidity.
- **Configurable name without verification.** Rejected outright: reintroduces
  the silent recipe/predicate divergence class of bug, now with more ways to
  trigger it (copying a recipe between engines with different `output_env`).

## Consequences

- Additive, no breaking change: engines that never set `output_env` behave
  exactly as before, except that a recipe which fails to reference its
  injected variable now refuses to run instead of failing its predicate
  after a wasted model call.
- A recipe copied from another engine with a different `output_env` fails
  loud at prepare time, naming both the expected variable and the recipe.
- The verification only applies when the policy computes a path; policies
  without `output_path` are untouched, and a routing entry whose params
  yield no path keeps the existing let-it-fail-naturally behavior.
