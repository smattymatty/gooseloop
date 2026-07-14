# Security

gooseloop drives tool-equipped language models with recipe pipelines.
That makes prompt injection a first-class threat, not an edge case: any
content a recipe pastes into a prompt (files, model outputs, fetched
pages) can try to steer a shell-capable phase. This document states the
threat model, what the framework does about it, and what it deliberately
does not promise.

## Reporting a vulnerability

Report privately via GitHub security advisories:
https://github.com/smattymatty/gooseloop/security/advisories/new

Please do not open a public issue for anything exploitable. You can
expect an acknowledgement within a few days. There is no bug bounty;
credit is given in the changelog unless you ask otherwise.

Only the latest release line (0.x) is supported with fixes.

## Threat model

The attacker is a line of text. Untrusted content enters prompts by
design, and a model that follows a hostile instruction can use whatever
its tools reach: the shell, the filesystem, the network. Assume input
filtering fails; natural language has no grammar of intent. The
framework therefore defends in layers, each one deterministic and
OS- or code-enforced rather than prompt-enforced where possible.

## The layers

1. **Context fencing** (prompt level, weakest). Every rendered recipe
   wraps injected content in sentinel blocks and instructs the model
   that the blocks are untrusted data, not instructions. This raises
   the bar; it binds nothing.

2. **Input seatbelts** (code level). Engines validate their inputs
   before any model call where the input has a checkable shape (for
   example, the hello_world engine refuses guest-list lines that do not
   look like names). Seatbelts, not guarantees.

3. **The boundary** (OS level, PROTOCOL section 15, ADR 0015). When
   bubblewrap is available, every goose process runs inside a mount
   namespace where credential-shaped paths do not exist: `.env*`,
   `*.pem`, `*.key`, `credentials*`, `~/.ssh`, `~/.aws`, `~/.gnupg` and
   the rest of the built-in floor, extended per project by a committed
   `.gooseignore` file. A masked file reads as empty. If a
   `.gooseignore` is present and bubblewrap is missing, the run is
   refused (exit 4) rather than silently degraded.

4. **The egress tripwire** (code level, ADR 0014). Every phase
   transcript and summary is scanned before it persists. Secret-shaped
   values (known token signatures, KEY=value assignments with
   secret-naming keys, PEM blocks) are redacted from the on-disk
   artifacts, the phase event is flagged, and a ROTATE CREDENTIALS
   action is raised into the operator's queue.

## What is NOT protected

Be honest with yourself about these before running loops over content
you do not control:

- **Anything already in the environment.** goose inherits the spawn
  environment, including provider API keys; it could not run otherwise.
  The boundary masks files, not `os.environ`. Curate what you export.
- **goose's own credential store.** The model's shell runs with goose's
  privileges, and whatever goose can read to authenticate (its config,
  its keyring access) is in the same trust domain. Masking it would
  break every run. Prefer keyring-backed provider secrets over files,
  and treat the provider key as the one secret a loop always carries.
- **Anything outside the deny-list.** The boundary is a deny-list by
  design (the filesystem looks normal, minus the masks). Secrets in
  unconventionally named files need a `.gooseignore` entry.
- **Egress that reaches the provider.** Output scanning happens when
  gooseloop persists artifacts. A value the model printed has already
  been sent to the model provider; the raised action says rotate, and
  rotation is the remedy. Redaction protects the artifact trail, not
  the wire.
- **Write access.** The boundary is confidentiality, not integrity.
  Phases can write anywhere the operator can, minus the masks.
- **Denial of wallet.** A manipulated phase can burn tokens. Watch your
  runs; that is what the dashboard and the run lock are for.

## Operator practice

- Install bubblewrap and commit a `.gooseignore` naming your project's
  secrets. The refusal semantics then guarantee no one on the team runs
  unsandboxed by accident.
- Treat a ROTATE CREDENTIALS card as real every time. The tripwire only
  fires on evidence.
- Run loops as a user whose reachable filesystem you would be willing
  to paste into a prompt, because that is the actual exposure.
