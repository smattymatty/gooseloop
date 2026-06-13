# ADR 0003 — Slug is required in frontmatter; no filename fallback

**Status:** Accepted (2026-06-01)

## Context

Prospect markdown files carry a frontmatter `slug:` field — a kebab-case identifier intended as the stable identity of a prospect. The filename tracks where the file lives on disk; the slug is what survives renames, references, and routing.

The codebase had two layers disagreeing about whether `slug:` is required:

- `python/validate.py` treats `slug` as a required frontmatter key. `python3 -m python.validate` rejects any prospect file without one.
- `python/prospects.py:57` (`load_all()` loader) had a fallback: `slug = fm.get("slug") or f.stem.lower()`. If frontmatter omitted `slug:`, the loader silently derived one from the filename.

Today the runner runs `validate` first, so the fallback was dead code. But the inconsistency created two future failure modes:

1. A contributor reading `prospects.py` could conclude "slug is optional" and ship a slug-less prospect.
2. A future tool that calls `load_all()` directly (bypassing `validate`) would get a silently-derived-from-filename slug, defeating the slug-as-stable-identity rule the moment someone renamed the file.

## Decision

`slug:` is **required** in every prospect file's frontmatter. The filename fallback in `prospects.py` is removed. `load_all()` refuses to load any prospect file missing a frontmatter slug, with the same error format `validate.py` uses.

## Consequences

- The slug-as-stable-identity rhetoric in `CLAUDE.md` becomes load-bearing in the code, not just in docs.
- Any prospect file that lacks `slug:` must be edited before it can be loaded. Validator already enforces this, so the only practical change is the dead-code removal in `prospects.py`.
- Future tools that touch prospect files inherit the strict contract — they cannot accidentally normalise from filename.
- Renames on disk no longer affect prospect identity. The dashboard, routing layer, output paths, and analyst CLI all key off slug.

## Rejected alternative — keep the filename fallback (lenient)

Keeping the fallback was the back-compat path. It would have permitted slug-less files to flow through `load_all()` while `validate` rejected them, preserving the two-layer disagreement. The "smooth migration" benefit is illusory — there are no legacy slug-less files at the time of writing, and a forward-only rule is cheaper to maintain than a permanent grandfather clause that nobody is grandfathering.
