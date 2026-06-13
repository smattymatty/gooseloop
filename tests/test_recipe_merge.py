"""Per ADR 0008: one test per row of the merge rules table, plus removal sentinel."""

from pathlib import Path

import pytest

from gooseloop.recipe_merge import (
    REMOVE_SENTINEL,
    load_layered_recipe,
    merge_recipes,
    resolved_recipe_yaml,
)


# ---- scalar rule: later wins -------------------------------------

def test_scalar_later_wins():
    base = {"title": "old", "version": "1.0"}
    overlay = {"title": "new"}
    assert merge_recipes(base, overlay) == {"title": "new", "version": "1.0"}


def test_prompt_scalar_replaces_fully():
    base = {"prompt": "first do A\nthen B"}
    overlay = {"prompt": "do C only"}
    assert merge_recipes(base, overlay)["prompt"] == "do C only"


# ---- dict rule: deep merge ---------------------------------------

def test_dict_deep_merge():
    base = {"settings": {"goose_model": "x", "goose_provider": "openrouter"}}
    overlay = {"settings": {"goose_model": "y"}}
    merged = merge_recipes(base, overlay)
    assert merged["settings"] == {"goose_model": "y", "goose_provider": "openrouter"}


def test_dict_deep_merge_nested():
    base = {"settings": {"a": {"x": 1, "y": 2}}}
    overlay = {"settings": {"a": {"y": 9}}}
    assert merge_recipes(base, overlay) == {"settings": {"a": {"x": 1, "y": 9}}}


# ---- keyed list: context (label) ---------------------------------

def test_context_same_label_overrides():
    base = {"context": [{"label": "JOURNAL", "source": "env_method:journal_text"}]}
    overlay = {"context": [{"label": "JOURNAL", "source": "file:./j.md"}]}
    merged = merge_recipes(base, overlay)
    assert merged["context"] == [{"label": "JOURNAL", "source": "file:./j.md"}]


def test_context_new_label_appends():
    base = {"context": [{"label": "A", "source": "file:a"}]}
    overlay = {"context": [{"label": "B", "source": "file:b"}]}
    merged = merge_recipes(base, overlay)
    assert merged["context"] == [
        {"label": "A", "source": "file:a"},
        {"label": "B", "source": "file:b"},
    ]


def test_context_overlay_can_add_optional_flag():
    base = {"context": [{"label": "A", "source": "file:a"}]}
    overlay = {"context": [{"label": "A", "optional": True}]}
    merged = merge_recipes(base, overlay)
    assert merged["context"] == [{"label": "A", "source": "file:a", "optional": True}]


# ---- keyed list: extensions ((type, name)) -----------------------

def test_extensions_keyed_by_type_and_name():
    base = {"extensions": [{"type": "stdio", "name": "git"}]}
    overlay = {"extensions": [{"type": "stdio", "name": "git", "cmd": "/usr/bin/git"}]}
    merged = merge_recipes(base, overlay)
    assert merged["extensions"] == [{"type": "stdio", "name": "git", "cmd": "/usr/bin/git"}]


def test_extensions_new_combination_appends():
    base = {"extensions": [{"type": "stdio", "name": "git"}]}
    overlay = {"extensions": [{"type": "builtin", "name": "developer"}]}
    merged = merge_recipes(base, overlay)
    assert merged["extensions"] == [
        {"type": "stdio", "name": "git"},
        {"type": "builtin", "name": "developer"},
    ]


# ---- plain list: later replaces ----------------------------------

def test_plain_list_fully_replaced():
    base = {"tags": ["a", "b", "c"]}
    overlay = {"tags": ["x"]}
    assert merge_recipes(base, overlay) == {"tags": ["x"]}


# ---- removal sentinel --------------------------------------------

def test_remove_sentinel_drops_context_entry():
    base = {"context": [
        {"label": "A", "source": "file:a"},
        {"label": "B", "source": "file:b"},
    ]}
    overlay = {"context": [{"label": "A", "source": REMOVE_SENTINEL}]}
    merged = merge_recipes(base, overlay)
    assert merged["context"] == [{"label": "B", "source": "file:b"}]


# ---- ordering: multiple overlays ---------------------------------

def test_three_layers_order_local_then_cli():
    base = {"settings": {"max_turns": 4}}
    local = {"settings": {"max_turns": 8}}
    cli = {"settings": {"max_turns": 12}}
    merged = merge_recipes(base, local, cli)
    assert merged == {"settings": {"max_turns": 12}}


# ---- purity ------------------------------------------------------

def test_merge_does_not_mutate_inputs():
    base = {"settings": {"a": 1}}
    overlay = {"settings": {"a": 2}}
    merge_recipes(base, overlay)
    assert base == {"settings": {"a": 1}}
    assert overlay == {"settings": {"a": 2}}


# ---- load_layered_recipe + resolved_recipe_yaml -------------------

def test_load_layered_with_local(tmp_path):
    base = tmp_path / "review.yaml"
    base.write_text("title: base\nsettings:\n  max_turns: 4\n")
    local = tmp_path / "review.local.yaml"
    local.write_text("settings:\n  max_turns: 8\n")
    merged = load_layered_recipe(base, local_path=local)
    assert merged == {"title": "base", "settings": {"max_turns": 8}}


def test_load_layered_local_missing_ok(tmp_path):
    base = tmp_path / "review.yaml"
    base.write_text("title: only\n")
    merged = load_layered_recipe(base, local_path=tmp_path / "nope.yaml")
    assert merged == {"title": "only"}


def test_resolved_recipe_yaml_roundtrips():
    merged = {"title": "x", "settings": {"max_turns": 4}}
    text = resolved_recipe_yaml(merged)
    assert "title: x" in text
    assert "max_turns: 4" in text


def test_non_mapping_yaml_rejected(tmp_path):
    p = tmp_path / "broken.yaml"
    p.write_text("- a\n- b\n")
    with pytest.raises(ValueError):
        load_layered_recipe(p)
