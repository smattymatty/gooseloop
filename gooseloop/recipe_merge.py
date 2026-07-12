"""Compose-style recipe overlay merge.

Per ADR 0008 and PROTOCOL.md §6. Two YAML documents merge according to a
type-and-name-dispatched rule table:

    scalars           - later wins (full replace)
    dicts             - deep-merge; scalar leaves: later wins
    context list      - keyed by 'label'; same label overrides, new appends
    extensions list   - keyed by (type, name); same key overrides, new appends
    plain lists       - later replaces fully

A keyed-list overlay entry with `source: REMOVE` (the removal sentinel)
deletes that entry from the merge result.

Layer order is `merge_recipes(base, *overlays)`; later overlays override
earlier ones. The looper composes layers in the order: base file ->
<name>.local.yaml -> --review-overlay X1 -> --review-overlay X2 -> ...
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml


REMOVE_SENTINEL = "REMOVE"


def merge_recipes(base: dict[str, Any], *overlays: dict[str, Any]) -> dict[str, Any]:
    """Merge `base` with successive overlays, returning a new dict.

    The merge is pure: input dicts are never mutated. Per ADR 0008 the
    merge rules dispatch on field name (for keyed lists) and value type
    (everything else).
    """
    out: dict[str, Any] = _copy(base)
    for overlay in overlays:
        out = _merge_dict(out, overlay, path=())
    return out


def load_layered_recipe(
    base_path: Path,
    *,
    local_path: Path | None = None,
    overlay_paths: list[Path] | None = None,
) -> dict[str, Any]:
    """Resolve a recipe by loading base + local + CLI overlays.

    Convention: `<name>.local.yaml` sits next to the base recipe in the
    same directory; the caller computes the local path and passes it.
    Missing local file is fine (skipped).
    """
    base = _read_yaml(base_path)
    overlays: list[dict[str, Any]] = []
    if local_path is not None and local_path.exists():
        overlays.append(_read_yaml(local_path))
    for p in overlay_paths or []:
        overlays.append(_read_yaml(p))
    return merge_recipes(base, *overlays)


def resolved_recipe_yaml(merged: dict[str, Any]) -> str:
    """Render the merged recipe back to YAML (for --resolve debug output)."""
    return yaml.safe_dump(merged, sort_keys=False, default_flow_style=False)


# ---- internals ------------------------------------------------------

# Keyed-list rules: list-field name -> the key function on each entry.
_Keyer = Callable[[dict[str, Any]], Any]

_KEYED_LISTS: dict[str, _Keyer] = {
    "context": lambda entry: entry.get("label"),
    "extensions": lambda entry: (entry.get("type"), entry.get("name")),
}


def _read_yaml(path: Path) -> dict[str, Any]:
    text = path.read_text()
    data = yaml.safe_load(text)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"recipe at {path} must be a YAML mapping at top level")
    return data


def _copy(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_copy(v) for v in value]
    return value


def _merge_dict(
    base: dict[str, Any], overlay: dict[str, Any], *, path: tuple[str, ...]
) -> dict[str, Any]:
    out: dict[str, Any] = _copy(base)
    for k, ov in overlay.items():
        if k not in out:
            out[k] = _copy(ov)
            continue
        bv = out[k]
        if isinstance(bv, dict) and isinstance(ov, dict):
            out[k] = _merge_dict(bv, ov, path=path + (k,))
        elif isinstance(bv, list) and isinstance(ov, list):
            out[k] = _merge_list(bv, ov, list_name=k)
        else:
            out[k] = _copy(ov)
    return out


def _merge_list(base: list[Any], overlay: list[Any], *, list_name: str) -> list[Any]:
    keyer = _KEYED_LISTS.get(list_name)
    if keyer is None:
        # Plain list: later replaces fully.
        return [_copy(v) for v in overlay]
    return _merge_keyed_list(base, overlay, keyer=keyer)


def _merge_keyed_list(
    base: list[Any], overlay: list[Any], *, keyer: _Keyer
) -> list[Any]:
    """Merge two keyed lists by item identity.

    Walk overlay in order. For each entry:
      - If key matches an existing base entry: merge fields (overlay wins).
        REMOVE sentinel on `source` field deletes the base entry.
      - If key is new: append to the result.

    Preserves base order for retained entries; new entries land at the
    end in overlay order.
    """
    result: list[Any] = [_copy(b) for b in base]
    by_key: dict[Any, int] = {}
    for i, entry in enumerate(result):
        if isinstance(entry, dict):
            by_key[keyer(entry)] = i

    for entry in overlay:
        if not isinstance(entry, dict):
            result.append(_copy(entry))
            continue
        key = keyer(entry)
        if key in by_key:
            if entry.get("source") == REMOVE_SENTINEL:
                idx = by_key.pop(key)
                result[idx] = None  # tombstone
                continue
            idx = by_key[key]
            merged = _merge_dict(result[idx], entry, path=())
            result[idx] = merged
        else:
            result.append(_copy(entry))

    return [r for r in result if r is not None]
