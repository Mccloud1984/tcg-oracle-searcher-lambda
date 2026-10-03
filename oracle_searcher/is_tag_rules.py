"""Single source of truth for which `is:`/`has:` values `card_is_tags` can answer exactly.

Three independent mechanisms populate or interpret `card_is_tags` (docs/is-tags.md has the full
per-tag inventory):

1. `api/parsing/rewrite.py`'s `_DERIVED_EXPANSIONS` rewrites a tag into a subtree of *other*
   attributes (`otag:`, `t:`, `layout:`, ...) before the compiler ever sees it -- those tags
   never reach `card_is_tags` as a leaf at all, so they don't need to be in `KNOWN_IS_TAGS`.
2. `oracle_searcher.importer.IS_TAG_CHECKS` computes a tag once per card at import time, from
   that card's own representative printing.
3. `oracle_searcher.is_tag_sweep.SWEEP_TAGS` lists tags `apply_sweep` adds after import, from a
   live Scryfall sweep (facts a single row can't derive on its own).

`KNOWN_IS_TAGS` is the union of (2) and (3): every tag name `card_is_tags` can actually contain.
`oracle_searcher.sqlite_compiler` raises `Unsupported` for any other `is:`/`has:` value reaching
it as a literal `card_is_tags` leaf, rather than silently compiling to "matches nothing" -- the
same failure mode `docs/PLAN-2026-10-03.md`'s `Unsupported` contract exists to prevent.
"""

from __future__ import annotations

from oracle_searcher.importer import IS_TAG_CHECKS
from oracle_searcher.is_tag_sweep import SWEEP_TAGS

KNOWN_IS_TAGS: frozenset[str] = frozenset(IS_TAG_CHECKS) | SWEEP_TAGS

__all__ = ["KNOWN_IS_TAGS"]
