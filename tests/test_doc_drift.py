"""doc-drift engine tests.

The deterministic core (map parsing, revision detection, the classify decision
table, multi-canonical folding, discovery/filtering, the state memory, the
context bundle, the deterministic pipeline) is tested directly. A few tests
build a real git repo for doc_rev() and fake urlopen for the URL/collection
paths. The model recipes are integration surface, exercised only by live runs.
"""

import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from engines.doc_drift import DocDriftEngine, DocDriftEnvironment
from engines.doc_drift.engine import (
    CANDIDATE,
    ERROR,
    IN_SYNC,
    SKIP,
    SUPPRESSED,
    Collection,
    Pair,
    Rev,
    Source,
    _classify,
    _combine_canon,
    _discover_sitemap,
    _draft_outcome,
    _filter_urls,
    _persist_state,
    _safe_filename,
    doc_rev,
    fetch_url,
    html_to_text,
    load_map,
    parse_canonical,
    parse_source,
    probe_url,
)
from gooseloop.phase import Context

import re as _re


# ---- helpers -----------------------------------------------------

def _ctx(env) -> Context:
    return Context(model="t", session_dir=None, base_env={}, environment=env)


def _make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@e.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "T"], check=True)
    return path


def _commit(repo: Path, rel: str, content: str, when: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    subprocess.run(["git", "-C", str(repo), "add", rel], check=True)
    env = {**os.environ, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", f"touch {rel}"], check=True, env=env)


def _write(path: Path, body: str) -> Path:
    path.write_text(body)
    return path


def _env(tmp_path: Path, map_body: str) -> DocDriftEnvironment:
    return DocDriftEnvironment(
        map_path=_write(tmp_path / "doc-map.toml", map_body),
        state_path=tmp_path / "state.json",
        drafts_dir=tmp_path / "drafts",
    )


class _FakeResp:
    def __init__(self, body: bytes, headers: dict):
        self._body, self.headers = body, headers

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self, n=None): return self._body


def _patch_urls(monkeypatch, mapping: dict):
    """mapping: url -> (body_bytes, headers_dict). Unknown urls raise URLError."""
    def fake(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        if url in mapping:
            body, headers = mapping[url]
            return _FakeResp(body, headers)
        raise urllib.error.URLError(f"no fake for {url}")
    monkeypatch.setattr(urllib.request, "urlopen", fake)


def _pair(**kw):
    base = dict(id="p", canonical=(Source("file", "/c"),), derived=Source("file", "/d"),
                note="", intentional=False)
    base.update(kw)
    return Pair(**base)


# ---- map loading -------------------------------------------------

def test_load_map_single_pair(tmp_path):
    spec = load_map(_write(tmp_path / "m.toml",
                           '[[pair]]\nid="a"\ncanonical="src.py"\nderived="doc.md"\nnote="n"\n'))
    assert len(spec.pairs) == 1 and not spec.collections
    p = spec.pairs[0]
    assert p.id == "a"
    assert p.canonical == (Source("file", str((tmp_path / "src.py").resolve())),)
    assert p.derived == Source("file", str((tmp_path / "doc.md").resolve()))


def test_load_map_multi_canonical(tmp_path):
    spec = load_map(_write(tmp_path / "m.toml",
                           '[[pair]]\nid="a"\ncanonical=["x.py","y.md"]\nderived="d.md"\n'))
    assert len(spec.pairs[0].canonical) == 2


def test_load_map_derived_list_expands(tmp_path):
    spec = load_map(_write(tmp_path / "m.toml",
                           '[[pair]]\nid="a"\ncanonical="s"\nderived=["one.md","two.md"]\n'))
    assert [p.id for p in spec.pairs] == ["a::one.md", "a::two.md"]


def test_load_map_url_source(tmp_path):
    spec = load_map(_write(tmp_path / "m.toml",
                           '[[pair]]\nid="a"\ncanonical="s"\nderived="https://x.com/p"\n'))
    assert spec.pairs[0].derived == Source("url", "https://x.com/p")


def test_load_map_collection(tmp_path):
    spec = load_map(_write(tmp_path / "m.toml",
                           '[[collection]]\nid="blog"\ncanonical="t.md"\n'
                           'sitemap="https://x.com/sitemap.xml"\nmatch="^/blog/.+"\n'))
    assert len(spec.collections) == 1
    c = spec.collections[0]
    assert c.id == "blog" and c.sitemap.endswith("sitemap.xml")
    assert c.match.search("/blog/post/") and not c.match.search("/guide/")


def test_load_map_pair_missing_field_raises(tmp_path):
    with pytest.raises(ValueError):
        load_map(_write(tmp_path / "m.toml", '[[pair]]\nid="a"\ncanonical="s"\n'))


def test_load_map_collection_missing_discovery_raises(tmp_path):
    with pytest.raises(ValueError):
        load_map(_write(tmp_path / "m.toml", '[[collection]]\nid="b"\ncanonical="t"\n'))


# ---- revisions ---------------------------------------------------

def test_doc_rev_missing(tmp_path):
    assert doc_rev(tmp_path / "x").exists is False


def test_doc_rev_untracked_content_hash(tmp_path):
    p = tmp_path / "f.md"; p.write_text("hi")
    assert doc_rev(p).token.startswith("h:")


def test_doc_rev_git_sha(tmp_path):
    repo = _make_repo(tmp_path / "r")
    _commit(repo, "f.md", "x", "2026-01-01T00:00:00")
    rev = doc_rev(repo / "f.md")
    assert not rev.token.startswith("h:") and len(rev.token) == 12 and rev.ts > 0


# ---- combine canonical -------------------------------------------

def test_combine_canon_single():
    r = Rev("c", 5, True)
    assert _combine_canon([r]) is r


def test_combine_canon_multi_folds_token_and_max_ts():
    out = _combine_canon([Rev("a", 5, True), Rev("b", 9, True)])
    assert out.token.startswith("multi:") and out.ts == 9 and out.exists


def test_combine_canon_missing_propagates_error():
    out = _combine_canon([Rev("a", 5, True), Rev("", None, False, "gone")])
    assert out.exists is False and out.detail == "gone"


# ---- classify ----------------------------------------------------

def test_classify_error_missing_canonical():
    assert _classify(_pair(), Rev("", None, False, "x"), Rev("t", 5, True), None)[0] == ERROR


def test_classify_suppressed():
    assert _classify(_pair(intentional=True), Rev("c", 9, True), Rev("d", 1, True), None)[0] == SUPPRESSED


def test_classify_candidate_when_canon_newer():
    assert _classify(_pair(), Rev("c", 9, True), Rev("d", 1, True), None)[0] == CANDIDATE


def test_classify_in_sync_when_derived_caught_up():
    assert _classify(_pair(), Rev("c", 1, True), Rev("d", 9, True), None)[0] == IN_SYNC


def test_classify_skip_when_drafted_same_revs():
    prior = {"canon_token": "c", "deriv_token": "d", "status": "drafted"}
    assert _classify(_pair(), Rev("c", 9, True), Rev("d", 1, True), prior)[0] == SKIP


def test_classify_dismissal_resurfaces_on_change():
    prior = {"canon_token": "c", "deriv_token": "d", "status": "dismissed"}
    assert _classify(_pair(), Rev("c2", 9, True), Rev("d", 1, True), prior)[0] == CANDIDATE


def test_classify_url_first_run_candidate():
    pair = _pair(derived=Source("url", "https://x.com/p"))
    v, d = _classify(pair, Rev("c", 100, True), Rev("h:dd", None, True), None)
    assert v == CANDIDATE and "not yet verified" in d


def test_classify_url_in_sync_when_unchanged():
    pair = _pair(derived=Source("url", "https://x.com/p"))
    prior = {"canon_token": "c", "deriv_token": "h:dd", "status": "in-sync"}
    assert _classify(pair, Rev("c", 100, True), Rev("h:dd", None, True), prior)[0] == IN_SYNC


def test_classify_url_candidate_when_page_changed():
    pair = _pair(derived=Source("url", "https://x.com/p"))
    prior = {"canon_token": "c", "deriv_token": "h:OLD", "status": "in-sync"}
    assert _classify(pair, Rev("c", 100, True), Rev("h:NEW", None, True), prior)[0] == CANDIDATE


# ---- url fetch + html ---------------------------------------------

def test_parse_source_and_canonical(tmp_path):
    assert parse_source("https://x.com", tmp_path) == Source("url", "https://x.com")
    assert parse_canonical(["a.py", "b.md"], tmp_path) == (
        Source("file", str((tmp_path / "a.py").resolve())),
        Source("file", str((tmp_path / "b.md").resolve())),
    )


def test_html_to_text():
    t = html_to_text("<html><script>x</script><h1>Hi</h1><p>$5 &amp; up</p></html>")
    assert "Hi" in t and "$5 & up" in t and "<" not in t and "script" not in t


def test_probe_url_hash(monkeypatch):
    _patch_urls(monkeypatch, {"https://x.com/p": (b"<p>hi</p>", {"Content-Type": "text/html"})})
    assert probe_url("https://x.com/p").token.startswith("h:")


def test_probe_url_etag(monkeypatch):
    _patch_urls(monkeypatch, {"https://x.com/p": (b"x", {"Content-Type": "text/html", "ETag": '"abc"'})})
    assert probe_url("https://x.com/p").token == "etag:abc"


def test_probe_url_network_error(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda r, timeout=None: (_ for _ in ()).throw(urllib.error.URLError("x")))
    rev = probe_url("https://x.com/p")
    assert rev.exists is False and "could not fetch" in rev.detail


def test_fetch_url_no_strip_keeps_markup(monkeypatch):
    _patch_urls(monkeypatch, {"https://x.com/s.xml": (b"<loc>https://x.com/a</loc>", {"Content-Type": "application/xml"})})
    text, _, _, err = fetch_url("https://x.com/s.xml", strip=False)
    assert err is None and "<loc>" in text


# ---- discovery / filtering ---------------------------------------

def test_filter_urls_matches_excludes_self_dedups():
    cands = ["https://x.com/blog/", "https://x.com/blog/a/", "https://x.com/blog/a/",
             "https://x.com/guide/", "/relative"]
    out = _filter_urls(cands, _re.compile(r"^/blog/.+"), "https://x.com/blog/")
    assert out == ["https://x.com/blog/a/"]


def test_discover_sitemap(monkeypatch):
    xml = (b"<urlset><url><loc>https://x.com/blog/</loc></url>"
           b"<url><loc>https://x.com/blog/one/</loc></url>"
           b"<url><loc>https://x.com/guide/</loc></url></urlset>")
    _patch_urls(monkeypatch, {"https://x.com/sitemap.xml": (xml, {"Content-Type": "application/xml"})})
    urls, err = _discover_sitemap("https://x.com/sitemap.xml", _re.compile(r"^/blog/.+"))
    assert err is None and urls == ["https://x.com/blog/one/"]


def test_collection_expands_to_pairs(tmp_path, monkeypatch):
    xml = b"<urlset><url><loc>https://x.com/blog/one/</loc></url><url><loc>https://x.com/blog/two/</loc></url></urlset>"
    _patch_urls(monkeypatch, {"https://x.com/sitemap.xml": (xml, {"Content-Type": "application/xml"})})
    env = _env(tmp_path, '[[collection]]\nid="blog"\ncanonical="t.md"\n'
                         'sitemap="https://x.com/sitemap.xml"\nmatch="^/blog/.+"\n')
    ids = sorted(p.id for p in env.pairs())
    assert ids == ["blog::one", "blog::two"]
    assert all(p.derived.is_url for p in env.pairs())


def test_load_map_collection_glob(tmp_path):
    spec = load_map(_write(tmp_path / "m.toml",
                           '[[collection]]\nid="docs"\ncanonical="t.md"\nglob="docs/**/*.md"\n'))
    assert spec.collections[0].glob == "docs/**/*.md"


def test_collection_glob_expands_files(tmp_path):
    (tmp_path / "docs" / "sub").mkdir(parents=True)
    (tmp_path / "docs" / "a.md").write_text("a")
    (tmp_path / "docs" / "sub" / "b.md").write_text("b")
    (tmp_path / "docs" / "ignore.txt").write_text("x")
    env = _env(tmp_path, '[[collection]]\nid="docs"\ncanonical="t.md"\nglob="docs/**/*.md"\n')
    pairs = {p.id: p for p in env.pairs()}
    assert set(pairs) == {"docs::a", "docs::sub-b"}
    assert all(not p.derived.is_url for p in pairs.values())


def test_collection_glob_no_match_records_problem(tmp_path):
    env = _env(tmp_path, '[[collection]]\nid="docs"\ncanonical="t.md"\nglob="nope/**/*.md"\n')
    assert env.pairs() == []
    assert env.collection_problems()[0]["collection"] == "docs"


def test_collection_failure_records_problem(tmp_path, monkeypatch):
    _patch_urls(monkeypatch, {})  # any fetch fails
    env = _env(tmp_path, '[[collection]]\nid="blog"\ncanonical="t.md"\n'
                         'sitemap="https://x.com/sitemap.xml"\nmatch="^/blog/.+"\n')
    assert env.pairs() == []
    probs = env.collection_problems()
    assert probs and probs[0]["collection"] == "blog"


# ---- triage end to end -------------------------------------------

def _file_pair_env(tmp_path, repo):
    return _env(tmp_path, f'[[pair]]\nid="a"\ncanonical="{repo / "src.py"}"\nderived="{repo / "doc.md"}"\n')


def test_triage_candidate(tmp_path):
    repo = _make_repo(tmp_path / "r")
    _commit(repo, "doc.md", "old", "2026-01-01T00:00:00")
    _commit(repo, "src.py", "new", "2026-02-01T00:00:00")
    assert _file_pair_env(tmp_path, repo).row_for("a").verdict == CANDIDATE


def test_triage_in_sync(tmp_path):
    repo = _make_repo(tmp_path / "r")
    _commit(repo, "src.py", "new", "2026-01-01T00:00:00")
    _commit(repo, "doc.md", "after", "2026-02-01T00:00:00")
    assert _file_pair_env(tmp_path, repo).row_for("a").verdict == IN_SYNC


def test_triage_multi_canonical_url_derived(tmp_path, monkeypatch):
    canon = tmp_path / "p.py"; canon.write_text("PRICE=5")
    _patch_urls(monkeypatch, {"https://x.com/pricing": (b"<p>price 4</p>", {"Content-Type": "text/html"})})
    env = _env(tmp_path, f'[[pair]]\nid="a"\ncanonical=["{canon}"]\nderived="https://x.com/pricing"\n')
    row = env.row_for("a")
    assert row.verdict == CANDIDATE and row.pair.derived.is_url


# ---- context bundle ----------------------------------------------

def test_write_context_bundle_includes_both_sides(tmp_path, monkeypatch):
    canon = tmp_path / "p.py"; canon.write_text("PRICE=5")
    _patch_urls(monkeypatch, {"https://x.com/pricing": (b"<p>price is 4</p>", {"Content-Type": "text/html"})})
    env = _env(tmp_path, f'[[pair]]\nid="a"\ncanonical="{canon}"\nderived="https://x.com/pricing"\nnote="match prices"\n')
    bundle = env.write_context_bundle(env.row_for("a"))
    text = bundle.read_text()
    assert "PRICE=5" in text and "price is 4" in text and "match prices" in text
    assert bundle.exists()


def _sha(repo: Path, rel: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--format=%H", "--", rel],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def _recap_env(tmp_path, repo, recaps_dir):
    return DocDriftEnvironment(
        map_path=_write(
            tmp_path / "doc-map.toml",
            f'[[pair]]\nid="a"\ncanonical="{repo / "src.py"}"\nderived="{repo / "doc.md"}"\n',
        ),
        state_path=tmp_path / "state.json",
        drafts_dir=tmp_path / "drafts",
        recaps_dir=recaps_dir,
    )


def test_bundle_includes_recap_when_present(tmp_path):
    repo = _make_repo(tmp_path / "r")
    _commit(repo, "doc.md", "old", "2026-01-01T00:00:00")
    _commit(repo, "src.py", "PRICE=9", "2026-02-01T00:00:00")
    sha = _sha(repo, "src.py")
    recaps = tmp_path / "recaps"; recaps.mkdir()
    (recaps / f"20260201-000000-bump-price-{sha[:8]}.md").write_text("RECAP: bumped the price to 9")
    (recaps / "weekly").mkdir()
    (recaps / "weekly" / "weekly-2026-02-01.md").write_text("weekly rollup, no sha")
    text = _recap_env(tmp_path, repo, recaps).write_context_bundle(_recap_env(tmp_path, repo, recaps).row_for("a")).read_text()
    assert "WHAT CHANGED IN THE CANONICAL" in text
    assert "RECAP: bumped the price to 9" in text
    assert "weekly rollup" not in text  # weekly/ rollups have no sha, skipped


def test_bundle_omits_recap_when_dir_absent(tmp_path):
    repo = _make_repo(tmp_path / "r")
    _commit(repo, "doc.md", "old", "2026-01-01T00:00:00")
    _commit(repo, "src.py", "PRICE=9", "2026-02-01T00:00:00")
    text = _recap_env(tmp_path, repo, tmp_path / "nope").write_context_bundle(
        _recap_env(tmp_path, repo, tmp_path / "nope").row_for("a")
    ).read_text()
    assert "WHAT CHANGED IN THE CANONICAL" not in text


def test_bundle_omits_recap_when_no_match(tmp_path):
    repo = _make_repo(tmp_path / "r")
    _commit(repo, "doc.md", "old", "2026-01-01T00:00:00")
    _commit(repo, "src.py", "PRICE=9", "2026-02-01T00:00:00")
    recaps = tmp_path / "recaps"; recaps.mkdir()
    (recaps / "20260201-000000-unrelated-deadbeef.md").write_text("RECAP: a different commit")
    text = _recap_env(tmp_path, repo, recaps).write_context_bundle(
        _recap_env(tmp_path, repo, recaps).row_for("a")
    ).read_text()
    assert "WHAT CHANGED IN THE CANONICAL" not in text
    assert "a different commit" not in text


# ---- state -------------------------------------------------------

def test_write_state_drafted(tmp_path):
    repo = _make_repo(tmp_path / "r")
    _commit(repo, "doc.md", "old", "2026-01-01T00:00:00")
    _commit(repo, "src.py", "new", "2026-02-01T00:00:00")
    env = _file_pair_env(tmp_path, repo)
    env.write_state({"a": "drafted"})
    assert json.loads(env.state_path.read_text())["pairs"]["a"]["status"] == "drafted"


def test_state_skip_loop(tmp_path):
    repo = _make_repo(tmp_path / "r")
    _commit(repo, "doc.md", "old", "2026-01-01T00:00:00")
    _commit(repo, "src.py", "new", "2026-02-01T00:00:00")
    _file_pair_env(tmp_path, repo).write_state({"a": "drafted"})
    assert _file_pair_env(tmp_path, repo).row_for("a").verdict == SKIP
    _commit(repo, "src.py", "newer", "2026-03-01T00:00:00")
    assert _file_pair_env(tmp_path, repo).row_for("a").verdict == CANDIDATE


def test_false_positive_records_in_sync(tmp_path):
    repo = _make_repo(tmp_path / "r")
    _commit(repo, "doc.md", "old", "2026-01-01T00:00:00")
    _commit(repo, "src.py", "new", "2026-02-01T00:00:00")
    _file_pair_env(tmp_path, repo).write_state({"a": "in-sync"})
    assert _file_pair_env(tmp_path, repo).row_for("a").verdict == IN_SYNC


def test_write_state_error_to_map_health(tmp_path):
    env = _env(tmp_path, '[[pair]]\nid="a"\ncanonical="/no/such"\nderived="/also/no"\n')
    env.write_state({})
    assert json.loads(env.state_path.read_text())["map_health"][0]["pair_id"] == "a"


def test_corrupt_state_recovers(tmp_path):
    env = _env(tmp_path, '[[pair]]\nid="a"\ncanonical="s"\nderived="d"\n')
    env.state_path.write_text("{bad")
    assert env.state() == {"version": 1, "pairs": {}, "map_health": []}


# ---- draft outcome ------------------------------------------------

def test_draft_outcome(tmp_path):
    yes = _write(tmp_path / "y.md", "<!-- doc-drift: drift=yes -->\n# x\n")
    none = _write(tmp_path / "n.md", "<!-- doc-drift: drift=none -->\n# y\n")
    bare = _write(tmp_path / "b.md", "# no marker\n")
    assert _draft_outcome(yes) == "drafted"
    assert _draft_outcome(none) == "in-sync"
    assert _draft_outcome(bare) == "drafted"
    assert _draft_outcome(tmp_path / "missing.md") is None


# ---- engine: precheck + deterministic pipeline -------------------

def _engine_and_env(tmp_path, body):
    env = _env(tmp_path, body)
    return DocDriftEngine(), env


def test_precheck_missing_map_guides(tmp_path):
    env = DocDriftEnvironment(tmp_path / "nope.toml", tmp_path / "s.json", tmp_path / "d")
    with pytest.raises(RuntimeError) as e:
        DocDriftEngine().precheck(_ctx(env))
    assert "cp doc-map.example.toml" in str(e.value)


def test_precheck_empty_map(tmp_path):
    eng, env = _engine_and_env(tmp_path, "# nothing\n")
    with pytest.raises(RuntimeError) as e:
        eng.precheck(_ctx(env))
    assert "no [[pair]] or [[collection]]" in str(e.value)


def test_precheck_valid(tmp_path):
    eng, env = _engine_and_env(tmp_path, '[[pair]]\nid="a"\ncanonical="s"\nderived="d"\n')
    eng.precheck(_ctx(env))  # no raise


def test_precheck_body_recipe_exists():
    # The shipped engine's routed recipe must exist (regression for the
    # .example.yaml naming bug that crashed mid-run).
    assert (Path(DocDriftEngine().recipes_dir()) / "draft-doc-patch.yaml").exists()


def test_pipeline_builds_one_body_phase_per_candidate(tmp_path):
    repo = _make_repo(tmp_path / "r")
    _commit(repo, "doc.md", "old", "2026-01-01T00:00:00")
    _commit(repo, "src.py", "new", "2026-02-01T00:00:00")
    eng, env = _engine_and_env(
        tmp_path, f'[[pair]]\nid="a"\ncanonical="{repo / "src.py"}"\nderived="{repo / "doc.md"}"\n')
    pipe = eng.pipeline(_ctx(env))
    assert len(pipe.body) == 1
    phase = pipe.body[0]
    benv = phase.build_env(None)
    assert benv["PAIR_ID"] == "a"
    assert Path(benv["CONTEXT_FILE"]).exists()  # bundle written during pipeline()
    assert benv["OUTPUT_PATH"].endswith("a.patch.md")


def test_pipeline_skips_in_sync(tmp_path):
    repo = _make_repo(tmp_path / "r")
    _commit(repo, "src.py", "new", "2026-01-01T00:00:00")
    _commit(repo, "doc.md", "after", "2026-02-01T00:00:00")
    eng, env = _engine_and_env(
        tmp_path, f'[[pair]]\nid="a"\ncanonical="{repo / "src.py"}"\nderived="{repo / "doc.md"}"\n')
    assert eng.pipeline(_ctx(env)).body == []


# ---- engine: operator actions raised post-body -------------------

def test_persist_state_raises_action_only_for_confirmed_drift(tmp_path):
    repo = _make_repo(tmp_path / "r")
    _commit(repo, "doc.md", "old", "2026-01-01T00:00:00")
    _commit(repo, "src.py", "new", "2026-02-01T00:00:00")
    env = _file_pair_env(tmp_path, repo)
    ctx = _ctx(env)
    # Body wrote a real-drift draft.
    draft = env.draft_path("a"); draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("<!-- doc-drift: drift=yes -->\n# Drift patch: a\n")
    _persist_state("", ctx)
    actions = ctx.operator_actions
    assert any("seal the doc-drift draft for a" in a["action"] for a in actions)
    assert all(a["why"] for a in actions)


def test_persist_state_no_action_for_false_positive(tmp_path):
    repo = _make_repo(tmp_path / "r")
    _commit(repo, "doc.md", "old", "2026-01-01T00:00:00")
    _commit(repo, "src.py", "new", "2026-02-01T00:00:00")
    env = _file_pair_env(tmp_path, repo)
    ctx = _ctx(env)
    draft = env.draft_path("a"); draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("<!-- doc-drift: drift=none -->\n# In sync: a\n")
    _persist_state("", ctx)
    assert ctx.operator_actions == []
    assert json.loads(env.state_path.read_text())["pairs"]["a"]["status"] == "in-sync"


# ---- misc --------------------------------------------------------

def test_safe_filename():
    assert _safe_filename("blog::one-two") == "blog-one-two"
    assert _safe_filename("////") == "pair"


def test_env_vars_shape(tmp_path):
    env = _env(tmp_path, '[[pair]]\nid="a"\ncanonical="s"\nderived="d"\n')
    assert set(env.env_vars()) >= {"MAP_PATH", "STATE_PATH", "DRAFTS_DIR", "DRIFT_DATE"}
