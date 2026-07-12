"""DocDriftEngine + DocDriftEnvironment.

The third reference engine. It reads a canonical→derived document map, finds
derived views that fell behind their canonical source, and drafts a patch to
the derived side for an operator to seal. Drafts are never auto-applied.

A side may be a FILE PATH or a live URL. A derived view is often a published
surface (a guide page, a blog post) whose only honest copy lives at its URL, so
checking the file it was built from is not enough. And a derived COLLECTION (a
whole blog) is discovered dynamically from a sitemap or index, so every post is
checked without listing them by hand.

Pipeline shape:

    review:   pastes the triage table (env_method:drift_candidates) and reports
              it to the operator. It does NOT route — routing is deterministic.
    body:     one draft-doc-patch invocation per drift candidate, built directly
              by the engine in pipeline(). Each reads a pre-assembled context
              bundle (canonical + derived, fetched if a URL) via env_file and
              writes a patch draft to <drafts_dir>/<pair>.patch.md.
    summary:  renders a drift report and, in its post_process, raises the
              operator actions (only for confirmed drift) and writes the state.

Why routing is deterministic, not model-driven:

    The triage already decided everything — which pairs changed, against which
    canonical. A model router would only transcribe that and, at the scale of a
    crawled blog, risk omitting or fabricating entries. So the engine builds the
    body phases itself. The model's real job (reading two documents and judging
    their disagreement) stays in the body, where it belongs.

The change signal:

    File sources carry a git commit sha (or content hash if untracked) AND a
    timestamp, so a file/file pair gets a temporal shortcut: a derived at least
    as recent as the canonical already followed it. URL sources usually expose
    no Last-Modified and no ETag, so they carry a content hash over normalized
    text and no usable timestamp; for those, drift is content-change-driven (a
    side changed since the last settled check). The body's verdict (real drift
    vs a false positive) is captured back into state so an unchanged in-sync
    pair stops re-routing.

A note on the framework: an env_method context source takes no arguments and
cannot see a phase's routing params, so per-pair content must be passed via
env_file pointing at a real file. That is why the body reads a bundle the engine
writes, not an env_method. (The parameterless drift_candidates table is fine.)
"""

from __future__ import annotations

import glob as globmod
import hashlib
import os
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

from gooseloop import (
    Context,
    Engine,
    Environment,
    Phase,
    Pipeline,
    predicates,
)
from gooseloop.toolkit import (
    FetchResult,
    Source,
    cap,
    fetch_url as _toolkit_fetch,
    html_to_text,
    load_state,
    parse_source,
    parse_sources,
    safe_filename,
    save_state as _save_state,
)


_HERE = Path(__file__).resolve().parent

_USER_AGENT = "doc-drift/1.0 (+https://stormdevelopments.ca)"
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


# ---- sources: Source/parse_source come from gooseloop.toolkit ----


def parse_canonical(raw, base: Path) -> tuple[Source, ...]:
    """Canonical may be a single source or a list (the derived must agree with
    all of them). Returns a tuple of Sources."""
    return parse_sources(raw, base)


# ---- the map: static pairs + dynamic collections -----------------


@dataclass(frozen=True)
class Pair:
    id: str
    canonical: tuple[Source, ...]
    derived: Source
    note: str
    intentional: bool


@dataclass(frozen=True)
class Collection:
    """A derived view that expands to many pairs by discovery.

    Discovery is one of:
      - `glob`    — a filesystem pattern (e.g. ../docs/**/*.md); each matching
                    file becomes a Pair. This is how a whole doc tree or wiki
                    gets checked without listing every page.
      - `sitemap` — a sitemap URL; each <loc> whose path matches `match`.
      - `index`   — an index page URL; each <a href> whose path matches `match`.
    Each discovered view is checked against the shared canonical(s).
    """
    id: str
    canonical: tuple[Source, ...]
    glob: Optional[str]
    sitemap: Optional[str]
    index: Optional[str]
    match: re.Pattern
    note: str
    intentional: bool


@dataclass(frozen=True)
class MapSpec:
    pairs: list[Pair]
    collections: list[Collection]


def load_map(map_path: Path) -> MapSpec:
    """Parse doc-map.toml into static Pairs and Collection specs. No network.

    Static [[pair]] entries are fully resolved here. [[collection]] entries are
    parsed but not expanded (expansion fetches a sitemap/index, which happens
    lazily in the environment). Raises ValueError on a malformed entry.
    """
    with open(map_path, "rb") as f:
        data = tomllib.load(f)
    base = map_path.resolve().parent

    pairs: list[Pair] = []
    for i, entry in enumerate(data.get("pair", [])):
        if not isinstance(entry, dict):
            raise ValueError(f"doc-map: [[pair]] #{i} is not a table")
        pid = str(entry.get("id", "")).strip()
        canonical = entry.get("canonical")
        derived = entry.get("derived")
        if not pid or not canonical or not derived:
            raise ValueError(
                f"doc-map: [[pair]] #{i} needs non-empty id, canonical, and derived "
                f"(got id={pid!r}, canonical={canonical!r}, derived={derived!r})"
            )
        note = str(entry.get("note", "")).strip()
        intentional = bool(entry.get("intentional", False))
        canon = parse_canonical(canonical, base)
        derived_list = derived if isinstance(derived, list) else [derived]
        multi = len(derived_list) > 1
        for d in derived_list:
            d = str(d).strip()
            sub_id = f"{pid}::{_short_name(d)}" if multi else pid
            pairs.append(Pair(sub_id, canon, parse_source(d, base), note, intentional))

    collections: list[Collection] = []
    for i, entry in enumerate(data.get("collection", [])):
        if not isinstance(entry, dict):
            raise ValueError(f"doc-map: [[collection]] #{i} is not a table")
        cid = str(entry.get("id", "")).strip()
        canonical = entry.get("canonical")
        glob = entry.get("glob")
        sitemap = entry.get("sitemap")
        index = entry.get("index")
        if not cid or not canonical or not (glob or sitemap or index):
            raise ValueError(
                f"doc-map: [[collection]] #{i} needs id, canonical, and one of "
                f"glob/sitemap/index (got id={cid!r}, canonical={canonical!r}, "
                f"glob={glob!r}, sitemap={sitemap!r}, index={index!r})"
            )
        match = str(entry.get("match", "")).strip()
        try:
            match_re = re.compile(match) if match else re.compile(r".")
        except re.error as e:
            raise ValueError(f"doc-map: [[collection]] {cid!r} has a bad match regex: {e}")
        collections.append(Collection(
            id=cid,
            canonical=parse_canonical(canonical, base),
            glob=str(glob).strip() if glob else None,
            sitemap=str(sitemap).strip() if sitemap else None,
            index=str(index).strip() if index else None,
            match=match_re,
            note=str(entry.get("note", "")).strip(),
            intentional=bool(entry.get("intentional", False)),
        ))

    return MapSpec(pairs=pairs, collections=collections)


def _short_name(raw: str) -> str:
    raw = raw.strip().rstrip("/")
    return raw.rsplit("/", 1)[-1] or raw


def _slug_from_url(url: str) -> str:
    path = urlparse(url).path.strip("/")
    return path.rsplit("/", 1)[-1] if path else (urlparse(url).netloc or "page")


# ---- revisions: the cheap, deterministic change signal -----------


@dataclass(frozen=True)
class Rev:
    token: str
    ts: Optional[int]
    exists: bool
    detail: str = ""


def doc_rev(path: Path) -> Rev:
    if not path.exists():
        return Rev("", None, False, detail=f"file does not exist: {path}")
    git = _git_rev(path)
    if git is not None:
        sha, ts = git
        return Rev(sha[:12], ts, True)
    return _content_rev(path)


def _git_rev(path: Path) -> Optional[tuple[str, int]]:
    cmd = ["git", "-C", str(path.parent), "log", "-1", "--format=%H %ct", "--", str(path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    sha, _, ct = proc.stdout.strip().partition(" ")
    try:
        return sha, int(ct)
    except ValueError:
        return None


def _content_rev(path: Path) -> Rev:
    data = path.read_bytes()
    return Rev("h:" + hashlib.sha256(data).hexdigest()[:12], int(path.stat().st_mtime), True)


def fetch_url(url: str, *, strip: bool = True) -> FetchResult:
    """gooseloop.toolkit.fetch_url with this engine's User-Agent."""
    return _toolkit_fetch(url, strip=strip, user_agent=_USER_AGENT)


def probe_url(url: str) -> Rev:
    r = fetch_url(url, strip=True)
    if r.text is None:
        return Rev("", None, False, r.error or f"could not fetch {url}")
    token = (f"etag:{_clean_etag(r.etag)}" if r.etag
             else "h:" + hashlib.sha256(r.text.encode("utf-8")).hexdigest()[:12])
    return Rev(token, r.last_modified_unix, True)


def _clean_etag(etag: str) -> str:
    return etag.strip().lstrip("W/").strip('"')


_HREF_RE = re.compile(r"href=[\"']([^\"']+)[\"']", re.IGNORECASE)
_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)


def _combine_canon(revs: list[Rev]) -> Rev:
    """Fold one or more canonical Revs into a single change signal."""
    missing = next((r for r in revs if not r.exists), None)
    if missing is not None:
        return Rev("", None, False, detail=missing.detail)
    if len(revs) == 1:
        return revs[0]
    joined = "|".join(r.token for r in revs)
    token = "multi:" + hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]
    timestamps = [r.ts for r in revs if r.ts is not None]
    return Rev(token, max(timestamps) if timestamps else None, True)


# ---- triage ------------------------------------------------------

CANDIDATE = "candidate"
IN_SYNC = "in-sync"
SKIP = "skip"
ERROR = "error"
SUPPRESSED = "suppressed"

_HANDLED_STATUSES = ("drafted", "dismissed")


@dataclass
class TriageRow:
    pair: Pair
    canon: Rev
    deriv: Rev
    verdict: str
    detail: str
    prior_status: Optional[str]


def _classify(pair: Pair, canon: Rev, deriv: Rev, prior: Optional[dict]) -> tuple[str, str]:
    """The whole triage decision, deterministic and testable."""
    if not canon.exists:
        return ERROR, canon.detail or "a canonical source is not readable"
    if not deriv.exists:
        return ERROR, deriv.detail or f"derived not readable: {pair.derived.value}"
    if pair.intentional:
        return SUPPRESSED, "declared intentional divergence (intentional = true)"

    if isinstance(prior, dict) \
            and prior.get("canon_token") == canon.token \
            and prior.get("deriv_token") == deriv.token:
        status = prior.get("status")
        if status in _HANDLED_STATUSES:
            return SKIP, f"already {status} at these revisions"
        if status == "in-sync":
            return IN_SYNC, "unchanged since the last in-sync check"

    if canon.ts is not None and deriv.ts is not None and deriv.ts >= canon.ts:
        return IN_SYNC, "derived view is at least as recent as the canonical"

    if prior is None:
        return CANDIDATE, "not yet verified against the canonical"
    return CANDIDATE, "a side changed since the last check"


# ---- the environment ---------------------------------------------


class DocDriftEnvironment(Environment):

    def __init__(
        self,
        map_path: Path,
        state_path: Path,
        drafts_dir: Path,
        recaps_dir: Optional[Path] = None,
    ) -> None:
        self.map_path = map_path
        self.state_path = state_path
        self.drafts_dir = drafts_dir
        # Optional: git-recap's per-commit summary folder. If it exists on disk,
        # the bundle gets a "what changed in the canonical and why" section built
        # from the recaps for the commits that touched the canonical since the
        # derived last followed. If it doesn't exist (or git-recap never ran),
        # the feature is silently off and the bundle is exactly as before.
        self.recaps_dir = recaps_dir
        self._mapspec: Optional[MapSpec] = None
        self._pairs: Optional[list[Pair]] = None
        self._state: Optional[dict] = None
        self._triage: Optional[list[TriageRow]] = None
        self._collection_problems: list[dict] = []
        # url -> (text, etag, ts, error); one fetch per URL per run.
        self._fetch_cache: dict[str, FetchResult] = {}

    def env_vars(self) -> dict[str, str]:
        return {
            "MAP_PATH": str(self.map_path),
            "STATE_PATH": str(self.state_path),
            "DRAFTS_DIR": str(self.drafts_dir),
            "DRIFT_DATE": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }

    # ---- map + state ---------------------------------------------

    def mapspec(self) -> MapSpec:
        if self._mapspec is None:
            self._mapspec = load_map(self.map_path)
        return self._mapspec

    def pairs(self) -> list[Pair]:
        """Static pairs plus every pair discovered by expanding collections.

        Expansion fetches a sitemap or index; a failure yields zero pairs for
        that collection and a recorded problem rather than crashing the run.
        """
        if self._pairs is not None:
            return self._pairs
        spec = self.mapspec()
        pairs = list(spec.pairs)
        self._collection_problems = []
        for coll in spec.collections:
            discovered, problem = self._expand_collection(coll)
            pairs.extend(discovered)
            if problem:
                self._collection_problems.append({"collection": coll.id, "problem": problem})
        self._pairs = pairs
        return pairs

    def _expand_collection(self, coll: Collection) -> tuple[list[Pair], Optional[str]]:
        # Filesystem discovery: one Pair per matching file, no network.
        if coll.glob:
            base = self.map_path.resolve().parent
            found, err = _discover_glob(coll.glob, base)
            if err:
                return [], err
            pairs = [
                Pair(
                    id=f"{coll.id}::{slug}",
                    canonical=coll.canonical,
                    derived=Source("file", path),
                    note=coll.note,
                    intentional=coll.intentional,
                )
                for path, slug in found
            ]
            return pairs, None

        # URL discovery: one Pair per matching link.
        if coll.sitemap:
            urls, err = _discover_sitemap(coll.sitemap, coll.match)
            source_url = coll.sitemap
        else:
            urls, err = _discover_index(coll.index, coll.match)
            source_url = coll.index
        if err:
            return [], err
        if not urls:
            return [], f"{source_url} matched no URLs for pattern {coll.match.pattern!r}"
        pairs = [
            Pair(
                id=f"{coll.id}::{_slug_from_url(u)}",
                canonical=coll.canonical,
                derived=Source("url", u),
                note=coll.note,
                intentional=coll.intentional,
            )
            for u in urls
        ]
        return pairs, None

    def state(self) -> dict:
        if self._state is None:
            self._state = _load_state(self.state_path)
        return self._state

    def triage(self) -> list[TriageRow]:
        if self._triage is not None:
            return self._triage
        state_pairs = self.state().get("pairs", {})
        rows: list[TriageRow] = []
        for pair in self.pairs():
            canon = _combine_canon([self._probe(s) for s in pair.canonical])
            deriv = self._probe(pair.derived)
            prior = state_pairs.get(pair.id)
            verdict, detail = _classify(pair, canon, deriv, prior)
            prior_status = prior.get("status") if isinstance(prior, dict) else None
            rows.append(TriageRow(pair, canon, deriv, verdict, detail, prior_status))
        self._triage = rows
        return rows

    def row_for(self, pair_id: str) -> Optional[TriageRow]:
        return next((r for r in self.triage() if r.pair.id == pair_id), None)

    def collection_problems(self) -> list[dict]:
        self.pairs()  # ensure expansion ran
        return list(self._collection_problems)

    # ---- probing with a per-run fetch cache ----------------------

    def _probe(self, source: Source) -> Rev:
        if not source.is_url:
            return doc_rev(Path(source.value))
        r = self._cached_fetch(source.value)
        if r.text is None:
            return Rev("", None, False, r.error or f"could not fetch {source.value}")
        token = (f"etag:{_clean_etag(r.etag)}" if r.etag
                 else "h:" + hashlib.sha256(r.text.encode("utf-8")).hexdigest()[:12])
        return Rev(token, r.last_modified_unix, True)

    def _cached_fetch(self, url: str) -> FetchResult:
        if url not in self._fetch_cache:
            self._fetch_cache[url] = fetch_url(url, strip=True)
        return self._fetch_cache[url]

    def _source_text(self, source: Source) -> str:
        if source.is_url:
            r = self._cached_fetch(source.value)
            if r.text is None:
                return f"(could not fetch {source.value}: {r.error})"
            return cap(r.text)
        return _read_capped(Path(source.value))

    # ---- review content loader (parameterless => env_method ok) --

    def drift_candidates(self) -> str:
        rows = self.triage()
        candidates = [r for r in rows if r.verdict == CANDIDATE]
        errors = [r for r in rows if r.verdict == ERROR]
        problems = self.collection_problems()
        chunks = [
            f"Declared/discovered pairs: {len(rows)}   "
            f"candidates: {len(candidates)}   errors: {len(errors)}   "
            f"collection problems: {len(problems)}",
            "",
        ]
        if candidates:
            chunks.append("== DRIFT CANDIDATES (the engine will draft a patch for each) ==")
            chunks.extend(self._render_row(r) for r in candidates)
        if errors:
            chunks.append("== MAP ERRORS (a source did not resolve) ==")
            chunks.extend(self._render_row(r) for r in errors)
        if problems:
            chunks.append("== COLLECTION PROBLEMS ==")
            chunks.extend(f"  {p['collection']}: {p['problem']}" for p in problems)
        quiet = [r for r in rows if r.verdict in (IN_SYNC, SKIP, SUPPRESSED)]
        if quiet:
            chunks.append(f"== QUIET: {len(quiet)} pair(s) in sync / skipped / suppressed ==")
        return "\n".join(chunks).rstrip()

    def _render_row(self, r: TriageRow) -> str:
        canon = "; ".join(f"[{s.kind}] {s.value}" for s in r.pair.canonical)
        return "\n".join([
            f"  PAIR {r.pair.id}",
            f"    verdict:   {r.verdict} — {r.detail}",
            f"    canonical: {canon}  (rev {r.canon.token or 'MISSING'})",
            f"    derived:   [{r.pair.derived.kind}] {r.pair.derived.value}  (rev {r.deriv.token or 'MISSING'})",
            "",
        ])

    # ---- the context bundle the body reads via env_file ----------

    def write_context_bundle(self, row: TriageRow) -> Path:
        """Assemble canonical(s) + derived + note into one file for the body.

        This is where URL content is fetched and pasted, so the body recipe (a
        model call that cannot fetch) sees the live page. The bundle also lets
        the operator inspect exactly what the model was shown.
        """
        parts: list[str] = []
        for src in row.pair.canonical:
            parts.append(f"== CANONICAL (source of truth): {src.value} ==")
            parts.append(self._source_text(src))
            parts.append("")
        parts.append(f"== DERIVED (the view that may have fallen behind): {row.pair.derived.value} ==")
        parts.append(self._source_text(row.pair.derived))
        parts.append("")
        parts.append("== RELATIONSHIP NOTE (what 'in sync' means for this pair) ==")
        parts.append(row.pair.note or "(none provided)")
        recap = self._recap_section(row)
        if recap:
            parts.append("")
            parts.append(recap)
        path = self._bundle_path(row.pair.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(parts), encoding="utf-8")
        return path

    def _bundle_path(self, pair_id: str) -> Path:
        return self.drafts_dir / ".context" / f"{_safe_filename(pair_id)}.md"

    def draft_path(self, pair_id: str) -> Path:
        return self.drafts_dir / f"{_safe_filename(pair_id)}.patch.md"

    # ---- optional: what-changed context from git-recap -----------

    def _recap_section(self, row: TriageRow) -> str:
        """A "what changed in the canonical, and why" block, or "".

        Nice-if-it-exists: returns "" (no section at all) when there's no
        recaps dir on disk, no temporal anchor on the derived side, no
        file canonical, or no recap that matches a commit in range. The
        bundle is then exactly what it was before this feature existed.

        When recaps ARE on disk, it pastes the git-recap summaries for the
        commits that touched the canonical AFTER the derived view last moved,
        so the body can focus its patch on the real change instead of
        re-diffing two whole documents in its head.
        """
        if self.recaps_dir is None or not self.recaps_dir.is_dir():
            return ""
        if row.deriv.ts is None:
            return ""  # no "since when" to bound the canonical's changes
        index = _index_recaps(self.recaps_dir)
        if not index:
            return ""
        seen: set[Path] = set()
        blocks: list[str] = []
        for src in row.pair.canonical:
            if src.is_url:
                continue
            for sha in _commits_touching(Path(src.value), row.deriv.ts):
                recap = _recap_for_sha(sha, index)
                if recap is not None and recap not in seen:
                    seen.add(recap)
                    blocks.append(f"--- {recap.name} ---\n{_read_capped(recap)}")
        if not blocks:
            return ""
        header = (
            "== WHAT CHANGED IN THE CANONICAL SINCE THE DERIVED LAST FOLLOWED ==\n"
            "(Operator commit recaps for the canonical changes that landed after\n"
            "the derived view last moved. Use them to focus the patch on what\n"
            "actually changed and why; they do not override the canonical text\n"
            "above, which is still the source of truth.)"
        )
        return "\n".join([header, ""] + blocks)

    # ---- state: the cross-run memory -----------------------------

    def write_state(self, outcomes: dict[str, str]) -> Path:
        rows = self.triage()
        pairs_state: dict[str, dict] = {}
        map_health: list[dict] = list(self.collection_problems())
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for r in rows:
            if r.pair.id in outcomes:
                status = outcomes[r.pair.id]
            elif r.verdict == SKIP:
                status = r.prior_status or "in-sync"
            elif r.verdict == ERROR:
                status = "error"
                map_health.append({"pair_id": r.pair.id, "problem": r.detail})
            elif r.verdict == SUPPRESSED:
                status = "suppressed"
            elif r.verdict == IN_SYNC:
                status = "in-sync"
            else:
                status = "candidate"  # never handled (body errored) — recheck next run
            pairs_state[r.pair.id] = {
                "canon_token": r.canon.token,
                "deriv_token": r.deriv.token,
                "status": status,
                "checked_at": now,
            }
        new_state = {"version": 1, "checked_at": now, "pairs": pairs_state, "map_health": map_health}
        _save_state(self.state_path, new_state)
        self._state = new_state
        return self.state_path


# ---- discovery ---------------------------------------------------


def _discover_glob(pattern: str, base: Path) -> tuple[list[tuple[str, str]], Optional[str]]:
    """Filesystem discovery. Returns [(absolute_path, slug), ...] or a problem.

    The pattern resolves relative to the map's directory unless absolute, and
    supports ** via recursive glob. The slug is the match's path relative to the
    fixed (non-wildcard) part of the pattern, so ids stay short and stable
    (../site/docs/**/*.md -> docs slugs like "guide/pricing", not the full path).
    """
    pat = pattern if os.path.isabs(pattern) else os.path.join(str(base.resolve()), pattern)
    matches = sorted(m for m in globmod.glob(pat, recursive=True) if os.path.isfile(m))
    if not matches:
        return [], f"glob {pattern!r} matched no files"
    root = _glob_root(pat)
    return [(str(Path(m).resolve()), _slug_for_file(m, root)) for m in matches], None


def _glob_root(pattern: str) -> str:
    """The longest leading run of non-wildcard path segments in a glob pattern."""
    fixed: list[str] = []
    for seg in pattern.split(os.sep):
        if any(c in seg for c in "*?["):
            break
        fixed.append(seg)
    return os.sep.join(fixed) or os.sep


def _slug_for_file(path: str, root: str) -> str:
    rel = os.path.relpath(path, root)
    rel = os.path.splitext(rel)[0]
    return _safe_filename(rel.replace(os.sep, "-"))


def _discover_sitemap(sitemap_url: str, match: re.Pattern) -> tuple[list[str], Optional[str]]:
    r = fetch_url(sitemap_url, strip=False)
    if r.text is None:
        return [], r.error
    urls = _filter_urls(_LOC_RE.findall(r.text), match, sitemap_url)
    return urls, None


def _discover_index(index_url: str, match: re.Pattern) -> tuple[list[str], Optional[str]]:
    r = fetch_url(index_url, strip=False)
    if r.text is None:
        return [], r.error
    absolute = [urljoin(index_url, h) for h in _HREF_RE.findall(r.text)]
    return _filter_urls(absolute, match, index_url), None


def _filter_urls(candidates: list[str], match: re.Pattern, exclude_self: str) -> list[str]:
    self_norm = exclude_self.rstrip("/")
    out: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        url = raw.strip()
        if not _URL_RE.match(url):
            continue
        if url.rstrip("/") == self_norm:
            continue
        if not match.search(urlparse(url).path):
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
    return sorted(out)


# ---- state io + helpers ------------------------------------------


def _empty_state() -> dict:
    return {"version": 1, "pairs": {}, "map_health": []}


def _load_state(path: Path) -> dict:
    return load_state(path, _empty_state())


def _safe_filename(text: str) -> str:
    return safe_filename(text, fallback="pair")


def _read_capped(path: Path) -> str:
    try:
        return cap(path.read_text(encoding="utf-8", errors="replace"))
    except OSError as e:
        return f"(could not read {path}: {e})"


# ---- git-recap bridge: match commits to their recap files --------

# git-recap names per-commit files <stamp>-<slug>-<sha8>.md; the trailing
# hex run is a commit-sha prefix. The weekly/ rollups carry no sha and are
# skipped (top-level glob only). 7-40 hex tolerates 7- or 8-char prefixes.
_RECAP_SHA_RE = re.compile(r"-([0-9a-f]{7,40})$", re.IGNORECASE)


def _index_recaps(recaps_dir: Path) -> dict[str, Path]:
    """Map sha-prefix token -> recap file for git-recap's per-commit summaries.

    Returns {} if the dir is absent. Scans only the top level, so weekly
    rollups under weekly/ (which have no sha) are ignored.
    """
    if not recaps_dir.is_dir():
        return {}
    out: dict[str, Path] = {}
    for p in sorted(recaps_dir.glob("*.md")):
        m = _RECAP_SHA_RE.search(p.stem)
        if m:
            out[m.group(1).lower()] = p
    return out


def _recap_for_sha(sha: str, index: dict[str, Path]) -> Optional[Path]:
    """The recap file whose sha-prefix token leads `sha`, or None."""
    sha = sha.lower()
    for token, path in index.items():
        if sha.startswith(token):
            return path
    return None


def _commits_touching(path: Path, since_unix: int) -> list[str]:
    """Full SHAs of commits that touched `path` after `since_unix`.

    Empty if the path isn't readable, isn't git-tracked, or nothing landed.
    The bound is the derived view's own commit time, so this is exactly the
    canonical's drift delta. Never raises: a non-repo path just yields [].
    """
    if not path.exists():
        return []
    since_iso = datetime.fromtimestamp(since_unix, timezone.utc).isoformat()
    cmd = [
        "git", "-C", str(path.parent), "log",
        f"--since={since_iso}", "--format=%H", "--", str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


_DRIFT_NONE_RE = re.compile(r"drift\s*=\s*none", re.IGNORECASE)


def _draft_outcome(path: Path) -> Optional[str]:
    """The body's verdict, read off the draft's first-line marker.

    `drift=none` => false positive (record in-sync). Anything else (or no
    marker) => a real drafted patch awaiting a seal. None => no usable draft.
    """
    if not path.exists() or path.stat().st_size == 0:
        return None
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        head = f.read(400)
    return "in-sync" if _DRIFT_NONE_RE.search(head) else "drafted"


# ---- the engine --------------------------------------------------


class DocDriftEngine(Engine):
    """Drafts patches for derived views that fell behind their canonical source.

    Routing is deterministic: pipeline() triages, then builds one draft-doc-patch
    body phase per candidate (no model router). The engine carries no per-run
    state itself; everything lives on the environment threaded through ctx.
    """

    @property
    def name(self) -> str:
        return "doc-drift"

    def recipes_dir(self) -> str:
        return str(_HERE / "recipes")

    def precheck(self, ctx: Context) -> None:
        env = ctx.environment
        if not isinstance(env, DocDriftEnvironment):
            raise RuntimeError("doc-drift: environment must be a DocDriftEnvironment instance.")
        if not env.map_path.exists():
            raise RuntimeError(_MISSING_MAP_HELP.format(path=env.map_path))
        try:
            spec = env.mapspec()
        except (tomllib.TOMLDecodeError, ValueError) as e:
            raise RuntimeError(f"doc-drift: could not load the map at {env.map_path}:\n  {e}")
        if not spec.pairs and not spec.collections:
            raise RuntimeError(
                f"doc-drift: the map at {env.map_path} declares no [[pair]] or "
                f"[[collection]] entries. Add at least one."
            )
        body_recipe = Path(self.recipes_dir()) / "draft-doc-patch.yaml"
        if not body_recipe.exists():
            raise RuntimeError(
                f"doc-drift: body recipe missing: {body_recipe}\n"
                f"The engine routes drift candidates to this file; create it."
            )

    def pipeline(self, ctx: Context) -> Pipeline:
        recipes = _HERE / "recipes"
        env = ctx.environment
        body: list[Phase] = []
        if isinstance(env, DocDriftEnvironment):
            for row in env.triage():
                if row.verdict != CANDIDATE:
                    continue
                bundle = env.write_context_bundle(row)
                draft = env.draft_path(row.pair.id)
                build_env = {
                    "PAIR_ID": row.pair.id,
                    "CONTEXT_FILE": str(bundle),
                    "OUTPUT_PATH": str(draft),
                }
                body.append(Phase(
                    name=f"draft:{row.pair.id}",
                    recipe_path=str(recipes / "draft-doc-patch.yaml"),
                    build_env=(lambda _c, e=build_env: dict(e)),
                    success_predicate=predicates.file_nonempty(draft),
                    post_process=(lambda _o, c, p=draft: c.record_output(p)),
                    label=f"draft:{row.pair.id}",
                ))
        return Pipeline(
            review=Phase(name="review", recipe_path=str(recipes / "review.example.yaml")),
            body=body,
            summary=Phase(
                name="summary",
                recipe_path=str(recipes / "summary.example.yaml"),
                post_process=_persist_state,
            ),
        )


def _persist_state(_stdout: str, ctx: Context) -> None:
    """Summary post_process: raise the operator actions AND write the memory.

    Runs after the body, so the real verdicts are known. A "seal this draft"
    action is raised only for a candidate the body confirmed drifted (drift=yes)
    — never for a false positive, never as a pre-body guess. Unresolved sources
    (map errors, failed collection crawls) become fix-the-map actions.
    """
    env = ctx.environment
    if not isinstance(env, DocDriftEnvironment):
        return
    outcomes: dict[str, str] = {}
    for row in env.triage():
        if row.verdict != CANDIDATE:
            continue
        outcome = _draft_outcome(env.draft_path(row.pair.id))
        if outcome is None:
            continue
        outcomes[row.pair.id] = outcome
        if outcome == "drafted":
            note = f" {row.pair.note}" if row.pair.note else ""
            ctx.add_operator_action(
                f"Review and seal the doc-drift draft for {row.pair.id}",
                why=(f"the derived view drifted from its canonical; a patch draft "
                     f"is waiting at {env.draft_path(row.pair.id)}.{note}"),
            )
    for row in env.triage():
        if row.verdict == ERROR:
            ctx.add_operator_action(
                f"Fix doc-map entry {row.pair.id}: {row.detail}",
                why="doc-drift can't check a pair whose sources don't resolve.",
            )
    for problem in env.collection_problems():
        ctx.add_operator_action(
            f"Fix doc-map collection {problem['collection']}: {problem['problem']}",
            why="the collection didn't expand, so its pages went unchecked this run.",
        )
    path = env.write_state(outcomes)
    ctx.session_log(
        f"doc-drift: state for {len(env.triage())} pairs -> {path.name} "
        f"({sum(v == 'drafted' for v in outcomes.values())} drafted)"
    )


_MISSING_MAP_HELP = (
    "doc-drift: no map found at {path}\n"
    "\n"
    "Copy the committed template and edit it:\n"
    "\n"
    "    cp doc-map.example.toml doc-map.toml\n"
    "\n"
    "Your doc-map.toml is gitignored (it points at your own repos and URLs);\n"
    "doc-map.example.toml is the committed template with the full schema. The\n"
    "path the engine reads is set by [doc_drift] map = ... in gooseloop.toml.\n"
    "\n"
    "A [[pair]] checks one source against one view; a [[collection]] discovers\n"
    "many views from a sitemap or index (e.g. every post on a blog) and checks\n"
    "each. File paths resolve relative to the map's directory; http(s) URLs are\n"
    "fetched live."
)
