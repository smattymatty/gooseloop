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
    _draft_touches,
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
    r = fetch_url("https://x.com/s.xml", strip=False)
    assert r.ok and "<loc>" in r.text


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


def _journal_env(tmp_path, repo, journal_dir):
    return DocDriftEnvironment(
        map_path=_write(
            tmp_path / "doc-map.toml",
            f'[[pair]]\nid="a"\ncanonical="{repo / "src.py"}"\nderived="{repo / "doc.md"}"\n',
        ),
        state_path=tmp_path / "state.json",
        drafts_dir=tmp_path / "drafts",
        journal_dir=journal_dir,
    )


def test_recent_journal_pastes_last_dailies_and_weeklies(tmp_path):
    """The declared composition (env_method:recent_journal on the draft
    recipe): last 5 dailies + last 2 weeklies, oldest first — visible as
    a wiring chip, deterministic, operator-removable."""
    repo = _make_repo(tmp_path / "r")
    _commit(repo, "doc.md", "old", "2026-01-01T00:00:00")
    _commit(repo, "src.py", "PRICE=9", "2026-02-01T00:00:00")
    journal = tmp_path / "journal"
    (journal / "daily").mkdir(parents=True)
    (journal / "weekly").mkdir(parents=True)
    for i in range(1, 8):  # 7 dailies; only the last 5 must paste
        (journal / "daily" / f"2026-02-0{i}.md").write_text(f"DAY {i}")
    (journal / "weekly" / "2026-W04.md").write_text("WEEK 4")
    (journal / "weekly" / "2026-W05.md").write_text("WEEK 5")
    (journal / "weekly" / "2026-W03.md").write_text("WEEK 3")
    out = _journal_env(tmp_path, repo, journal).recent_journal()
    assert "DAY 3" in out and "DAY 7" in out
    assert "DAY 1" not in out and "DAY 2" not in out  # only the last 5
    assert "WEEK 4" in out and "WEEK 5" in out
    assert "WEEK 3" not in out  # only the last 2 weeklies


def test_recent_journal_placeholder_when_absent(tmp_path):
    repo = _make_repo(tmp_path / "r")
    _commit(repo, "doc.md", "old", "2026-01-01T00:00:00")
    _commit(repo, "src.py", "PRICE=9", "2026-02-01T00:00:00")
    out = _journal_env(tmp_path, repo, tmp_path / "nope").recent_journal()
    assert "no journal" in out  # a render never fails on a missing journal


def test_bundle_no_longer_embeds_journal(tmp_path):
    """The buried bundle section is gone on purpose: the journal enters as
    a DECLARED context source, not code nobody can see in the wiring."""
    repo = _make_repo(tmp_path / "r")
    _commit(repo, "doc.md", "old", "2026-01-01T00:00:00")
    _commit(repo, "src.py", "PRICE=9", "2026-02-01T00:00:00")
    journal = tmp_path / "journal"
    (journal / "daily").mkdir(parents=True)
    (journal / "daily" / "2026-02-01.md").write_text("JOURNAL ENTRY")
    env = _journal_env(tmp_path, repo, journal)
    text = env.write_context_bundle(env.row_for("a")).read_text()
    assert "WHAT CHANGED IN THE CANONICAL" not in text
    assert "JOURNAL ENTRY" not in text


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


def test_injected_env_declaration_matches_what_phases_actually_get(tmp_path):
    """Verify, don't trust (the ADR 0011 stance, applied to the new
    Engine.injected_env declaration): every var doc_drift DECLARES as
    phase-injected must actually appear in the env its built body phases
    receive. A declaration that drifts from the build_env closure would
    make the dashboard's 'injected per phase' chip a lie."""
    from engines.doc_drift import DocDriftEngine
    from gooseloop.phase import Context

    repo = _make_repo(tmp_path / "r")
    _commit(repo, "doc.md", "old", "2026-01-01T00:00:00")
    _commit(repo, "src.py", "PRICE=9", "2026-02-01T00:00:00")
    env = _file_pair_env(tmp_path, repo)
    engine = DocDriftEngine()
    ctx = Context(model="m", session_dir=None, base_env={}, environment=env)

    pipeline = engine.pipeline(ctx)
    assert pipeline.body, "fixture must produce at least one draft phase"
    declared = set(engine.injected_env())
    assert declared, "doc_drift must declare its injected vars"
    for phase in pipeline.body:
        built = set(phase.build_env(ctx))
        missing = declared - built
        assert not missing, f"declared-but-never-injected: {missing}"


def test_draft_phase_raises_seal_action_the_moment_it_lands(tmp_path):
    """Caught live 2026-07-13: summary-time raising meant a drift=yes
    draft was invisible mid-run and a crashed pass lost its decisions.
    The draft phase's post now raises immediately; ctx dedup keeps the
    summary's re-raise from doubling the ledger."""
    from engines.doc_drift.engine import _record_and_raise
    from gooseloop.phase import Context

    repo = _make_repo(tmp_path / "r")
    _commit(repo, "doc.md", "old", "2026-01-01T00:00:00")
    _commit(repo, "src.py", "PRICE=9", "2026-02-01T00:00:00")
    env = _file_pair_env(tmp_path, repo)
    row = env.row_for("a")
    ctx = Context(model="m", session_dir=None, base_env={}, environment=env)

    draft = tmp_path / "drafts" / "a.patch.md"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("<!-- doc-drift: drift=yes -->\n# Drift patch: a\n")
    _record_and_raise(ctx, row, draft)
    actions = ctx.artifacts.get("operator_actions", [])
    assert len(actions) == 1
    assert "seal the doc-drift draft for a" in actions[0]["action"]

    # Raising twice (phase + summary belt-and-suspenders) stays single.
    _record_and_raise(ctx, row, draft)
    assert len(ctx.artifacts.get("operator_actions", [])) == 1

    # drift=none never raises.
    draft.write_text("<!-- doc-drift: drift=none -->\n# In sync: a\n")
    ctx2 = Context(model="m", session_dir=None, base_env={}, environment=env)
    _record_and_raise(ctx2, row, draft)
    assert ctx2.artifacts.get("operator_actions", []) == []


# ---- fix 2: the mtime shortcut is first-sight only ----------------

def test_classify_mtime_shortcut_only_on_first_sight():
    """The false-negative the shortcut used to cause: a pair already in-sync,
    the canonical then moves, the derived was NOT re-edited (same token) but its
    timestamp is still newer. Old code declared IN_SYNC via mtime and buried the
    drift. With history, only a token match is trusted, so this is a candidate."""
    prior = {"canon_token": "c_old", "deriv_token": "d",
             "canon_tokens": {"/c": "c_old"}, "status": "in-sync"}
    canon = Rev("c_new", 100, True)   # canonical moved
    deriv = Rev("d", 200, True)       # derived unchanged token, but newer mtime
    assert _classify(_pair(), canon, deriv, prior)[0] == CANDIDATE
    # And on genuine first sight the shortcut still spares the cold-start flood.
    assert _classify(_pair(), canon, deriv, None)[0] == IN_SYNC


def test_classify_derived_only_edit_is_candidate():
    """A derived-only change (canonical stable) must re-verify: the edit may have
    introduced drift the old temporal shortcut swallowed."""
    prior = {"canon_token": "c", "deriv_token": "d_old",
             "canon_tokens": {"/c": "c"}, "status": "in-sync"}
    canon = Rev("c", 1, True)
    deriv = Rev("d_new", 9, True)
    assert _classify(_pair(), canon, deriv, prior)[0] == CANDIDATE


# ---- fix 3: the touches gate --------------------------------------

def _multi_pair():
    return _pair(canonical=(Source("file", "/a"), Source("file", "/b")))


def _prior(touches, tb="tb_old"):
    return {"canon_token": "combined_old", "deriv_token": "d",
            "canon_tokens": {"/a": "ta", "/b": tb},
            "status": "in-sync", "touches": touches}


def test_touches_gate_skips_untouched_canonical():
    """B changed, the derived did not, and this view's last draft only relied on
    A. B cannot have drifted a view that never read it, so it is in sync with no
    draft, no model call."""
    canon = Rev("combined_new", 5, True)
    tokens = {"/a": "ta", "/b": "tb_new"}   # only B moved
    verdict, detail = _classify(_multi_pair(), canon, Rev("d", 9, True),
                                _prior(["/a"]), canon_tokens=tokens)
    assert verdict == IN_SYNC and "not referenced" in detail


def test_touches_gate_rechecks_when_a_touched_canonical_changes():
    canon = Rev("combined_new", 5, True)
    tokens = {"/a": "ta", "/b": "tb_new"}
    assert _classify(_multi_pair(), canon, Rev("d", 9, True),
                     _prior(["/b"]), canon_tokens=tokens)[0] == CANDIDATE


def test_touches_gate_off_when_set_unknown():
    """No learned touches set (never drafted) => never narrow. Fail safe."""
    prior = {"canon_token": "combined_old", "deriv_token": "d",
             "canon_tokens": {"/a": "ta", "/b": "tb_old"}, "status": "in-sync"}
    canon = Rev("combined_new", 5, True)
    tokens = {"/a": "ta", "/b": "tb_new"}
    assert _classify(_multi_pair(), canon, Rev("d", 9, True), prior,
                     canon_tokens=tokens)[0] == CANDIDATE


def test_touches_gate_off_when_empty_set():
    """An empty touches set must not skip everything (isdisjoint over empty is
    vacuously true) — the truthiness guard keeps an empty set from narrowing."""
    canon = Rev("combined_new", 5, True)
    tokens = {"/a": "ta", "/b": "tb_new"}
    assert _classify(_multi_pair(), canon, Rev("d", 9, True),
                     _prior([]), canon_tokens=tokens)[0] == CANDIDATE


def test_touches_gate_off_when_derived_also_changed():
    """If the derived itself moved, re-verify regardless of touches."""
    canon = Rev("combined_new", 5, True)
    tokens = {"/a": "ta", "/b": "tb_new"}
    assert _classify(_multi_pair(), canon, Rev("d_new", 9, True),
                     _prior(["/a"]), canon_tokens=tokens)[0] == CANDIDATE


# ---- fix 3: touches parsing + state round trip --------------------

def test_draft_touches_parses_and_matches(tmp_path):
    known = ["/repo/pricing.py", "/repo/capacity.py"]
    exact = _write(tmp_path / "e.md",
                   "<!-- doc-drift: drift=yes -->\n"
                   "<!-- doc-drift: touches=/repo/pricing.py -->\n# x\n")
    base = _write(tmp_path / "b.md",
                  "<!-- doc-drift: drift=yes -->\n"
                  "<!-- doc-drift: touches=capacity.py, pricing.py -->\n# x\n")
    none = _write(tmp_path / "n.md", "<!-- doc-drift: drift=yes -->\n# no touches\n")
    empty = _write(tmp_path / "z.md",
                   "<!-- doc-drift: drift=yes -->\n<!-- doc-drift: touches= -->\n# x\n")
    assert _draft_touches(exact, known) == ["/repo/pricing.py"]
    assert set(_draft_touches(base, known)) == set(known)      # basename match
    assert _draft_touches(none, known) is None                 # absent => unknown
    assert _draft_touches(empty, known) is None                # empty => unknown


def test_write_state_records_canon_tokens_and_touches(tmp_path):
    repo = _make_repo(tmp_path / "r")
    _commit(repo, "doc.md", "old", "2026-01-01T00:00:00")
    _commit(repo, "src.py", "new", "2026-02-01T00:00:00")
    env = _file_pair_env(tmp_path, repo)
    canon_value = next(iter(env.row_for("a").canon_tokens))
    env.write_state({"a": "drafted"}, {"a": [canon_value]})
    st = json.loads(env.state_path.read_text())["pairs"]["a"]
    assert st["touches"] == [canon_value]
    assert st["canon_tokens"] and canon_value in st["canon_tokens"]


def test_write_state_preserves_touches_when_not_redrafted(tmp_path):
    repo = _make_repo(tmp_path / "r")
    _commit(repo, "doc.md", "old", "2026-01-01T00:00:00")
    _commit(repo, "src.py", "new", "2026-02-01T00:00:00")
    canon_value = next(iter(_file_pair_env(tmp_path, repo).row_for("a").canon_tokens))
    _file_pair_env(tmp_path, repo).write_state({"a": "drafted"}, {"a": [canon_value]})
    # A later run that does not re-draft this pair keeps the learned set.
    _file_pair_env(tmp_path, repo).write_state({}, {})
    st = json.loads((tmp_path / "state.json").read_text())["pairs"]["a"]
    assert st["touches"] == [canon_value]


# ---- fix 1: canonical-first map-gap discovery ---------------------

def _disc_env(tmp_path, map_body, window, roots=None):
    return DocDriftEnvironment(
        map_path=_write(tmp_path / "doc-map.toml", map_body),
        state_path=tmp_path / "state.json",
        drafts_dir=tmp_path / "drafts",
        discovery_window_days=window,
        discovery_roots=roots or [],
    )


def _dummy_pair(repo: Path) -> str:
    """A valid pair that watches nothing under the docs dir being tested, so
    the docs there read as unmapped."""
    return (f'[[pair]]\nid="a"\ncanonical="{repo / "src.py"}"\n'
            f'derived="{repo / "other.md"}"\n')


def _seed_repo(repo: Path) -> None:
    _commit(repo, "src.py", "code", "2026-01-01T00:00:00")
    _commit(repo, "other.md", "mapped-derived", "2026-01-01T00:00:00")


def test_doc_root_flagged_when_enough_recent_unmapped(tmp_path):
    repo = _make_repo(tmp_path / "r")
    _seed_repo(repo)
    for i in range(3):
        _commit(repo, f"adrs/{i}.md", f"doc{i}", "2026-06-01T00:00:00")
    env = _disc_env(tmp_path, _dummy_pair(repo), window=10 ** 7, roots=[repo])
    roots = env.unmapped_doc_roots()
    hit = next(r for r in roots if r["dir"] == str(repo / "adrs"))
    assert hit["count"] == 3


def test_doc_root_ignores_glob_mapped_dir(tmp_path):
    repo = _make_repo(tmp_path / "r")
    _commit(repo, "src.py", "code", "2026-01-01T00:00:00")
    for i in range(3):
        _commit(repo, f"guide/{i}.md", f"doc{i}", "2026-06-01T00:00:00")
    body = (f'[[collection]]\nid="g"\ncanonical="{repo / "src.py"}"\n'
            f'glob="{repo / "guide"}/**/*.md"\n')
    env = _disc_env(tmp_path, body, window=10 ** 7, roots=[repo])
    assert env.unmapped_doc_roots() == []   # the whole dir is glob-covered


def test_doc_root_below_threshold_ignored(tmp_path):
    repo = _make_repo(tmp_path / "r")
    _seed_repo(repo)
    for i in range(2):   # only 2 < _DOC_ROOT_MIN
        _commit(repo, f"notes/{i}.md", f"n{i}", "2026-06-01T00:00:00")
    env = _disc_env(tmp_path, _dummy_pair(repo), window=10 ** 7, roots=[repo])
    assert env.unmapped_doc_roots() == []


def test_doc_root_stale_ignored(tmp_path):
    repo = _make_repo(tmp_path / "r")
    _seed_repo(repo)
    for i in range(3):
        _commit(repo, f"adrs/{i}.md", f"d{i}", "2020-01-01T00:00:00")  # ancient
    env = _disc_env(tmp_path, _dummy_pair(repo), window=1, roots=[repo])
    assert env.unmapped_doc_roots() == []   # none changed within the window


def test_doc_root_prunes_junk_dirs(tmp_path):
    repo = _make_repo(tmp_path / "r")
    _seed_repo(repo)
    for i in range(3):
        _commit(repo, f"node_modules/{i}.md", f"junk{i}", "2026-06-01T00:00:00")
    env = _disc_env(tmp_path, _dummy_pair(repo), window=10 ** 7, roots=[repo])
    assert env.unmapped_doc_roots() == []   # node_modules never walked


def test_doc_root_discovery_off_without_roots_or_window(tmp_path):
    repo = _make_repo(tmp_path / "r")
    _seed_repo(repo)
    for i in range(3):
        _commit(repo, f"adrs/{i}.md", f"d{i}", "2026-06-01T00:00:00")
    assert _disc_env(tmp_path, _dummy_pair(repo), window=10 ** 7, roots=[]) \
        .unmapped_doc_roots() == []                       # no roots
    assert _disc_env(tmp_path, _dummy_pair(repo), window=0, roots=[repo]) \
        .unmapped_doc_roots() == []                       # window disabled


def test_persist_state_raises_doc_root_action(tmp_path):
    repo = _make_repo(tmp_path / "r")
    _seed_repo(repo)
    for i in range(3):
        _commit(repo, f"adrs/{i}.md", f"d{i}", "2026-06-01T00:00:00")
    env = _disc_env(tmp_path, _dummy_pair(repo), window=10 ** 7, roots=[repo])
    ctx = _ctx(env)
    _persist_state("", ctx)
    assert any(a["action"].startswith("Add a collection for") and "adrs" in a["action"]
               for a in ctx.operator_actions)
