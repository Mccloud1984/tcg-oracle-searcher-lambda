"""`prefer:` is lifted out of the query tree onto `Query.prefer`; `game:` parses as a printing-level filter.

Scryfall reads both (scryfall.com/docs/syntax): `game:paper|mtgo|arena` matches printings available in that game, and
`prefer:` picks which printing of each card comes back without filtering anything. Before this, both were parse errors.
"""

from __future__ import annotations

import pytest

from api.parsing.card_query_nodes import CardAttributeNode, CardBinaryOperatorNode
from api.parsing.nodes import AndNode, NotNode, StringValueNode, TrueNode


@pytest.mark.parametrize("value", ["oldest", "newest", "usd-low", "usd-high", "promo", "default"])
def test_prefer_is_lifted_out_of_the_tree(parse_query, value: str) -> None:
    with_prefer = parse_query(f"otag:ramp prefer:{value} t:artifact")
    without = parse_query("otag:ramp t:artifact")
    assert with_prefer.prefer == value
    assert with_prefer.root == without.root


def test_prefer_alone_leaves_a_match_everything_tree(parse_query) -> None:
    query = parse_query("prefer:usd-low")
    assert query.prefer == "usd-low"
    assert isinstance(query.root, TrueNode)


def test_prefer_value_is_case_insensitive(parse_query) -> None:
    assert parse_query("t:land PREFER:USD-Low").prefer == "usd-low"


def test_a_query_without_prefer_has_none(parse_query) -> None:
    assert parse_query("otag:ramp").prefer is None


@pytest.mark.parametrize(
    ("query", "message"),
    [
        ("prefer:cheapest", "unknown prefer value 'cheapest'"),
        ("t:land (prefer:usd-low or t:artifact)", "prefer: only at the top level"),
        ("prefer:usd-low or t:artifact", "prefer: only at the top level"),
        ("t:land -prefer:usd-low", "prefer: only at the top level"),
        ("prefer:usd-low prefer:oldest", "prefer: more than once"),
    ],
)
def test_prefer_misplaced_or_unknown_is_a_parse_error(parse_query, query: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_query(query)


def _game_leaf(node: object, value: str) -> bool:
    return (
        isinstance(node, CardBinaryOperatorNode)
        and isinstance(node.lhs, CardAttributeNode)
        and node.lhs.attribute_name == "card_games"
        and node.operator == ":"
        and node.rhs == StringValueNode(value)
    )


@pytest.mark.parametrize("value", ["paper", "mtgo", "arena"])
def test_game_parses_to_a_games_leaf(parse_query, value: str) -> None:
    assert _game_leaf(parse_query(f"game:{value}").root, value)


def test_negated_game_and_prefer_together(parse_query) -> None:
    query = parse_query("otag:ramp -game:mtgo game:paper prefer:usd-low")
    assert query.prefer == "usd-low"
    assert isinstance(query.root, AndNode)
    leaves = query.root.operands
    assert any(isinstance(leaf, NotNode) and _game_leaf(leaf.operand, "mtgo") for leaf in leaves)
    assert any(_game_leaf(leaf, "paper") for leaf in leaves)
