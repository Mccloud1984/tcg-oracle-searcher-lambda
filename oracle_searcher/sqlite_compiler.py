"""Compile Sylvan's parsed query AST into a parameterised SQLite ``WHERE`` clause.

Walks the tree `api.parsing.parse_scryfall_query` returns (already built of
`api.parsing.card_query_nodes` classes — the hand parser constructs those directly, no
separate `to_card_query_ast` step). Sylvan's own `to_sql` methods on those nodes target
PostgreSQL (jsonb `@>`/`<@`, arrays, a different schema) and are reference only: this
module re-derives the SQL for `oracle_searcher/schema.py`'s SQLite schema from scratch.

Anything this module does not handle exactly raises `Unsupported` — never a partial or
guessed translation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from titlecase import titlecase

from api.parsing.card_query_nodes import (
    CardAttributeNode,
    CardBinaryOperatorNode,
    ExactNameNode,
    fold_accents,
    get_art_tags_comparison_object,
    get_colors_comparison_object,
    get_frame_data_comparison_object,
    get_is_tags_comparison_object,
    get_keywords_comparison_object,
    get_legality_comparison_object,
    get_oracle_tags_comparison_object,
    get_rarity_number,
)
from api.parsing.db_info import CARD_SUPERTYPES, CARD_TYPES, FieldType, ParserClass
from api.parsing.nodes import (
    AndNode,
    AttributeNode,
    ManaValueNode,
    NotNode,
    NumericValueNode,
    OrNode,
    Query,
    RegexValueNode,
    StringValueNode,
    TrueNode,
)
from oracle_searcher.schema import COLOR_BITS

if TYPE_CHECKING:
    from api.parsing.nodes import QueryNode

_YEAR_STRING_LENGTH = 4


class Unsupported(Exception):  # noqa: N818 - name fixed by docs/PLAN-2026-10-03.md's search() contract
    """A query is valid syntax but this compiler can't express it as exact SQLite SQL."""

    def __init__(self, reason: str) -> None:
        """Initialize with a human-readable reason naming the node/attribute/operator."""
        self.reason = reason
        super().__init__(reason)


# Columns that get a face-level EXISTS check in addition to their own card-level value,
# per schema.py: "a numeric predicate matches when the card's own value or any face's
# value matches." cmc, edhrec_rank and the prices are card-level only.
_FACE_NUMERIC_COLUMNS = frozenset({"creature_power", "creature_toughness", "planeswalker_loyalty"})

# Text columns matched with a single substring/word-order LIKE pattern rather than exact
# equality when the operator is `:`. card_name is handled separately (folded + accent-stripped).
_PATTERN_TEXT_COLUMNS = frozenset({"oracle_text", "flavor_text", "card_artist"})

# Text columns that are lowercased at import and so compare with a case-insensitive equality
# (no pattern matching) for `:`.
_EXACT_LOWER_TEXT_COLUMNS = frozenset({"card_set_code", "card_layout", "card_border", "card_watermark"})

# JSON-array columns holding a flat set of lower/title-cased strings, all sharing one
# membership-operator family (schema.py: card_types/subtypes are "union over faces";
# the rest are plain per-card tag sets). One shared helper (`_compile_array_membership`)
# serves all of them instead of six near-duplicates.
_ARRAY_COLUMNS = frozenset(
    {"card_types", "card_subtypes", "card_keywords", "card_oracle_tags", "card_art_tags", "card_is_tags", "card_frame_data"}
)

# COLOR-class columns, each an INTEGER bitmask per schema.py (W=1,U=2,B=4,R=8,G=16,C=32).
_COLOR_COLUMNS = frozenset({"card_colors", "card_color_identity", "produced_mana"})

_TAG_VALUE_GETTERS = {
    "card_keywords": get_keywords_comparison_object,
    "card_oracle_tags": get_oracle_tags_comparison_object,
    "card_art_tags": get_art_tags_comparison_object,
    "card_is_tags": get_is_tags_comparison_object,
    "card_frame_data": get_frame_data_comparison_object,
}


def compile_query(query: Query) -> tuple[str, list[Any]]:
    """Compile a parsed `Query` to `(where_sql, params)` for `WHERE {where_sql}` over `cards AS card`."""
    params: list[Any] = []
    sql = _compile_node(query.root, params)
    return sql, params


def _compile_node(node: QueryNode, params: list[Any]) -> str:
    cls = node.__class__
    if cls is AndNode:
        return _compile_nary(node.operands, "AND", "TRUE", params)
    if cls is OrNode:
        return _compile_nary(node.operands, "OR", "FALSE", params)
    if cls is NotNode:
        return f"NOT ({_compile_node(node.operand, params)})"
    if cls is TrueNode:
        return "TRUE"
    if cls is ExactNameNode:
        return _compile_exact_name(node, params)
    if isinstance(node, CardBinaryOperatorNode):
        return _compile_binary(node, params)
    if isinstance(node, (CardAttributeNode, AttributeNode)):
        msg = f"a bare attribute ({node.attribute_name!r}) is not something a WHERE clause alone (no comparison) can express"
        raise Unsupported(msg)
    msg = f"node type {cls.__name__} is not supported"
    raise Unsupported(msg)


def _compile_nary(operands: list[QueryNode], joiner: str, empty: str, params: list[Any]) -> str:
    if not operands:
        return empty
    parts = [_compile_node(op, params) for op in operands]
    if len(parts) == 1:
        return parts[0]
    return "(" + f" {joiner} ".join(parts) + ")"


def _compile_exact_name(node: ExactNameNode, params: list[Any]) -> str:
    params.append(node.value.lower())
    return "(lower(card.card_name) = ?)"


def _compile_binary(node: CardBinaryOperatorNode, params: list[Any]) -> str:
    lhs = node.lhs
    if not isinstance(lhs, CardAttributeNode):
        msg = f"a non-attribute left-hand side ({lhs!r}) is not supported"
        raise Unsupported(msg)
    if not lhs.field_infos:
        msg = f"attribute {lhs.original_attribute!r} has no known field mapping"
        raise Unsupported(msg)

    field_info = lhs.field_infos[0]
    attr = lhs.attribute_name
    operator = node.operator

    if field_info.parser_class == ParserClass.MANA:
        msg = f"mana cost / devotion comparisons ({attr} {operator} ...) are not supported yet"
        raise Unsupported(msg)
    if field_info.parser_class == ParserClass.DATE:
        return _compile_date(node, params)
    if field_info.parser_class == ParserClass.YEAR:
        return _compile_year(node, params)
    if field_info.parser_class == ParserClass.RARITY:
        return _compile_rarity(node, params)
    if field_info.parser_class == ParserClass.NUMERIC:
        return _compile_numeric(attr, operator, node.rhs, params)
    if field_info.parser_class == ParserClass.COLOR:
        return _compile_color(attr, operator, node.rhs, params)
    if field_info.parser_class == ParserClass.LEGALITY:
        return _compile_legality(lhs, operator, node.rhs, params)

    if attr in ("card_types", "card_subtypes"):
        return _compile_type_subtype(operator, node.rhs, params)
    if attr in _ARRAY_COLUMNS:
        return _compile_array_membership(attr, operator, node.rhs, params)

    if field_info.field_type == FieldType.TEXT:
        return _compile_text(attr, operator, node.rhs, params)

    msg = f"attribute {attr!r} with field type {field_info.field_type!r} is not supported"
    raise Unsupported(msg)


# ── numeric (cmc, power, toughness, loyalty, edhrec_rank, prices) ───────────────────────────


def _numeric_value(rhs: QueryNode) -> float:
    if isinstance(rhs, NumericValueNode):
        return rhs.value
    msg = f"a non-numeric right-hand side ({rhs!r}) for a numeric comparison is not supported"
    raise Unsupported(msg)


def _compile_numeric(attr: str, operator: str, rhs: QueryNode, params: list[Any]) -> str:
    value = _numeric_value(rhs)
    sql_op = "=" if operator == ":" else operator
    if sql_op not in ("=", "!=", "<", "<=", ">", ">="):
        msg = f"numeric operator {operator!r} is not supported"
        raise Unsupported(msg)

    params.append(value)
    own = f"card.{attr} {sql_op} ?"
    if attr not in _FACE_NUMERIC_COLUMNS:
        return f"({own})"
    params.append(value)
    face = f"EXISTS (SELECT 1 FROM card_faces AS cf WHERE cf.card_id = card.id AND cf.{attr} {sql_op} ?)"
    return f"({own} OR {face})"


# ── rarity (text rarity name -> card_rarity_int) ─────────────────────────────────────────────


def _compile_rarity(node: CardBinaryOperatorNode, params: list[Any]) -> str:
    rhs = node.rhs
    if isinstance(rhs, StringValueNode):
        rarity_number = get_rarity_number(rhs.value)
    elif isinstance(rhs, NumericValueNode):
        rarity_number = rhs.value
    else:
        msg = f"a non-string, non-numeric rarity value ({rhs!r}) is not supported"
        raise Unsupported(msg)
    return _compile_numeric("card_rarity_int", node.operator, NumericValueNode(rarity_number), params)


# ── date / year ──────────────────────────────────────────────────────────────────────────────


def _date_value(rhs: QueryNode) -> str:
    if isinstance(rhs, (StringValueNode, NumericValueNode)):
        return str(rhs.value)
    msg = f"a non-literal date value ({rhs!r}) is not supported"
    raise Unsupported(msg)


def _year_bounds_sql(year: int, operator: str, params: list[Any]) -> str:
    start_of_year = f"{year}-01-01"
    start_of_next_year = f"{year + 1}-01-01"
    if operator == "=":
        params.extend([start_of_year, start_of_next_year])
        return "(? <= card.released_at AND card.released_at < ?)"
    if operator == ">":
        params.append(start_of_next_year)
        return "(card.released_at >= ?)"
    if operator == "<":
        params.append(start_of_year)
        return "(card.released_at < ?)"
    if operator == ">=":
        params.append(start_of_year)
        return "(card.released_at >= ?)"
    if operator == "<=":
        params.append(start_of_next_year)
        return "(card.released_at < ?)"
    msg = f"year/date operator {operator!r} is not supported"
    raise Unsupported(msg)


def _compile_year(node: CardBinaryOperatorNode, params: list[Any]) -> str:
    value = _date_value(node.rhs)
    operator = "=" if node.operator == ":" else node.operator
    if not (isinstance(value, str) and len(value) == _YEAR_STRING_LENGTH and value.isdigit()):
        msg = f"year value {value!r} must be a 4-digit year"
        raise Unsupported(msg)
    return _year_bounds_sql(int(value), operator, params)


def _compile_date(node: CardBinaryOperatorNode, params: list[Any]) -> str:
    value = _date_value(node.rhs)
    operator = "=" if node.operator == ":" else node.operator
    # A bare 4-digit year given to `date:` is a year range, exactly like `year:` -- Scryfall
    # accepts both YYYY and YYYY-MM-DD for date:, and a bare year compared lexically against a
    # full ISO date would never equal anything.
    if len(value) == _YEAR_STRING_LENGTH and value.isdigit():
        return _year_bounds_sql(int(value), operator, params)
    if operator not in ("=", "!=", "<", "<=", ">", ">="):
        msg = f"date operator {node.operator!r} is not supported"
        raise Unsupported(msg)
    params.append(value)
    return f"(card.released_at {operator} ?)"


# ── color / color identity / produced mana (INTEGER bitmasks) ───────────────────────────────


def _compile_color(attr: str, operator: str, rhs: QueryNode, params: list[Any]) -> str:
    if not isinstance(rhs, StringValueNode):
        msg = f"a non-string color value ({rhs!r}) is not supported"
        raise Unsupported(msg)
    comparison = get_colors_comparison_object(rhs.value.strip().lower(), attr)
    query_mask = sum(COLOR_BITS[code] for code in comparison)
    is_identity = attr == "card_color_identity"

    # `:` is shorthand that flips meaning by attribute, matching Scryfall: for card_colors /
    # produced_mana it means "at least these colors" (superset, same as `>=`); for color
    # identity it means "at most these colors" (subset, same as `<=`) -- verified live against
    # api.scryfall.com (docs/technical/scryfall_syntax_analysis.md; id<=esper / id:esper agree).
    resolved_operator = operator
    if operator == ":":
        resolved_operator = "<=" if is_identity else ">="

    if resolved_operator == "=":
        params.append(query_mask)
        return f"(card.{attr} = ?)"
    if resolved_operator == ">=":
        # An empty query_mask ("c"/"colorless") makes superset vacuously true for every row;
        # the real meaning is "has no colors at all", i.e. exact equality to 0.
        if query_mask == 0:
            params.append(query_mask)
            return f"(card.{attr} = ?)"
        params.extend([query_mask, query_mask])
        return f"((card.{attr} & ?) = ?)"
    if resolved_operator == ">":
        params.extend([query_mask, query_mask, query_mask])
        return f"((card.{attr} & ?) = ? AND card.{attr} != ?)"
    if resolved_operator == "<=":
        params.append(query_mask)
        return f"((card.{attr} & ~?) = 0)"
    if resolved_operator == "<":
        params.extend([query_mask, query_mask])
        return f"((card.{attr} & ~?) = 0 AND card.{attr} != ?)"
    if resolved_operator in ("!=", "<>"):
        params.append(query_mask)
        return f"(card.{attr} != ?)"
    msg = f"color operator {operator!r} is not supported"
    raise Unsupported(msg)


# ── legality (JSON object {format: status}) ──────────────────────────────────────────────────


def _compile_legality(lhs: CardAttributeNode, operator: str, rhs: QueryNode, params: list[Any]) -> str:
    if operator not in (":", "="):
        msg = f"legality operator {operator!r} is not supported"
        raise Unsupported(msg)
    if not isinstance(rhs, StringValueNode):
        msg = f"a non-string legality value ({rhs!r}) is not supported"
        raise Unsupported(msg)
    comparison = get_legality_comparison_object(rhs.value.strip(), lhs.original_attribute)
    ((format_name, status),) = comparison.items()
    params.extend([f"$.{format_name}", status])
    return "(json_extract(card.card_legalities, ?) = ?)"


# ── card_types / card_subtypes (ambiguous `t:`/`type:` alias resolves per value) ────────────

# Type-line words the importer's `parse_type_line` (api.card_processing, reused verbatim) puts in
# card_types because they sit before the type line's em dash, even though Sylvan's CARD_TYPES /
# CARD_SUPERTYPES don't recognise them as a real card type -- e.g. a token's type line is "Token
# Creature — Elf" and an emblem's is just "Emblem" (schema.py: card_types/card_subtypes are
# parsed the same way as the importer parses them). Confirmed against the real 2026-10-03 build
# (docs/PLAN-2026-10-03.md, Purple item 2): `card_types` held `["Token", "Creature"]` for every
# token row and `["Emblem"]` for every emblem row, never in card_subtypes. Routing `t:token` to
# card_subtypes instead (the pre-fix behaviour) found zero rows against a real build, where
# Scryfall finds 821. Also the five is_extra reveal words `search.py` already special-cases.
EXTRA_TYPE_VALUES = frozenset({"Token", "Emblem", "Vanguard", "Plane", "Phenomenon", "Scheme"})


def _compile_type_subtype(operator: str, rhs: QueryNode, params: list[Any]) -> str:
    if not isinstance(rhs, StringValueNode):
        msg = f"a non-string type value ({rhs!r}) is not supported"
        raise Unsupported(msg)
    value = rhs.value.strip().title()
    column = "card_types" if value in CARD_SUPERTYPES | CARD_TYPES | EXTRA_TYPE_VALUES else "card_subtypes"
    return _array_membership_sql(column, operator, value, params)


def _compile_array_membership(attr: str, operator: str, rhs: QueryNode, params: list[Any]) -> str:
    if not isinstance(rhs, StringValueNode):
        msg = f"a non-string value ({rhs!r}) for {attr!r} is not supported"
        raise Unsupported(msg)
    getter = _TAG_VALUE_GETTERS[attr]
    comparison = getter(rhs.value.strip())
    if len(comparison) != 1:
        msg = f"{attr!r} value {rhs.value!r} did not normalize to exactly one entry"
        raise Unsupported(msg)
    (value,) = comparison.keys()
    return _array_membership_sql(attr, operator, value, params)


def _array_membership_sql(column: str, operator: str, value: str, params: list[Any]) -> str:
    """One value against a JSON array column, treating the array as a set (schema.py).

    Shared by card_types/subtypes and the plain tag/keyword arrays: all are a flat JSON
    array of strings with the identical operator family (`:`/`>=` membership, `=` exact
    singleton, etc.) -- see this module's docstring on `_ARRAY_COLUMNS`.
    """
    member = f"EXISTS (SELECT 1 FROM json_each(card.{column}) WHERE value = ?)"
    other_member = f"EXISTS (SELECT 1 FROM json_each(card.{column}) WHERE value != ?)"
    no_members = f"NOT EXISTS (SELECT 1 FROM json_each(card.{column}))"

    if operator in (":", ">="):
        params.append(value)
        return f"({member})"
    if operator == "=":
        params.extend([value, value])
        return f"({member} AND NOT ({other_member}))"
    if operator == "<=":
        params.append(value)
        return f"(NOT ({other_member}))"
    if operator == ">":
        params.extend([value, value])
        return f"({member} AND {other_member})"
    if operator == "<":
        # A non-empty array can only be a *proper* subset of a one-element set by being empty.
        return f"({no_members})"
    if operator in ("!=", "<>"):
        params.extend([value, value])
        return f"NOT ({member} AND NOT ({other_member}))"
    msg = f"array operator {operator!r} on {column!r} is not supported"
    raise Unsupported(msg)


# ── free text (name, oracle text, flavor text, artist, set/layout/border/watermark, …) ──────


def _compile_text(attr: str, operator: str, rhs: QueryNode, params: list[Any]) -> str:
    if isinstance(rhs, RegexValueNode):
        return _compile_regex(attr, rhs, params)

    if not isinstance(rhs, (StringValueNode, ManaValueNode)):
        msg = f"a non-string value ({rhs!r}) for {attr!r} is not supported"
        raise Unsupported(msg)
    value = rhs.value.strip()

    if operator == ":":
        if attr in _EXACT_LOWER_TEXT_COLUMNS:
            params.append(value.lower())
            return f"(lower(card.{attr}) = ?)"
        if attr == "collector_number":
            params.append(value)
            return f"(card.{attr} = ?)"
        return _text_pattern_sql(attr, value, params)

    if operator not in ("=", "!=", "<", "<=", ">", ">="):
        msg = f"text operator {operator!r} on {attr!r} is not supported"
        raise Unsupported(msg)

    compare_value = value
    if attr in ("card_name", "card_artist"):
        compare_value = titlecase(value)
    elif attr == "card_set_code":
        compare_value = value.lower()
    params.append(compare_value)
    return f"(card.{attr} {operator} ?)"


def _text_pattern_sql(attr: str, value: str, params: list[Any]) -> str:
    column = attr
    search_value = value
    if attr == "card_name":
        column = "card_name_folded"
        search_value = fold_accents(value)
    words = search_value.lower().split()
    pattern = "%" + "%".join(_escape_like(word) for word in words) + "%" if words else "%"
    params.append(pattern)
    return rf"(lower(card.{column}) LIKE ? ESCAPE '\')"


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def _compile_regex(attr: str, rhs: RegexValueNode, params: list[Any]) -> str:
    column = attr
    if attr not in _PATTERN_TEXT_COLUMNS and attr != "card_name":
        msg = f"regex matching on {attr!r} is not supported"
        raise Unsupported(msg)
    params.append(rhs.value)
    return f"(card.{column} REGEXP ?)"
