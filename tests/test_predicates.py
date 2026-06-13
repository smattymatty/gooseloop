"""file_freshly_touched, file_nonempty, json_in_stdout."""

import time
from pathlib import Path

from gooseloop.predicates import file_freshly_touched, file_nonempty, json_in_stdout


def test_file_nonempty_true_when_file_has_content(tmp_path):
    p = tmp_path / "f"
    p.write_text("x")
    assert file_nonempty(p)("ignored") is True


def test_file_nonempty_false_when_empty(tmp_path):
    p = tmp_path / "f"
    p.write_text("")
    assert file_nonempty(p)("ignored") is False


def test_file_nonempty_false_when_missing(tmp_path):
    assert file_nonempty(tmp_path / "missing")("ignored") is False


def test_file_freshly_touched_requires_newer_mtime(tmp_path):
    p = tmp_path / "f"
    p.write_text("x")
    pre = p.stat().st_mtime
    # Same mtime — not "strictly newer".
    assert file_freshly_touched(p, pre_mtime=pre)("ignored") is False


def test_file_freshly_touched_passes_when_updated_after_snapshot(tmp_path):
    p = tmp_path / "f"
    p.write_text("first")
    pre = 0.0
    time.sleep(0.01)
    p.write_text("second")
    assert file_freshly_touched(p, pre_mtime=pre)("ignored") is True


def test_json_in_stdout_requires_keys():
    output = '<<<DELIVERABLE_JSON>>>\n{"status": "done", "summary": "ok"}\n<<<END_DELIVERABLE>>>'
    assert json_in_stdout(("status", "summary"))(output) is True
    assert json_in_stdout(("status", "missing"))(output) is False


def test_json_in_stdout_false_when_no_sentinels():
    output = '{"status": "done"}'
    assert json_in_stdout(("status",))(output) is False
