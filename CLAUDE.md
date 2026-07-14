# CLAUDE.md

Guidance for an AI agent helping someone build a loop with **gooseloop**. If
you are that agent: read this first, then read the files it points you to, in
the order it gives. Do not invent the contract from the code; the contract is
written down (`PROTOCOL.md`) and the code obeys it.

## What gooseloop is

An execution shell for [goose](https://block.github.io/goose/) recipe
pipelines. It runs an **Engine** against an **Environment**, driving every loop
through one fixed shape:

```
review  ->  body  ->  summary
```

The **sandwich** is the whole mental model. `review` always runs first and emits
a structured plan. `body` does the work that plan routed. `summary` always runs
last and renders the ledger. The framework owns that order; an engine cannot
change it. Everything else is what you, the author, fill in.

The framework knows nothing about any domain. It is generic on purpose. A loop
plugs in via two objects and some recipes. That is the entire extension surface.

## The three primitives

- **Engine** (verbs). Returns a `Pipeline(review, body, summary)`. Owns
  branching rules (`branch_policies`), ships its recipe defaults. Subclass
  `gooseloop.Engine`.
- **Environment** (nouns). Declares what the engine can see. The ABC has one
  abstract method, `env_vars()`. Anything a recipe needs as pasted text is a
  method the recipe calls via `env_method:<name>`. Subclass
  `gooseloop.Environment`, or a `gooseloop.contrib.*` mixin if one fits your
  shape.
- **Pipeline** (the bookend). The `review -> body -> summary` dataclass an
  engine returns from `pipeline()`.

Public surface is whatever `gooseloop/__init__.py` exports. Import from there,
not from submodules.

## If you are an agent authoring a loop, do this

1. **Read `PROTOCOL.md` end to end.** It is canonical. It defines the
   review JSON schema (§2), the summary contract (§3), what body phases may and
   may not mutate (§4), `BranchPolicy` (§5), recipe overlay merge (§6), and the
   recipe `context:` block (§7). If the code ever disagrees with it, the code is
   the bug.

2. **Read the three example engines, in this order.** They are a deliberate
   progression along one axis: *who decides what the body does.*

   | Engine | Read it for | Routing |
   |---|---|---|
   | `engines/hello_world/` | every contract in its simplest form | model-driven: the review's `routing[]` decides the body |
   | `engines/git_recap/` | real I/O, cross-run watermark state, `skip_when` seatbelts on model routing | model-driven: review routes a daily (+ a weekly when the ISO week closed) |
   | `engines/doc_drift/` | the advanced end: deterministic body, `env_file` bundles, URL fetch, cross-run state, `post_process` | engine-driven: `pipeline()` builds the body itself; the model judges, it does not route |

   The first thing to settle for any new loop is where it sits on that axis. If
   the review can look at state and *decide* what work to do, you want
   model-driven routing (copy hello_world's shape). If the work is already
   determined and the model's only job is to *do* each unit, you want
   deterministic routing (copy doc_drift's shape). Getting this wrong is the
   most common way a first loop comes out awkward.

3. **Pull the loop's shape out of the human before writing code.** You need
   four answers. Ask for them plainly:
   - **Review's job.** What state does it look at, and what does it decide or
     report? (It must emit the §2 JSON, wrapped in the `<<<DELIVERABLE_JSON>>>`
     sentinels.)
   - **Body's work.** What is one unit of work, and what does each unit produce
     or edit? Is the set of units decided by the review (model-driven) or known
     up front (deterministic)?
   - **Summary's render.** What should the operator see at the end? It is
     human-facing, no schema.
   - **Environment's nouns.** What inputs does the loop read (files, globs, a
     journal, a config), and which get pasted into recipes as text?

4. **Scaffold by copying the closest example,** not from a blank file. New dir
   under `engines/<your_loop>/` with `engine.py` and a `recipes/` folder. Ship a
   `review.example.yaml` and a `summary.example.yaml` (users `cp` these into
   their project; PROTOCOL §10 shows the consuming-project layout). Point
   `gooseloop.toml` at your engine module. Validate any recipe with
   `gooseloop recipe --resolve <name>` before running the loop.

## Rules of the road (the load-bearing invariants)

These are in PROTOCOL.md in full; they are the ones most easily violated:

- The review's output is **frozen** once it runs. Body phases append to the
  ledger via `ctx.add_operator_action(...)`, `ctx.record_output(...)`,
  `ctx.session_log(...)`. They must not rewrite `routing`, `insights`,
  `summary`, or `status`.
- A body phase failure **does not abort the loop.** The summary still runs, and
  the operator sees what worked and what didn't. Design summaries to report
  partial runs honestly.
- Recipes are **procured, not edited in place.** The engine ships
  `*.example.yaml`; the user copies to `<name>.yaml` and overlays per-machine
  tweaks in `<name>.local.yaml` (gitignored). Overlay merge rules are §6.
- An `env_method:` source takes **no arguments** and cannot see a phase's
  params. Per-unit content the body needs must be passed via `env_file:`
  pointing at a real file the engine wrote (this is why doc_drift pre-assembles
  bundles). Reach for this the moment a body phase needs data specific to its
  routing entry.
- Everything a recipe pastes into a prompt is **untrusted input** (PROTOCOL
  §15, SECURITY.md). The framework sandboxes goose spawns (the boundary) and
  redacts secret-shaped output (the tripwire); an engine's job is the layer in
  between - validate inputs with a checkable shape before any model call, and
  name project-specific secrets in the committed `.gooseignore`.

## The examples are a teaching set, not furniture

hello_world, git_recap, and doc_drift exist to be read, run, and copied. Once a
project has its own engine, the examples can be deleted wholesale; the framework
does not depend on any of them. Keep them while you are learning the shape;
remove them when they are noise. `gooseloop.toml` ships pointing at hello_world
as the reference; a real project repoints `default_engine` at its own.

## Conventions

- `docs/adr/` holds the design history. When a decision is already recorded
  there (e.g. ADR 0006 for the named-slot Pipeline, 0007 for the review schema),
  link it rather than re-arguing it.
- Runtime output is gitignored by convention (`reviews/`, and each engine's
  configured output dir). `.gitignore` has no inline comments: a trailing
  `# ...` becomes part of the pattern and silently matches nothing, so keep
  every comment on its own line.
- Writing prose for this repo (docs, READMEs, recipe descriptions): direct and
  plain. No marketing filler, no em-dashes, no emoji in body copy.
