"""Post-parse pipeline — shared transforms after any front-end parser."""

from __future__ import annotations

from typing import TYPE_CHECKING

from api.parsing.card_query_nodes import CardAttributeNode, CardBinaryOperatorNode
from api.parsing.nodes import AndNode, NotNode, OrNode, Query, StringValueNode, TrueNode
from api.parsing.query_budget import check_query_byte_length
from api.parsing.regex_budget import validate_regex_patterns
from api.parsing.rewrite import flatten_and_deduplicate_compounds, rewrite_query

if TYPE_CHECKING:
    from collections.abc import Callable

    from api.parsing.nodes import QueryNode


# Scryfall's `prefer:` values (scryfall.com/docs/syntax; Sylvan's api/enums.py PreferOrder).
PREFER_VALUES = frozenset({"default", "oldest", "newest", "usd-low", "usd-high", "promo"})


def _is_prefer(node: QueryNode) -> bool:
    return (
        isinstance(node, CardBinaryOperatorNode) and isinstance(node.lhs, CardAttributeNode) and node.lhs.attribute_name == "prefer"
    )


def _prefer_value(node: CardBinaryOperatorNode) -> str:
    value = node.rhs.value.lower() if isinstance(node.rhs, StringValueNode) and node.operator == ":" else None
    if value not in PREFER_VALUES:
        msg = f"unknown prefer value {node.rhs.value if isinstance(node.rhs, StringValueNode) else node.rhs!r}"
        raise ValueError(msg)
    return value


def _reject_nested_prefer(node: QueryNode) -> None:
    if _is_prefer(node):
        msg = "prefer: only at the top level (not inside an or, or negated)"
        raise ValueError(msg)
    if isinstance(node, (AndNode, OrNode)):
        for operand in node.operands:
            _reject_nested_prefer(operand)
    elif isinstance(node, NotNode):
        _reject_nested_prefer(node.operand)


def _top_level_conjuncts(node: QueryNode) -> list[QueryNode]:
    if isinstance(node, AndNode):
        return [leaf for operand in node.operands for leaf in _top_level_conjuncts(operand)]
    return [node]


def lift_prefer(query: Query) -> Query:
    """Move a top-level `prefer:` leaf out of the tree onto `Query.prefer`.

    `prefer:` chooses which printing of each matching card comes back; it filters nothing, so it may appear once
    among the query's top-level and-ed terms, never inside an `or` or under `-`. An unknown value is an error.
    """
    conjuncts = _top_level_conjuncts(query.root)
    prefers = [leaf for leaf in conjuncts if _is_prefer(leaf)]
    if not prefers:
        _reject_nested_prefer(query.root)
        return query
    if len(prefers) > 1:
        msg = "prefer: more than once"
        raise ValueError(msg)
    rest = [leaf for leaf in conjuncts if not _is_prefer(leaf)]
    for leaf in rest:
        _reject_nested_prefer(leaf)
    root = TrueNode() if not rest else rest[0] if len(rest) == 1 else AndNode(rest)
    return Query(root, prefer=_prefer_value(prefers[0]))


def finalize_query(query: Query) -> Query:
    """Apply semantic rewrites, regex static limits, then flatten+dedupe normalization."""
    prefer = query.prefer
    query = rewrite_query(query)
    validate_regex_patterns(query)
    query = flatten_and_deduplicate_compounds(query)
    query.prefer = prefer
    return query


def parse_query(query: str | None, parser_fn: Callable[[str | None], Query]) -> Query:
    """Run *parser_fn* on *query*, then ``finalize_query``.

    Front-end parsers (``hand_parser.parse_str_to_query``, ``pyparsing_based.parse_str_to_query``)
    each turn a string into a raw AST; this is the single entry for the full public pipeline.
    """
    if query is not None:
        check_query_byte_length(query)
    return finalize_query(lift_prefer(parser_fn(query)))
