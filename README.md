# gooseloop

An execution shell for [goose](https://block.github.io/goose/) recipe pipelines.

`gooseloop` runs an **Engine** against an **Environment**, driving a `Pipeline`
of `review -> body -> summary` through goose, with retry, rate-limit handling,
session bookkeeping, recipe overlay merging, and a typed session ledger.

It is generic. The framework knows nothing about your domain. Engines plug in.

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

## The shape

Three primitives:

- **Engine** (verbs). Returns a `Pipeline(review, body, summary)`. Owns
  branching, recipes, scoring, retry rules.
- **Environment** (nouns). One abstract method (`env_vars()`). Subclasses or
  contrib mixins (`gooseloop.contrib.CustomerPipelineEnvironment`,
  `gooseloop.contrib.ClaudeHandoffEnvironment`) add shape-specific contracts.
- **Pipeline** (the bookend). Review runs first and emits structured routing.
  Body phases run in queue order (cadence phases + review-spawned branches).
  Summary runs last and reads the final ledger.

The contract is documented in [`PROTOCOL.md`](PROTOCOL.md).
The design history lives in [`docs/adr/`](docs/adr/).

## Five minute tour

```python
from gooseloop import GooseLooper, Engine, Environment, Phase, Pipeline

class HelloEnvironment(Environment):
    def env_vars(self) -> dict[str, str]:
        return {"GREETING": "hello"}

class HelloEngine(Engine):
    name = "hello-world"

    def pipeline(self, ctx) -> Pipeline:
        return Pipeline(
            review=Phase(name="review", recipe_path="recipes/review.yaml"),
            body=[Phase(name="greet", recipe_path="recipes/greet.yaml")],
            summary=Phase(name="summary", recipe_path="recipes/summary.yaml"),
        )

GooseLooper(engine=HelloEngine(), environment=HelloEnvironment()).begin_loop()
```

The framework ships a working version of this engine under `engines/hello_world/`.

## Built-in engines

Three reference engines ship in `engines/`. They live alongside the framework,
not inside the installed wheel, so run them **from a checkout** (the repo root,
where `engines/` is importable) and select one with `-e <module>`:

| Engine | What it does | Run (from the repo root) |
|--------|--------------|--------------------------|
| `hello_world` | The minimal reference engine (the tour above). | `python3 -m gooseloop run -e engines.hello_world` |
| `git_recap` | Keeps a work journal across configured repos: one combined daily entry per date (commits since each repo's watermark), plus a weekly review when an ISO week closes. Configure `[git_recap]` in `gooseloop.toml`. | `python3 -m gooseloop run git_recap` |
| `doc_drift` | Finds derived docs/pages that fell behind their canonical source and drafts a patch to seal. Configure `[doc_drift]`, then `cp doc-map.example.toml doc-map.toml` and edit. | `python3 -m gooseloop run -e engines.doc_drift` |

Engines can also be run by short name — `python3 -m gooseloop run doc_drift`
resolves to `engines.doc_drift` by scanning the loop root's packages, and
`python3 -m gooseloop engines` lists everything it finds. Drop the engine
argument to run whatever `[gooseloop] default_engine` points at (`hello_world`
by default). The installed `gooseloop` console script is equivalent to
`python3 -m gooseloop`, but the built-in engines above still need a checkout on
the path. Add `--review-only` to any of them to stop after the review phase.

## Recipe overlay merge

Recipes compose docker-compose style. The looper resolves layers in this order:

1. Base recipe (declared in `gooseloop.toml`).
2. `<name>.local.yaml` (gitignored; per-machine).
3. `--review-overlay X.yaml` / `--summary-overlay X.yaml` (ad-hoc).

Inspect the merged result with `gooseloop recipe --resolve <name>`.

Merge rules table is in [PROTOCOL.md §6](PROTOCOL.md).

## Contrib mixins

Domain-shaped Environment ABCs ship under `gooseloop.contrib`:

- `CustomerPipelineEnvironment`: customer-acquisition pipelines
  (`build_digest()`, `journal_text()`, `lifecycle_dirs()`, ...).
- `ClaudeHandoffEnvironment`: Claude design-handoff engines
  (`handoff_dir()`, `target_repo()`, `dev_up_probe()`, ...).

An engine with no fit subclasses bare `Environment` and writes one method.

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

