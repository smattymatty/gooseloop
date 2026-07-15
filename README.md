# gooseloop

An execution shell for [goose](https://block.github.io/goose/) recipe pipelines.

`gooseloop` runs an **Engine** against an **Environment**, driving a `Pipeline`
of `review -> body -> summary` through goose, with retry, rate-limit handling,
session bookkeeping, recipe overlay merging, and a typed session ledger.

It is generic. The framework knows nothing about your domain: you `pip install`
it, write an engine in your own project, point gooseloop at it, and that engine
does whatever your business logic requires. gooseloop ships the execution shell;
your engine, environment, and recipes are the domain, and they live in your
repo, not this one.

gooseloop is an independent project built on Block's
[goose](https://block.github.io/goose/) (Apache 2.0) as its layer of execution
primitives: recipes are goose recipes, and every phase is a goose CLI call the
engine makes. gooseloop is not affiliated with or endorsed by Block, Inc.

## Install

```
pip install gooseloop
```

Or, from a checkout:

```
pip install -e .
```

Requires Python 3.11+, the `goose` CLI on `$PATH`, and a YAML recipe directory.

## How it fits together

You install gooseloop as a library. Your loop lives in **your** project:

```
my-project/                 # gooseloop is pip-installed; this repo is yours
├── gooseloop.toml          # default_engine = "my_engine"; recipes, retry, etc.
├── my_engine/              # your engine package, importable from the project root
│   ├── __init__.py         # exposes `engine` (and optional `environment`)
│   ├── engine.py           # your Engine subclass; returns the Pipeline
│   └── recipes/            # your review / summary / body *.example.yaml
├── review.yaml             # cp'd from the engine's review.example.yaml
├── summary.yaml            # cp'd from the engine's summary.example.yaml
└── reviews/sessions/       # gooseloop-managed run output (gitignored)
```

Run it from the project root: `gooseloop run` (the `default_engine`), or
`gooseloop run my_engine` by name. The framework never ships your domain; it
imports the engine your `gooseloop.toml` points at. The full consuming-project
contract is [PROTOCOL.md §10](PROTOCOL.md).

## The shape

Three primitives:

- **Engine** (verbs). Returns a `Pipeline(review, body, summary)`. Owns its
  `branch_policies`, its recipe defaults, and retry rules.
- **Environment** (nouns). One abstract method (`env_vars()`). Everything else
  your recipes need is a method you add to your own concrete class — the
  framework ships no domain-specific base classes. If several of your engines
  share a domain, factor the shared methods into your own base ABC in your
  project.
- **Pipeline** (the bookend). Review runs first and emits structured routing.
  Body phases run in queue order (cadence phases + review-spawned branches).
  Summary runs last and reads the final ledger.

The contract is documented in [`PROTOCOL.md`](PROTOCOL.md).
The design history lives in [`docs/adr/`](docs/adr/).

## Five minute tour

The skeleton of an engine and its environment:

```python
from gooseloop import GooseLooper, Engine, Environment, Phase, Pipeline

class HelloEnvironment(Environment):
    def env_vars(self) -> dict[str, str]:
        return {"NAMES": "Ada,Grace"}

class HelloEngine(Engine):
    @property
    def name(self) -> str:
        return "hello-world"

    def pipeline(self, ctx) -> Pipeline:
        return Pipeline(
            review=Phase(name="review", recipe_path="recipes/review.example.yaml"),
            body=[],  # the review's routing[] spawns one greet phase per name
            summary=Phase(name="summary", recipe_path="recipes/summary.example.yaml"),
        )

GooseLooper(engine=HelloEngine(), environment=HelloEnvironment()).begin_loop()
```

This is the skeleton. A real engine lives in its own package alongside its
recipes and fills in the rest: a `precheck` seatbelt on its inputs, the
`branch_policies` that route the review's `routing[]` into body phases, and
resolved recipe paths. The `engines/hello_world/` reference engine below is that
full version.

## Reference engines (a teaching set, not part of the wheel)

Three reference engines ship in this repo under `engines/`, to be read, run, and
copied. They live **beside** the framework, not inside the installed wheel, so
you run them from a checkout of this repo (the repo root, where `engines/` is
importable) and select one with `-e <module>`:

| Engine | What it does | Run (from the repo root) |
|--------|--------------|--------------------------|
| `hello_world` | The minimal reference engine (the tour above, in full). | `gooseloop run -e engines.hello_world` |
| `git_recap` | Keeps a work journal across configured repos: one combined daily entry per date (commits since each repo's watermark), plus a weekly review when an ISO week closes. Configure `[git_recap]` in `gooseloop.toml`. | `gooseloop run -e engines.git_recap` |
| `doc_drift` | Finds derived docs/pages that fell behind their canonical source and drafts a patch to seal. Configure `[doc_drift]`, then `cp doc-map.example.toml doc-map.toml` and edit. | `gooseloop run -e engines.doc_drift` |

Engines can also be run by short name — `gooseloop run doc_drift` resolves to
`engines.doc_drift` by scanning the loop root's packages, and `gooseloop engines`
lists everything it finds. Drop the engine argument to run whatever
`[gooseloop] default_engine` points at (`hello_world` by default). The
`gooseloop` console script and `python3 -m gooseloop` are equivalent; the
reference engines above still need a checkout on the path. Add `--review-only`
to any of them to stop after the review phase. Once your project has its own
engine, delete `engines/` — the framework does not depend on it.

## Recipe overlay merge

Recipes compose docker-compose style. The looper resolves layers in this order:

1. Base recipe (declared in `gooseloop.toml`).
2. `<name>.local.yaml` (gitignored; per-machine).
3. `--review-overlay X.yaml` / `--summary-overlay X.yaml` (ad-hoc).

Inspect the merged result with `gooseloop recipe --resolve <name>`.

Merge rules table is in [PROTOCOL.md §6](PROTOCOL.md).

## CLI

```
gooseloop run                      # run the default engine, one pass
gooseloop run doc_drift            # run any engine by short name
gooseloop run --review-only        # stop after review
gooseloop run --review-overlay x.yaml --summary-overlay y.yaml
gooseloop recipe --resolve review  # print fully-merged recipe
gooseloop engines                  # list every engine in the loop root
```

`gooseloop.toml` at the project root configures the default engine, recipes,
retry tuning, sessions dir. One loop root can host many engines; the default
is only what a bare `gooseloop run` runs.

## License

Apache 2.0, matching the goose layer it builds on. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
