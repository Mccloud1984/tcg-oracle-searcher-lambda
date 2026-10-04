"""The public search API: parse a Scryfall-syntax query, compile it, and run it.

`search()` is what grey's Lambda handler calls (docs/PLAN-2026-10-03.md's "Shared contract").
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from api.parsing import parse_scryfall_query
from api.parsing.card_query_nodes import CardAttributeNode, CardBinaryOperatorNode
from api.parsing.nodes import AndNode, NotNode, OrNode, StringValueNode
from oracle_searcher.sqlite_compiler import EXTRA_TYPE_VALUES, Unsupported, compile_query

if TYPE_CHECKING:
    import sqlite3

    from api.parsing.nodes import QueryNode

DEFAULT_PAGE_SIZE = 175

Order = Literal["edhrec", "released", "name", "cmc", "usd"]
Dir = Literal["auto", "asc", "desc"]

# Column backing each `order` value (docs/PLAN-2026-10-03.md's Orange section).
_ORDER_COLUMNS: dict[str, str] = {
    "edhrec": "edhrec_rank",
    "released": "released_at",
    "name": "name_sort_key",
    "cmc": "cmc",
    "usd": "price_usd",
}

# The SQL direction for `dir=auto`, each order's own arrow (scryfall.com/docs/api/cards/search, "Sorting Cards"):
# name A->Z, released Newest->Oldest, cmc 0->highest, usd 0.01->highest, edhrec lowest->highest. `dir=asc` and
# `dir=desc` are literal SQL ASC/DESC: that is what Scryfall's API does (live 2026-10-04: order=released dir=desc is
# newest first, dir=asc oldest first), whatever its docs' "direction of the arrows" wording suggests.
_DEFAULT_SQL_DIRECTION: dict[str, str] = {
    "edhrec": "ASC",
    "released": "DESC",
    "name": "ASC",
    "cmc": "ASC",
    "usd": "ASC",
}

# Orders whose NULLs (unranked edhrec, unpriced usd) sort last regardless of direction.
_NULLS_LAST_ORDERS = frozenset({"edhrec", "usd"})

# Type/subtype values that reveal is_extra rows when a query names them directly, the same way
# Scryfall's own search does (scryfall.com/docs/syntax, "Extra Cards and Funny Cards": vanguard,
# plane, scheme and phenomenon cards are hidden unless you search their type; schema.py's
# is_extra docstring adds tokens and emblems to that same hidden-by-default set). Scryfall's
# website also accepts `include:extras` to reveal everything, but Sylvan's hand parser has no
# `include` alias (api/parsing/db_info.py) -- that spelling isn't parseable here, so it isn't
# handled as a reveal trigger; a caller wanting it needs its own, separate is_extra override.
# Shared with sqlite_compiler.EXTRA_TYPE_VALUES (same words, same reason: these are exactly the
# type-line words the importer stores in card_types despite Sylvan not recognising them as types).

# `is:` values that reveal is_extra rows the same way. Only `funny`: live 2026-10-03 (see
# tests/fixtures/scryfall/is_tag_extras_reveal.json) `is:funny` returns all 1476 funny cards with or without
# `include:extras`, whereas is:digital (7154 vs 7386 with extras), is:alchemy (824 vs 966) and is:unique
# (16115 vs 20516) keep hiding extras, so those do not reveal.
EXTRA_REVEALING_IS_TAGS = frozenset({"funny"})


@dataclass(frozen=True)
class SearchResult:
    """What `search()` returns: one page of cards plus enough to page further."""

    data: list[dict[str, Any]]
    has_more: bool
    total_cards: int


def register_regexp(conn: sqlite3.Connection) -> None:
    """Register SQLite's `REGEXP` operator as Python `re.search`, case-insensitive.

    SQLite has no built-in REGEXP; `x REGEXP y` calls a user function named `regexp(y, x)`
    (pattern first, matched against the connection every time, since `create_function` is
    idempotent per name). Case-insensitive to mirror Sylvan's Postgres `~*` (card_query_nodes.py)
    -- the pattern itself was already validated by `api.parsing.regex_budget` at parse time.
    """

    def regexp(pattern: str, value: str | None) -> int:
        if value is None:
            return 0
        return 1 if re.search(pattern, value, re.IGNORECASE) else 0

    conn.create_function("REGEXP", 2, regexp, deterministic=True)


def _leaf_names_an_extra_type(node: QueryNode) -> bool:
    if not isinstance(node, CardBinaryOperatorNode):
        return False
    lhs, rhs = node.lhs, node.rhs
    if not isinstance(lhs, CardAttributeNode) or not isinstance(rhs, StringValueNode):
        return False
    value = rhs.value.strip()
    if lhs.attribute_name in ("card_types", "card_subtypes"):
        return value.title() in EXTRA_TYPE_VALUES
    return lhs.attribute_name == "card_is_tags" and value.lower() in EXTRA_REVEALING_IS_TAGS


def _reveals_extras(node: QueryNode) -> bool:
    """True when some leaf of the query names an is_extra type/subtype directly (see above)."""
    if isinstance(node, (AndNode, OrNode)):
        return any(_reveals_extras(operand) for operand in node.operands)
    if isinstance(node, NotNode):
        # A negated leaf (`-t:token`) asks to exclude tokens, not reveal them.
        return False
    return _leaf_names_an_extra_type(node)


def _order_by_sql(order: str, direction: str) -> str:
    if order not in _ORDER_COLUMNS:
        msg = f"order {order!r} is not supported"
        raise Unsupported(msg)
    if direction not in ("auto", "asc", "desc"):
        msg = f"dir {direction!r} is not supported"
        raise Unsupported(msg)

    column = _ORDER_COLUMNS[order]
    sql_direction = _DEFAULT_SQL_DIRECTION[order] if direction == "auto" else direction.upper()

    clauses = []
    if order in _NULLS_LAST_ORDERS:
        clauses.append(f"card.{column} IS NULL")
    clauses.append(f"card.{column} {sql_direction}")
    clauses.append("card.name_sort_key ASC")  # Scryfall lists ties (all unranked cards, e.g. tokens) by name
    clauses.append("card.id ASC")  # deterministic tiebreak for stable paging
    return ", ".join(clauses)


def where_for(q: str, *, include_extras: bool) -> tuple[str, list[Any]]:
    """The WHERE clause (table alias `card`) and parameters for query *q*; raises `Unsupported` rather than guessing.

    Without `include_extras` the clause hides what Scryfall hides by default, unless the query names an extra type.
    The lookup ops pass True: they must also find tokens and the like.
    """
    query = parse_scryfall_query(q)
    where_sql, params = compile_query(query)
    if include_extras or _reveals_extras(query.root):
        return where_sql, params
    return f"({where_sql}) AND card.is_extra = 0", params


def search(  # noqa: PLR0913, PLR0917 - signature fixed by docs/PLAN-2026-10-03.md's search() contract
    conn: sqlite3.Connection,
    q: str,
    order: Order = "edhrec",
    dir: Dir = "auto",  # noqa: A002 - name fixed by docs/PLAN-2026-10-03.md's search() contract
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> SearchResult:
    """Parse, compile and run *q* against *conn*. Raises `Unsupported` rather than guessing."""
    register_regexp(conn)
    where_sql, params = where_for(q, include_extras=False)

    total_cards = conn.execute(
        f"SELECT COUNT(*) FROM cards AS card WHERE {where_sql}",
        params,
    ).fetchone()[0]

    order_by_sql = _order_by_sql(order, dir)
    offset = (page - 1) * page_size
    rows = conn.execute(
        f"SELECT card.card_json FROM cards AS card WHERE {where_sql} ORDER BY {order_by_sql} LIMIT ? OFFSET ?",
        [*params, page_size + 1, offset],
    ).fetchall()

    has_more = len(rows) > page_size
    data = [json.loads(row[0]) for row in rows[:page_size]]
    return SearchResult(data=data, has_more=has_more, total_cards=total_cards)


__all__ = ["SearchResult", "Unsupported", "register_regexp", "search", "where_for"]
