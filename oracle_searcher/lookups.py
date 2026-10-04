"""The lookup ops of the search Lambda (`named`, `collection`, `prints`, `autocomplete`), over the open cards file.

Each op is `op(conn, event, attach, *, today=None) -> answer dict`. `attach` makes the printings file available on
`conn` (the handler's `printings_connection`); an op calls it only when it needs a printing, so lookups that stay
in the cards file keep the cards file's cold start. An op raises `Unsupported` when it can't answer exactly; the
handler turns that, and any other failure, into the contract's `{"unsupported"}` / `{"error"}` answers.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from oracle_searcher.importer import folded_name, merge_overlay, name_sort_key
from oracle_searcher.schema import PRINTINGS_TABLE
from oracle_searcher.search import Unsupported, register_regexp, where_for

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

    Attach = Callable[[], object]
    Card = dict[str, Any]

MAX_COLLECTION = 75
MAX_AUTOCOMPLETE = 20
MIN_AUTOCOMPLETE_PREFIX = 2

_COLUMNS = "card_json, released_at, oracle_id"
_FACES = "' // ' || card_name_folded || ' // '"
# The three ways a name matches, in order: the whole name, a face's name (earliest face first), the letters-and-digits key.
_NAME_TIERS = (
    f"SELECT {_COLUMNS} FROM cards WHERE is_extra = :extra AND card_name_folded = :folded ORDER BY id",
    f"SELECT {_COLUMNS} FROM cards WHERE is_extra = :extra AND instr({_FACES}, ' // ' || :folded || ' // ') > 0"
    f" ORDER BY instr({_FACES}, ' // ' || :folded || ' // '), id",
    f"SELECT {_COLUMNS} FROM cards WHERE is_extra = :extra AND :key != '' AND name_sort_key = :key ORDER BY id",
)
# Newest first; ties (one release day, many printings) by name, then collector number as a number where it is one.
_PRINTING_ORDER = "p.released_at DESC, card.name_sort_key, CAST(p.collector_number AS INTEGER), p.collector_number, p.id"


def _today(today: str | None) -> str:
    return today or datetime.now(UTC).date().isoformat()


def _matching_card_row(conn: sqlite3.Connection, name: str) -> tuple[str, str | None, str] | None:
    """The `_COLUMNS` of the card called `name`: visible cards through all three matching steps first, then hidden extras."""
    params = {"folded": folded_name(name), "key": name_sort_key(name)}
    for extra in (0, 1):
        for sql in _NAME_TIERS:
            if row := conn.execute(sql, {**params, "extra": extra}).fetchone():
                return row
    return None


def _printings(conn: sqlite3.Connection, where: str, params: list[Any], limit: int | None = None) -> list[Card]:
    """Printings matching `where` (aliases `card` and `p`), newest first, each as its full trimmed card JSON."""
    sql = f"SELECT card.card_json, p.card_json FROM cards AS card JOIN {PRINTINGS_TABLE} AS p ON p.oracle_id = card.oracle_id"
    sql += f" WHERE {where} ORDER BY {_PRINTING_ORDER}"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [merge_overlay(json.loads(card), json.loads(overlay)) for card, overlay in conn.execute(sql, params)]


def _card_by_name(conn: sqlite3.Connection, name: str, attach: Attach, today: str) -> Card | None:
    """The card called `name`; one whose chosen printing is not out yet comes back as its newest released printing.

    That swap is what Purroxy does with Scryfall's pick today (`_prefer_released_printing`). A card with no released
    printing at all (a brand-new one) keeps its upcoming printing. Only the swap opens the printings file.
    """
    row = _matching_card_row(conn, name)
    if row is None:
        return None
    card_json, released_at, oracle_id = row
    if released_at and released_at > today:
        attach()
        released = _printings(conn, "card.oracle_id = ? AND p.released_at <= ?", [oracle_id, today], limit=1)
        if released:
            return released[0]
    return json.loads(card_json)


def named(conn: sqlite3.Connection, event: dict[str, Any], attach: Attach, *, today: str | None = None) -> dict[str, Any]:
    """`{name}` -> `{card}`, or `{card: None}` when no card matches (Purroxy then asks Scryfall's fuzzy match)."""
    return {"card": _card_by_name(conn, event["name"], attach, _today(today))}


def _card_by_identifier(conn: sqlite3.Connection, ident: dict[str, Any], attach: Attach, today: str) -> Card | None:
    """One collection identifier: a name like `named`, or exactly one printing by id or by set and collector number."""
    keys = set(ident)
    if keys == {"name"}:
        return _card_by_name(conn, ident["name"], attach, today)
    if keys == {"id"}:
        where, params = "p.id = ?", [ident["id"]]
    elif keys == {"set", "collector_number"}:
        where, params = "p.set_code = ? AND p.collector_number = ?", [str(ident["set"]).lower(), str(ident["collector_number"])]
    else:
        msg = f"collection identifier {ident!r} is not supported"
        raise Unsupported(msg)
    attach()
    found = _printings(conn, where, params, limit=1)
    return found[0] if found else None


def collection(conn: sqlite3.Connection, event: dict[str, Any], attach: Attach, *, today: str | None = None) -> dict[str, Any]:
    """`{identifiers}` (at most 75) -> `{data, not_found}`, data in request order."""
    identifiers = event["identifiers"]
    if len(identifiers) > MAX_COLLECTION:
        msg = f"collection takes at most {MAX_COLLECTION} identifiers, got {len(identifiers)}"
        raise Unsupported(msg)
    day = _today(today)
    found = [(ident, _card_by_identifier(conn, ident, attach, day)) for ident in identifiers]
    return {"data": [card for _, card in found if card], "not_found": [ident for ident, card in found if not card]}


def _printing_filters(event: dict[str, Any]) -> tuple[list[str], list[Any]]:
    """WHERE fragments and parameters for the printing-level fields of a `prints` event."""
    clauses: list[str] = []
    params: list[Any] = []
    for field, op in (("sets_exclude", "NOT IN"), ("sets_restrict", "IN")):
        if event.get(field):
            sets = [str(code).lower() for code in event[field]]
            clauses.append(f"p.set_code {op} ({', '.join('?' for _ in sets)})")
            params += sets
    if event.get("released_on_or_before"):
        clauses.append("p.released_at <= ?")
        params.append(event["released_on_or_before"])
    if event.get("paper_only"):
        clauses.append("EXISTS (SELECT 1 FROM json_each(p.games) WHERE value = 'paper')")
    return clauses, params


def prints(conn: sqlite3.Connection, event: dict[str, Any], attach: Attach, *, today: str | None = None) -> dict[str, Any]:  # noqa: ARG001
    """`{q, sets_exclude?, sets_restrict?, released_on_or_before?, paper_only?}` -> `{data}`.

    Every printing of the cards `q` matches (extras included, no paging), newest first, narrowed by the
    printing-level fields.
    """
    register_regexp(conn)
    where, params = where_for(event["q"], include_extras=True)
    clauses, filter_params = _printing_filters(event)
    attach()
    return {"data": _printings(conn, " AND ".join([f"({where})", *clauses]), [*params, *filter_params])}


def autocomplete(conn: sqlite3.Connection, event: dict[str, Any], attach: Attach, *, today: str | None = None) -> dict[str, Any]:  # noqa: ARG001
    """`{prefix}` -> `{names}`: up to 20 visible card names.

    Names starting with the prefix first, then names with it inside, each group by EDHREC rank (unranked last).
    Under two characters there is nothing to suggest.
    """
    folded = folded_name(event["prefix"].strip())
    if len(folded) < MIN_AUTOCOMPLETE_PREFIX:
        return {"names": []}
    rows = conn.execute(
        "SELECT card_name FROM cards WHERE is_extra = 0 AND instr(card_name_folded, :folded) > 0"
        " ORDER BY instr(card_name_folded, :folded) != 1, edhrec_rank IS NULL, edhrec_rank, name_sort_key LIMIT :limit",
        {"folded": folded, "limit": MAX_AUTOCOMPLETE},
    )
    return {"names": [name for (name,) in rows]}


OPS = {"named": named, "collection": collection, "prints": prints, "autocomplete": autocomplete}
