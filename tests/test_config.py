"""LooperConfig.load() — no module singleton, no global state."""

from pathlib import Path

from gooseloop.config import LooperConfig


def test_load_with_missing_toml_returns_defaults(tmp_path, capsys):
    cfg = LooperConfig.load(anchor=tmp_path, warn_on_missing=False)
    assert cfg.default_model == "openrouter/owl-alpha"
    assert cfg.max_queue_depth == 50
    assert cfg.default_engine == "engines.hello_world"
    assert cfg.retry.max_retries == 6


def test_load_resolves_relative_sessions_dir(tmp_path):
    (tmp_path / "gooseloop.toml").write_text(
        '[gooseloop]\n'
        'default_model = "x"\n'
        'sessions_dir = "my/sessions"\n'
        'default_engine = "engines.hello_world"\n'
        'max_queue_depth = 10\n'
        '\n[gooseloop.retry]\n'
        'max_retries = 2\n'
        'base_delay = 1\n'
    )
    cfg = LooperConfig.load(anchor=tmp_path)
    assert cfg.sessions_dir == (tmp_path / "my/sessions").resolve()
    assert cfg.default_model == "x"
    assert cfg.retry.max_retries == 2


def test_load_two_instances_independent(tmp_path):
    """No module-level cache: separate anchors yield separate configs."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "gooseloop.toml").write_text(
        '[gooseloop]\n'
        'default_model = "model-a"\n'
        'sessions_dir = "reviews"\n'
        'default_engine = "engines.hello_world"\n'
        'max_queue_depth = 50\n'
        '\n[gooseloop.retry]\nmax_retries = 6\nbase_delay = 5\n'
    )
    (b / "gooseloop.toml").write_text(
        '[gooseloop]\n'
        'default_model = "model-b"\n'
        'sessions_dir = "reviews"\n'
        'default_engine = "engines.hello_world"\n'
        'max_queue_depth = 50\n'
        '\n[gooseloop.retry]\nmax_retries = 6\nbase_delay = 5\n'
    )
    cfg_a = LooperConfig.load(anchor=a)
    cfg_b = LooperConfig.load(anchor=b)
    assert cfg_a.default_model == "model-a"
    assert cfg_b.default_model == "model-b"


def test_engine_module_key_still_works_with_deprecation_note(tmp_path, capsys):
    """0.1.x configs keep working: the old `engine_module` key maps to
    default_engine with a rename nudge on stderr (ADR 0009)."""
    (tmp_path / "gooseloop.toml").write_text(
        '[gooseloop]\nengine_module = "engines.old_style"\n'
    )
    cfg = LooperConfig.load(anchor=tmp_path)
    assert cfg.default_engine == "engines.old_style"
    assert cfg.engine_module == "engines.old_style"  # deprecated alias attr
    assert "deprecated" in capsys.readouterr().err


def test_explicit_default_engine_beats_legacy_key(tmp_path, capsys):
    (tmp_path / "gooseloop.toml").write_text(
        '[gooseloop]\ndefault_engine = "engines.new"\nengine_module = "engines.old"\n'
    )
    cfg = LooperConfig.load(anchor=tmp_path)
    assert cfg.default_engine == "engines.new"
