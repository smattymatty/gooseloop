"""Tests for gooseloop.toolkit: the extracted engine helpers.

Ported from the tests that covered the original per-engine copies, plus the
hardening added at extraction time (safe_filename dot-stripping, FetchResult
header fields).
"""

import urllib.error
import urllib.request
from pathlib import Path

from gooseloop.toolkit import (
    ZWSP,
    FetchResult,
    Source,
    cap,
    fetch_url,
    html_to_text,
    load_state,
    parse_source,
    parse_sources,
    safe_filename,
    save_state,
    template_safe,
    unique_slug,
    url_resolves,
)


# ---- helpers -----------------------------------------------------


class _FakeResp:
    def __init__(self, body: bytes, headers: dict):
        self._body, self.headers = body, headers

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self, n=None): return self._body


def _patch_urls(monkeypatch, mapping: dict):
    def fake(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        if url in mapping:
            body, headers = mapping[url]
            return _FakeResp(body, headers)
        raise urllib.error.URLError(f"no fake for {url}")
    monkeypatch.setattr(urllib.request, "urlopen", fake)


class _Headers(dict):
    def get(self, key, default=None):
        return super().get(key, default)


# ---- sources -----------------------------------------------------


def test_parse_source_url_vs_file(tmp_path):
    assert parse_source("https://x.com", tmp_path) == Source("url", "https://x.com")
    assert parse_source("a.py", tmp_path) == Source("file", str((tmp_path / "a.py").resolve()))
    assert parse_source("https://x.com", tmp_path).is_url


def test_parse_sources_single_list_and_none(tmp_path):
    assert parse_sources(None, tmp_path) == ()
    assert parse_sources("https://x.com", tmp_path) == (Source("url", "https://x.com"),)
    many = parse_sources(["https://a.com", "  ", "b.md"], tmp_path)
    assert [s.kind for s in many] == ["url", "file"]


# ---- fetch + html ------------------------------------------------


def test_html_to_text():
    t = html_to_text("<html><script>x</script><h1>Hi</h1><p>$2 &amp; up</p></html>")
    assert "Hi" in t and "$2 & up" in t and "<" not in t and "script" not in t


def test_fetch_url_strips_html(monkeypatch):
    _patch_urls(monkeypatch, {"https://x.com/p": (b"<html><p>hi there</p></html>",
                                                  _Headers({"Content-Type": "text/html"}))})
    r = fetch_url("https://x.com/p")
    assert r.ok and "hi there" in r.text and "<" not in r.text


def test_fetch_url_json_passthrough(monkeypatch):
    _patch_urls(monkeypatch, {"https://api/x": (b'{"hits":[{"comment":"bill"}]}',
                                                _Headers({"Content-Type": "application/json"}))})
    r = fetch_url("https://api/x")
    assert r.ok and '"hits"' in r.text


def test_fetch_url_carries_revision_headers(monkeypatch):
    _patch_urls(monkeypatch, {"https://x.com/d": (b"<p>doc</p>", _Headers({
        "Content-Type": "text/html",
        "ETag": 'W/"abc123"',
        "Last-Modified": "Wed, 01 Jul 2026 10:00:00 GMT",
    }))})
    r = fetch_url("https://x.com/d")
    assert r.ok and r.etag == 'W/"abc123"'
    assert isinstance(r.last_modified_unix, int) and r.last_modified_unix > 0


def test_fetch_url_network_error(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda r, timeout=None: (_ for _ in ()).throw(urllib.error.URLError("x")))
    r = fetch_url("https://x.com/p")
    assert not r.ok and r.text is None and "could not fetch" in r.error
    assert url_resolves("https://x.com/p") is False


def test_fetch_result_ok_is_error_driven():
    assert FetchResult("", None).ok           # empty body is still a fetch
    assert not FetchResult(None, "boom").ok


def test_cap_truncates_with_marker():
    capped = cap("x" * 100, limit=50)
    assert "truncated at 50" in capped and capped.startswith("x" * 50)
    assert cap("short") == "short"


# ---- template_safe -----------------------------------------------


def test_template_safe_neutralizes_delimiters():
    out = template_safe("docker info -f '{{.SecurityOptions}}' and {% if x %}")
    assert "{{" not in out and "}}" not in out and "{%" not in out and "%}" not in out
    assert ".SecurityOptions" in out  # text preserved, only the delimiter split
    assert ZWSP in out


def test_template_safe_handles_comment_delims():
    out = template_safe("a {# note #} b")
    assert "{#" not in out and "#}" not in out and "note" in out


# ---- filename + slug safety --------------------------------------


def test_safe_filename_basic():
    assert safe_filename("guide::pricing/egress") == "guide-pricing-egress"


def test_safe_filename_refuses_traversal_and_dotfiles():
    # Regression 2026-07-12: model-emitted routing slugs become file paths via
    # BranchPolicy.output_path. The original helper let '..' through verbatim
    # (dots are in the allowed set), so a hallucinated slug of ".." resolved to
    # the output dir's parent. Leading dots are now stripped; never empty.
    assert safe_filename("..") == "item"
    assert safe_filename(".") == "item"
    assert safe_filename("../../etc/passwd") == "etc-passwd"
    assert safe_filename("..-secret") == "secret"
    assert safe_filename(".hidden") == "hidden"
    assert safe_filename("") == "item"
    assert "/" not in safe_filename("a/b/c")


def test_unique_slug():
    seen = {"a", "a-2"}
    assert unique_slug("a", seen) == "a-3"
    assert unique_slug("fresh", seen) == "fresh"


# ---- json state io -----------------------------------------------


def test_state_roundtrip_and_corrupt_recovery(tmp_path):
    p = tmp_path / "s.json"
    save_state(p, {"version": 1, "last_run": "now"})
    assert load_state(p, {"version": 1, "last_run": None})["last_run"] == "now"
    p.write_text("{bad")
    assert load_state(p, {"version": 1, "last_run": None})["last_run"] is None


def test_load_state_backfills_missing_keys(tmp_path):
    p = tmp_path / "s.json"
    save_state(p, {"a": 1})
    state = load_state(p, {"a": 0, "b": "default"})
    assert state == {"a": 1, "b": "default"}


def test_load_state_non_dict_falls_back(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("[1, 2, 3]")
    assert load_state(p, {"a": 1}) == {"a": 1}


def test_save_state_creates_parents(tmp_path):
    p = tmp_path / "deep" / "nested" / "s.json"
    save_state(p, {"ok": True})
    assert p.exists()
