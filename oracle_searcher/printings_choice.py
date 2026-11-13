"""The printing-level search keywords: `game:` filters a card's printings, `prefer:` picks the one a search returns.

Scryfall reads both (scryfall.com/docs/syntax). `game:paper|mtgo|arena` holds when a printing's `games` has the
value; here it is answered only as a top-level and-ed term (or its negation), where it can filter printings
exactly. Nested anywhere else it reaches the compiler, which has no `card_games` column and raises `Unsupported`.

A printing passes when it is released by today, is not a hidden printing of a visible card (as the `prints` lookup
reads them), and meets every `game:` term. A search naming either keyword returns only cards with a passing
printing, each in the passing printing `prefer:` picks: `usd-low` the lowest USD price (normal, else foil, else
etched; unpriced last; ties the newest), `usd-high` the reverse, `oldest`/`newest` by release date, and
`default`/`promo` (or no `prefer:`) the card's own printing when it passes, else the newest that does. Card
order and paging stay the search's own: `prefer:` picks the printing, never the card.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from api.parsing.card_query_nodes import CardAttributeNode, CardBinaryOperatorNode
from api.parsing.nodes import AndNode, NotNode, StringValueNode, TrueNode
from oracle_searcher.importer import merge_overlay
from oracle_searcher.schema import PRINTINGS_TABLE
from oracle_searcher.sqlite_compiler import Unsupported

if TYPE_CHECKING:
    import sqlite3

    from api.parsing.nodes import QueryNode

GAMES = frozenset({"paper", "mtgo", "arena"})
_KEEPS_THE_CARD_PRINTING = frozenset({None, "default", "promo"})

# Newest first; ties (one release day, many printings) by name, then collector number as a number where it is one.
PRINTING_ORDER = "p.released_at DESC, card.name_sort_key, CAST(p.collector_number AS INTEGER), p.collector_number, p.id"
_OLDEST = "p.released_at ASC, CAST(p.collector_number AS INTEGER), p.collector_number, p.id"
# A printing stores only what differs from its card (importer's overlay), so its prices may be the card's.
_PRICES = "coalesce(json_extract(p.card_json, '$.prices'), json_extract(card.card_json, '$.prices'))"
_USD = (
    f"CAST(coalesce(json_extract({_PRICES}, '$.usd'), json_extract({_PRICES}, '$.usd_foil'),"
    f" json_extract({_PRICES}, '$.usd_etched')) AS REAL)"
)
_PREFER_ORDER = {
    "usd-low": f"{_USD} IS NULL, {_USD} ASC, {PRINTING_ORDER}",
    "usd-high": f"{_USD} IS NULL, {_USD} DESC, {PRINTING_ORDER}",
    "newest": PRINTING_ORDER,
    "oldest": _OLDEST,
}
_CARD_PRINTING_FIRST = f"p.id != json_extract(card.card_json, '$.id'), {PRINTING_ORDER}"


def today_or_now(today: str | None) -> str:
    """`today` as given (tests), else today's UTC date in ISO form."""
    return today or datetime.now(UTC).date().isoformat()


def _game_value(node: QueryNode) -> str | None:
    """The game a `game:` leaf names, or None when `node` is not one."""
    if not (isinstance(node, CardBinaryOperatorNode) and isinstance(node.lhs, CardAttributeNode)):
        return None
    if node.lhs.attribute_name != "card_games":
        return None
    value = node.rhs.value.lower() if isinstance(node.rhs, StringValueNode) and node.operator in (":", "=") else None
    if value not in GAMES:
        msg = f"game {node.rhs!r} is not supported"
        raise Unsupported(msg)
    return value


def _conjuncts(node: QueryNode) -> list[QueryNode]:
    if isinstance(node, AndNode):
        return [leaf for operand in node.operands for leaf in _conjuncts(operand)]
    return [node]


_IN_GAME = "EXISTS (SELECT 1 FROM json_each(p.games) WHERE value = ?)"


@dataclass(frozen=True)
class PrintingTerms:
    """A query's tree with its top-level `game:` terms taken out, those terms as SQL over `p`, and its `prefer:`."""

    root: QueryNode
    game_sql: tuple[str, ...]
    game_params: tuple[str, ...]
    prefer: str | None

    @property
    def needed(self) -> bool:
        """True when the search has to read printings (a `game:` term, or a `prefer:` other than the card's own)."""
        return bool(self.game_sql) or self.prefer not in _KEEPS_THE_CARD_PRINTING

    def passing(self, today: str) -> tuple[str, list[Any]]:
        """WHERE over `card` and `p` for the card's printings that pass (see the module docstring)."""
        clauses = ["p.oracle_id = card.oracle_id", "(card.is_extra = 1 OR p.is_extra = 0)", "p.released_at <= ?", *self.game_sql]
        return " AND ".join(clauses), [today, *self.game_params]


def split_printing_terms(root: QueryNode, prefer: str | None) -> PrintingTerms:
    """Take the top-level `game:` and `-game:` terms out of `root`."""
    kept: list[QueryNode] = []
    game_sql: list[str] = []
    game_params: list[str] = []
    for leaf in _conjuncts(root):
        negated = isinstance(leaf, NotNode)
        game = _game_value(leaf.operand if negated else leaf)
        if game is None:
            kept.append(leaf)
            continue
        game_sql.append(f"NOT {_IN_GAME}" if negated else _IN_GAME)
        game_params.append(game)
    if not game_sql:
        return PrintingTerms(root, (), (), prefer)
    rest = TrueNode() if not kept else kept[0] if len(kept) == 1 else AndNode(kept)
    return PrintingTerms(rest, tuple(game_sql), tuple(game_params), prefer)


def has_passing_printing(terms: PrintingTerms, today: str) -> tuple[str, list[Any]]:
    """A card-level WHERE term: the card has a printing that passes."""
    passing, params = terms.passing(today)
    return f"EXISTS (SELECT 1 FROM {PRINTINGS_TABLE} AS p WHERE {passing})", params


def chosen_printings(conn: sqlite3.Connection, card_ids: list[int], terms: PrintingTerms, today: str) -> list[dict[str, Any]]:
    """Each of `card_ids` (in that order) as its preferred passing printing's full trimmed card JSON."""
    if not card_ids:
        return []
    passing, params = terms.passing(today)
    order = _PREFER_ORDER.get(terms.prefer or "", _CARD_PRINTING_FIRST)
    ids = ", ".join("?" for _ in card_ids)
    rows = conn.execute(
        "SELECT card_id, card_json, overlay FROM ("
        " SELECT card.id AS card_id, card.card_json AS card_json, p.card_json AS overlay,"
        f" ROW_NUMBER() OVER (PARTITION BY card.id ORDER BY {order}) AS pick"
        f" FROM cards AS card CROSS JOIN {PRINTINGS_TABLE} AS p WHERE card.id IN ({ids}) AND {passing}"
        ") WHERE pick = 1",
        [*card_ids, *params],
    ).fetchall()
    by_id = {card_id: merge_overlay(json.loads(card), json.loads(overlay)) for card_id, card, overlay in rows}
    return [by_id[card_id] for card_id in card_ids]
