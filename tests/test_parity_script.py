"""Tests for the pure helpers in scripts/parity.py (never calls Scryfall)."""

from __future__ import annotations

from scripts.parity import top_n_overlap


def test_identical_top_lists_with_repeated_names_overlap_fully() -> None:
    """Tokens repeat names (several `Alien`, five `Angel`); identical lists once scored 0.70, not 1.0."""
    names = ["Alien", "Alien", "Alien", "Angel", "Angel", "Angel", "Angelo"]
    assert top_n_overlap(names, names, n=7) == 1.0


def test_disjoint_lists_overlap_zero() -> None:
    assert top_n_overlap(["Sol Ring"], ["Black Lotus"]) == 0.0
