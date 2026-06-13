"""Phase, Pipeline, Context typed-ledger methods."""

from pathlib import Path

import pytest

from gooseloop import Context, Phase, Pipeline


def test_pipeline_construct_with_review_and_summary():
    review = Phase(name="r", recipe_path="r.yaml")
    summary = Phase(name="s", recipe_path="s.yaml")
    p = Pipeline(review=review, body=[], summary=summary)
    assert p.review is review
    assert p.summary is summary
    assert p.body == []


def test_pipeline_body_defaults_to_empty():
    review = Phase(name="r", recipe_path="r.yaml")
    summary = Phase(name="s", recipe_path="s.yaml")
    p = Pipeline(review=review, summary=summary)
    assert p.body == []


def test_pipeline_summary_can_be_none():
    review = Phase(name="r", recipe_path="r.yaml")
    p = Pipeline(review=review)
    assert p.summary is None


def _ctx() -> Context:
    return Context(model="test", session_dir=None, base_env={})


def test_add_operator_action_appends():
    ctx = _ctx()
    ctx.add_operator_action(action="do X", why="because")
    assert ctx.operator_actions == [{"action": "do X", "why": "because"}]


def test_add_operator_action_dedups_by_action_and_why():
    ctx = _ctx()
    ctx.add_operator_action(action="do X", why="because")
    ctx.add_operator_action(action="do X", why="because")
    ctx.add_operator_action(action="do X", why="different reason")
    assert ctx.operator_actions == [
        {"action": "do X", "why": "because"},
        {"action": "do X", "why": "different reason"},
    ]


def test_add_operator_action_carries_extras():
    ctx = _ctx()
    ctx.add_operator_action(action="verify", why="visual", panel_id="ServersTable")
    assert ctx.operator_actions == [{
        "action": "verify", "why": "visual", "panel_id": "ServersTable",
    }]


def test_add_operator_action_rejects_empty_action():
    ctx = _ctx()
    with pytest.raises(TypeError, match="action"):
        ctx.add_operator_action(action="", why="ok")


def test_add_operator_action_accepts_empty_why():
    """Empty why is allowed — some actions don't have a stated reason
    (e.g. the model emitted operator_actions as a bare string list and
    validate_review normalised it to {action: str, why: ""})."""
    ctx = _ctx()
    ctx.add_operator_action(action="just do this thing")
    ctx.add_operator_action(action="another", why="")
    assert ctx.operator_actions == [
        {"action": "just do this thing", "why": ""},
        {"action": "another", "why": ""},
    ]


def test_add_operator_action_rejects_non_str_why():
    ctx = _ctx()
    with pytest.raises(TypeError, match="why"):
        ctx.add_operator_action(action="ok", why=None)


def test_operator_actions_is_a_copy():
    ctx = _ctx()
    ctx.add_operator_action(action="a", why="b")
    snapshot = ctx.operator_actions
    snapshot.append({"action": "evil", "why": "mutation"})
    assert ctx.operator_actions == [{"action": "a", "why": "b"}]


def test_record_output_dedups():
    ctx = _ctx()
    ctx.record_output(Path("/tmp/a"))
    ctx.record_output("/tmp/a")
    ctx.record_output("/tmp/b")
    assert ctx.artifacts["outputs_written"] == ["/tmp/a", "/tmp/b"]


def test_session_log_noop_without_session(tmp_path):
    ctx = _ctx()
    ctx.session_log("nothing should explode")  # no session_dir => silent
