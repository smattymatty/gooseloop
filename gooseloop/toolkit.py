"""Engine toolkit: the helpers every real engine ended up writing.

Extracted from three independent copies (doc_drift, and the site-drift /
pain-harvest / site-pitch engines) once the duplication proved the need.
Everything here is stdlib-only and framework-agnostic: an engine can use any
of it without buying into anything else.

What lives here and why it earned the spot:

    Source / parse_source(s)   a doc, feed, or reference is a file path or a
                               live URL; every engine that reads inputs from
                               config re-invented this two-arm shape.
    FetchResult / fetch_url    hardened urllib fetch with HTML-to-text. The
                               named-field result exists because doc_drift
                               needed ETag/Last-Modified for revision probing
                               and had to fork the (text, error) tuple.
    html_to_text               minimal dependency-free HTML normalizer.
    cap                        paste budget with a visible truncation marker.
                               The marker is the load-bearing part: a silent
                               cap reads as "pasted everything" when it didn't.
    template_safe / ZWSP       goose renders assembled recipes through
                               minijinja; any surviving delimiter in pasted
                               reference text aborts the run. One ZWSP trick,
                               one home (context_prepend shares this constant).
    safe_filename/unique_slug  model-emitted routing params become file paths
                               via BranchPolicy.output_path. The model is
                               untrusted input: slashes and leading dots are
                               stripped so a hallucinated slug can never
                               resolve outside the output dir.
    load_state / save_state    cross-run JSON state with corrupt-file recovery
                               and defaults backfill.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Optional

DEFAULT_PASTE_CAP = 60_000
_HTTP_TIMEOUT = 15
_HTTP_MAX_BYTES = 5_000_000
_DEFAULT_USER_AGENT = "gooseloop/1.0 (+https://github.com/smattymatty/gooseloop)"
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


# ---- sources: a file path or a live URL --------------------------


@dataclass(frozen=True)
class Source:
    kind: str   # "file" | "url"
    value: str  # absolute path for files; the URL string for urls

    @property
    def is_url(self) -> bool:
        return self.kind == "url"


def parse_source(raw: str, base: Path) -> Source:
    raw = raw.strip()
    if _URL_RE.match(raw):
        return Source("url", raw)
    path = Path(raw).expanduser()
    resolved = path if path.is_absolute() else (base / path).resolve()
    return Source("file", str(resolved))


def parse_sources(raw: Any, base: Path) -> tuple[Source, ...]:
    """A single source, a list of them, or None. Returns a tuple of Sources."""
    if raw is None:
        return ()
    items = raw if isinstance(raw, list) else [raw]
    return tuple(parse_source(str(x), base) for x in items if str(x).strip())


# ---- http fetch + html->text (dependency-free) -------------------


@dataclass(frozen=True)
class FetchResult:
    """Outcome of one fetch_url call. Exactly one of text/error is set.

    etag and last_modified_unix carry the response headers when the server
    sent them; revision-probing engines build change tokens from these.
    """
    text: Optional[str]
    error: Optional[str]
    etag: Optional[str] = None
    last_modified_unix: Optional[int] = None

    @property
    def ok(self) -> bool:
        return self.error is None


def fetch_url(
    url: str,
    *,
    strip: bool = True,
    timeout: int = _HTTP_TIMEOUT,
    max_bytes: int = _HTTP_MAX_BYTES,
    user_agent: str = _DEFAULT_USER_AGENT,
) -> FetchResult:
    """Fetch a URL. strip=True normalizes HTML to visible text (stable for
    hashing and readable for the model); strip=False returns the body as-is
    (for JSON / sitemap XML / href scraping). Any network or HTTP error comes
    back as FetchResult(error=reason), never an exception."""
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(max_bytes)
            ctype = resp.headers.get("Content-Type", "")
            etag = resp.headers.get("ETag")
            last_mod = resp.headers.get("Last-Modified")
    except urllib.error.HTTPError as e:
        return FetchResult(None, f"HTTP {e.code} fetching {url}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        return FetchResult(None, f"could not fetch {url}: {e}")

    text = raw.decode("utf-8", errors="replace")
    if strip and ("html" in ctype.lower() or _looks_like_html(text)):
        text = html_to_text(text)
    return FetchResult(text, None, etag, _http_date_to_unix(last_mod))


def url_resolves(url: str) -> bool:
    """True if the URL fetches without a network/HTTP error."""
    return fetch_url(url, strip=False).ok


_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\f\v]+")
_BLANKLINES_RE = re.compile(r"\n{3,}")


def _looks_like_html(text: str) -> bool:
    head = text[:512].lower()
    return "<html" in head or "<!doctype html" in head


def html_to_text(html: str) -> str:
    """Minimal, dependency-free HTML->text: drop script/style, strip tags, tidy."""
    import html as _html
    text = _SCRIPT_STYLE_RE.sub(" ", html)
    text = _TAG_RE.sub("\n", text)
    text = _html.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _BLANKLINES_RE.sub("\n\n", text).strip()


def _http_date_to_unix(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return int(dt.timestamp()) if dt is not None else None


def cap(text: str, limit: int = DEFAULT_PASTE_CAP) -> str:
    """Bound pasted content. The truncation marker is deliberate: a capped
    paste must say so, or the model (and the operator reading the session)
    believes it saw everything."""
    if len(text) > limit:
        return f"{text[:limit]}\n\n(... truncated at {limit} chars ...)"
    return text


# ---- minijinja delimiter neutralizer -----------------------------

# goose renders the whole assembled recipe through minijinja. Any delimiter
# that survives in inlined reference text is parsed as a tag and aborts the
# run. Splitting each delimiter with a zero-width space keeps the text
# visually identical while goose no longer recognises a tag.
ZWSP = "​"
_TEMPLATE_DELIMS = ("{{", "}}", "{%", "%}", "{#", "#}")


def template_safe(text: str) -> str:
    for d in _TEMPLATE_DELIMS:
        while d in text:
            text = text.replace(d, d[0] + ZWSP + d[1])
    return text


# ---- filename + slug safety --------------------------------------

_SAFE_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def safe_filename(text: str, fallback: str = "item") -> str:
    """Reduce untrusted text (a model-emitted routing slug, a URL fragment) to
    a single safe path component. Separators collapse to '-'; leading dots are
    stripped so '..' and dotfiles cannot come back out. Never returns empty:
    a fully-consumed input yields `fallback`."""
    cleaned = _SAFE_RE.sub("-", text).strip("-").lstrip(".-")
    return cleaned or fallback


def unique_slug(raw: str, seen: set[str]) -> str:
    slug = safe_filename(raw)
    if slug not in seen:
        return slug
    i = 2
    while f"{slug}-{i}" in seen:
        i += 1
    return f"{slug}-{i}"


# ---- json state io -----------------------------------------------


def load_state(path: Path, defaults: dict[str, Any]) -> dict[str, Any]:
    """Read a JSON state file, falling back to a copy of `defaults` on a missing
    or corrupt file. Missing keys in a present file are backfilled from defaults."""
    if not path.exists():
        return dict(defaults)
    try:
        with open(path, "rb") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return dict(defaults)
    if not isinstance(data, dict):
        return dict(defaults)
    for k, v in defaults.items():
        data.setdefault(k, v)
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")
