# ADR 0015 — THE BOUNDARY: bubblewrap-masked filesystem for every goose spawn

**Status:** Accepted (2026-07-13)

## Context

Whatever a goose phase's shell can see, a sufficiently manipulated phase
can read and exfiltrate. The egress tripwire (ADR 0014) redacts what it
recognizes AFTER the value has already reached the model provider; prompt
preambles ask nicely. Neither binds a shell. The operating system can:
if a path does not exist inside the process's mount namespace, no amount
of prompt injection reads it.

Alternatives considered:

- **Allow-list** (loop root + declared inputs visible, nothing else).
  Stronger, but every engine grows a manifest of everything goose
  legitimately touches — its own config, caches, `/usr`, the provider
  keychain path — and the first missed entry breaks every loop. Rejected
  by the operator in favor of deny-list: the filesystem looks exactly
  like an ordinary run, minus the masks.
- **Reusing `.gitignore`.** Same syntax, wrong meaning: runtime outputs
  (`reviews/`, journals) are gitignored by convention (§10) and are
  precisely what phases must write. A separate file with goose-may-not-
  touch semantics avoids poisoning either contract.

## Decision

Every goose invocation is spawned under a bubblewrap prefix
(`gooseloop.boundary`), resolved once per pass by the looper before any
session artifact exists:

- `bwrap --die-with-parent --dev-bind / /` keeps filesystem, devices,
  network, and env identical to an unsandboxed run. The boundary is ONLY
  the masks: a masked directory becomes an empty `--tmpfs`, a masked file
  a `--ro-bind /dev/null` (content empty; the name still lists — hiding
  the name requires masking the directory).
- **The built-in floor always applies** when bubblewrap is available:
  `.env*`, `*.pem`, `*.key`, `id_rsa*`/`id_ed25519*`/`id_ecdsa*`,
  `credentials*`, `*.keyring` as basename globs, plus anchored homes of
  credentials (`~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.kube`, `~/.netrc`,
  `~/.npmrc`, `~/.pypirc`, gcloud, docker config, keyrings).
- **`.gooseignore` at the loop root EXTENDS the floor.** Gitignore-style
  syntax: basename globs and anchored paths, `#` comments. No `!`
  negation — a hole you can punch in a security boundary is a boundary
  with a hole; the parser refuses the file loudly.
- **File present but bubblewrap missing: the run is REFUSED** (exit 4,
  distinct from 1 run-error / 2 usage / 3 lock-held), before any session
  artifact is created. The operator demanded a boundary that cannot be
  provided. No file and no bubblewrap: the run proceeds with a one-line
  stderr nudge.
- Masks are found by one filesystem walk of `$HOME` (plus the loop root
  when outside it). Toolchain caches (`~/.cargo`, `~/.rustup`, `~/.npm`,
  `~/.local`, browser profiles, …) are never descended: measured, they
  cost 15s and ~800 false masks (crate test fixtures named
  `credentials.rs`, `server.pem`); real secrets under skipped trees are
  covered by anchored floor entries instead. Over-masking is the safe
  failure direction; the walk lands around two seconds.

## Consequences

- The prompt-injection class that reads credential files dies at the OS:
  inside the sandbox `cat .env` prints nothing and `~/.ssh` is empty.
  What remains reachable is what the deny-list misses — the floor is a
  floor, and each project's `.gooseignore` is where its own secrets get
  named. The file is committed, so the boundary travels with the repo.
- Phases keep full write access to everything unmasked, by design
  (deny-list). The boundary is confidentiality, not integrity.
- Secrets already IN the spawn environment (provider API keys) are
  inherited by goose as before — masking `.env` files does not strip
  `os.environ`. Goose could not run otherwise. The env is the operator's
  to curate.
- Session logs record `boundary: N paths masked (bwrap)` per pass, so a
  run's telemetry states whether it was sandboxed.

Protocol text: §15. Tripwire layer: ADR 0014.

## Amendment (2026-07-14): the mask map persists, and is itself masked

The session log recorded a count ("291 paths masked"), not a list — so
"a phase read an empty config because the floor caught a file it
legitimately needed" dead-ended, and run A's boundary could not be
diffed against run B's. Saved runs now write
`<session>/boundary-masks.json`: the patterns in force (floor +
`.gooseignore`, in order) and the exact masked paths, `~`-shortened,
paths only.

The map gets the boundary's own treatment, because a list of where
credentials live is reconnaissance material: `boundary-masks.json` is on
the BUILTIN_DENY floor (every past session's copy is caught by the
ordinary scan) and the current run's copy is appended to the spawn
prefix after it is written. Inside the sandbox the map reads empty; the
operator and the dash read it normally. Unsandboxed runs write
`{"enforced": false}` so the artifact states the nudge case too.
