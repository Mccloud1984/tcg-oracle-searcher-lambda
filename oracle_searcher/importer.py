"""Build the SQLite card file from Scryfall's oracle_cards and oracle_tags bulk dumps.

`build()` streams both files (gzipped or plain JSONL) and writes one row per oracle card into
the schema in `oracle_searcher.schema`, reusing Sylvan Librarian's pure helpers
(`api.card_processing`, `api.parsing.card_query_nodes`, and two functions ported from
`api.tag_import` -- see `_build_uuid_to_slug`/`_build_all_ancestors` below for why they're
ported rather than imported) for the parts they already get right: type-line parsing, rarity,
mana-cost dicts, devotion, name folding, and oracle-tag ancestor propagation -- rather than
rewriting them. Sylvan's own `preprocess_card`
is NOT reused here: it drops tokens, emblems and non-paper cards entirely, which is correct for
Sylvan's one-row-per-playable-face table but wrong for this schema, which keeps every oracle
card (including the "extra" ones) and flags them with `is_extra` instead (see `_is_extra`).
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from api.card_processing import (
    extract_collector_number_int,
    extract_frame_data_from_raw_card,
    maybe_float,
    parse_type_line,
    rarity_text_to_int,
)
from api.parsing.card_query_nodes import calculate_devotion, fold_accents, mana_cost_str_to_dict
from oracle_searcher.schema import COLOR_BITS, create_schema

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

logger = logging.getLogger(__name__)

# Layouts Scryfall's own search hides unless the query explicitly asks for them (confirmed live,
# 2026-10-03: see tests/fixtures/scryfall/layout_*_default.json -- each of these layouts' sample
# cards carries legalities that are all "not_legal"). "art_series" and "front_card" are covered by
# the set_type check below instead of listed here, since a handful of other oddities (memorabilia
# World Championship reprints, etc.) share those layouts with ordinary playable cards.
_HIDDEN_LAYOUTS = frozenset({"token", "double_faced_token", "emblem", "vanguard", "scheme", "planar"})

# Card types that can exist as a permanent on the battlefield (mirrors
# api.card_processing.PERMANENT_CARD_TYPES; devotion is undefined for an Instant/Sorcery).
_PERMANENT_CARD_TYPES = frozenset({"Artifact", "Battle", "Creature", "Enchantment", "Land", "Planeswalker"})

# card_json keeps only what Purroxy reads from a Scryfall card (backend/app/services/utils.py: format_card_response,
# _scryfall_to_card, card_image_uri, price_fields, purchase_fields; tokens.py; proxy art and frame code). Everything
# else (artist, frame, set URIs, flags, ...) is dropped: it was most of the 255 MB file.
_KEEP_CARD_FIELDS = frozenset(
    {
        "id",
        "oracle_id",
        "name",
        "mana_cost",
        "cmc",
        "type_line",
        "oracle_text",
        "flavor_text",
        "power",
        "toughness",
        "loyalty",
        "colors",
        "color_identity",
        "keywords",
        "layout",
        "set",
        "set_name",
        "set_type",
        "collector_number",
        "rarity",
        "released_at",
        "prices",
        "purchase_uris",
        "legalities",
        "game_changer",
        "edhrec_rank",
    }
)
_KEEP_FACE_FIELDS = frozenset({"name", "mana_cost", "type_line", "oracle_text", "power", "toughness", "loyalty"})
_KEEP_IMAGE_SIZES = frozenset({"grid", "large", "art_crop"})  # the sizes Purroxy asks for
_KEEP_PART_FIELDS = frozenset({"id", "component", "name", "type_line"})

_HYBRID_MANA_RE = re.compile(r"\{[2CWUBRG]/[WUBRG]")
_PHYREXIAN_MANA_RE = re.compile(r"/P\}")

# A single non-nested parenthesised span. Reminder text is never otherwise parenthesised in
# Oracle text, so repeatedly stripping innermost spans (see `_strip_reminder_text`) removes
# reminder text exactly, including the handful of real cards with reminder text nested two or
# three deep (e.g. "Super haste (This may attack the turn before you cast it. (You may put...))").
_PAREN_SPAN_RE = re.compile(r"\([^()]*\)")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]")
_RUN_OF_SPACES_RE = re.compile(r"[ \t]+")
_SPACE_AROUND_NEWLINE_RE = re.compile(r" *\n *")

# Scryfall's is:partner is "any flavor of Commander partner mechanic" on a legendary card (live
# 2026-10-03: 228 cards; docs/is-tags.md). Keywords alone match only 130: the Backgrounds and the
# Time Lord Doctors carry no keyword. "Partner with" is its own keyword string on some cards. The
# non-legendary Battlebond partners and the other "Doctor" subtypes (Doctor Strange) are not in it.
_PARTNER_KEYWORDS = frozenset({"partner", "partner with", "friends forever", "choose a background", "doctor's companion"})
_PARTNER_TYPE_LINE_RE = re.compile(r"\bBackground\b|\bTime Lord Doctor\b")


def _is_partner(card: dict[str, Any]) -> bool:
    type_line = card.get("type_line") or ""
    if "Legendary" not in type_line:
        return False
    keywords = {keyword.lower() for keyword in card.get("keywords") or []}
    return bool(keywords & _PARTNER_KEYWORDS or _PARTNER_TYPE_LINE_RE.search(type_line))


# Ported from Sylvan's api.admin_resource.BOOLEAN_IS_TAGS (a SQL expression per tag, evaluated
# against raw_card_blob in Postgres) to direct predicates over the raw Scryfall dict, evaluated
# once per card at import time instead of in a post-import SQL sweep. Each predicate reads the
# *representative* printing oracle_cards picked for this oracle card, not every printing, so a
# tag like `foil` here means "this printing is foil", not "a foil printing exists" -- a known
# narrowing versus Scryfall's real search, acceptable for v1 (single row per oracle card).
# Sylvan's CUSTOM_IS_TAGS and LAND_IS_TAGS (historic, permanent, fetchland, ...) need a live
# per-tag Scryfall search to discover membership and are out of scope here.
def _promo_types(card: dict[str, Any]) -> set[str]:
    return set(card.get("promo_types") or [])


def _finishes(card: dict[str, Any]) -> set[str]:
    return set(card.get("finishes") or [])


def _commander_front_face(card: dict[str, Any]) -> dict[str, Any]:
    """The face whose type line and printed toughness decide commander eligibility.

    Comprehensive Rule 712.8/901.5-ish framing: a double-faced permanent's characteristics (for
    anything checked before it's on the battlefield, like "can this be my commander") come from
    its front face. Scryfall's is:commander agrees: Westvale Abbey // Ormendahl, Profane Prince
    (front face Westvale Abbey, a plain Land) and Invasion of Ikoria // Zilortha, Apex of Ikoria
    (front face a Battle) are both excluded despite a legendary-creature back face (confirmed live
    2026-10-03: neither is in Scryfall's is:commander results, both are in ours before this fix).
    """
    faces = card.get("card_faces") or []
    return faces[0] if faces else card


# "As long as Grist isn't on the battlefield, it's a 1/1 Insect creature": a Legendary non-creature that is a creature
# in the command zone. Scryfall's is:commander includes it (live 2026-10-04); no other card has the text.
_COMMAND_ZONE_CREATURE_RE = re.compile(r"isn't on the battlefield, it's an? [^.]*\bcreature\b")


def _is_meld_result(card: dict[str, Any]) -> bool:
    return any(part.get("id") == card.get("id") and part.get("component") == "meld_result" for part in card.get("all_parts") or [])


def _is_commander_eligible(card: dict[str, Any], oracle_text: str | None) -> bool:
    """True for a card Scryfall's `is:commander` returns: can legally be named a commander.

    Front-face-only structural rule, replacing Sylvan's `is:commander` rewrite-time expansion
    (`api.parsing.rewrite._DERIVED_EXPANSIONS`, removed in the same change that added this): that
    expansion worked over the schema's face-UNIONED columns ("a card matches when ANY face
    matches" -- schema.py), which is right for text/type searches but wrong for a structural
    eligibility rule that Scryfall evaluates on the front face alone, and it used `toughness>=0`
    as a proxy for "this permanent prints a toughness" that silently broke on this schema's `*`
    toughness (stored as NULL, not 0 -- Sylvan's own comment assumed "* compares as 0 on both
    engines", true of its Postgres column, not of this REAL column). Verified live 2026-10-03:
    Ashaya, Soul of the Wild / Daxos, Blessed by the Sun / Lumra, Bellow of the Woods (all `*`
    toughness) are all in Scryfall's is:commander; `toughness>=0` excluded them here.

    Eligible: the front face is a Legendary permanent with a printed toughness field (creatures,
    Vehicles, Spacecraft -- present even when its value is "*", absent for anything without
    power/toughness at all) or is a Background; OR the card's oracle text grants eligibility
    outright ("can be your commander") -- checked on the combined text, since this is a textual
    grant rather than a structural one and nothing in the 2026-10-03 corpus needs narrowing it to
    one face. MINUS cards banned as commander (Griselbrand, Golos, Emrakul, Erayo were the
    over-catch Sylvan's comment recorded from the same structural rule, live-diffed against
    Scryfall's is:commander: all three legendary creatures above are `legalities.commander ==
    "banned"`).
    """
    front = _commander_front_face(card)
    type_line = front.get("type_line") or ""
    is_legendary = "Legendary" in type_line
    is_background = "Background" in type_line
    has_printed_toughness = "toughness" in front
    text = (oracle_text or "").lower()
    grants_eligibility = "can be your commander" in text
    command_zone_creature = is_legendary and bool(_COMMAND_ZONE_CREATURE_RE.search(text))
    structurally_eligible = (is_legendary and (has_printed_toughness or is_background)) or command_zone_creature
    banned_as_commander = (card.get("legalities") or {}).get("commander") == "banned"
    return (structurally_eligible or grants_eligibility) and not banned_as_commander and not _is_meld_result(card)


def _has_color_indicator(card: dict[str, Any]) -> bool:
    """True if the card (or any face) carries a printed color indicator (`has:indicator`).

    A card-level characteristic (the ability granting it doesn't change by printing), unlike
    the rest of this module's printing-booleans -- verified close to live `has:indicator`
    (366 here vs. 362 live, 2026-10-03; docs/is-tags.md).
    """
    if card.get("color_indicator"):
        return True
    return any(face.get("color_indicator") for face in card.get("card_faces") or [])


# Card types that are never cast as spells: live `-is:spell` (1322 cards, 2026-10-04) is every land plus these.
_NON_SPELL_TYPES = frozenset({"Land", "Attraction", "Contraption", "Stickers", "Conspiracy", "Dungeon"})


def _front_type_line(card: dict[str, Any]) -> str:
    """The front face's type line (a reversible_card printing has none at the top level)."""
    faces = card.get("card_faces") or []
    return (faces[0].get("type_line") if faces else None) or card.get("type_line") or ""


def _is_spell(card: dict[str, Any]) -> bool:
    """`is:spell`: the front face is not a land, Attraction, Contraption, Stickers, Conspiracy or Dungeon.

    Spell // Land modal DFCs count as spells (front face is the spell). A Land // Adventure card (Midgar, City of
    Mako) is castable as its adventure, so Scryfall counts it too.
    """
    if card.get("layout") == "adventure":
        return True
    type_line = _front_type_line(card)
    return not _NON_SPELL_TYPES & set(type_line.replace("—", " ").split())


def _is_scryfall_card_preview(card: dict[str, Any]) -> bool:
    """Previewed on a Scryfall card page.

    Scryfall's is:scryfallpreview is 6 cards; the 321 Secret Lair printings whose preview source is also "Scryfall"
    link a set page (/sets/slz?order=spoiled) and do not count. The 93 with no link at all (mostly slz) do not
    either: counting them gave 35 cards vs 6 (tried 2026-10-04). Live also lists Dig Through Time and Goblin
    Cratermaker, whose bulk previews are link-less or absent; we stay 2 under.
    """
    preview = card.get("preview") or {}
    return preview.get("source") == "Scryfall" and "scryfall.com/card/" in (preview.get("source_uri") or "")


IS_TAG_CHECKS: dict[str, Any] = {
    "arena_league": lambda c, *_: "arenaleague" in _promo_types(c),
    "booster": lambda c, *_: bool(c.get("booster")),
    "buyabox": lambda c, *_: "buyabox" in _promo_types(c),
    "commander": lambda c, _mana_cost_text, oracle_text: _is_commander_eligible(c, oracle_text),
    "convention": lambda c, *_: "convention" in _promo_types(c),
    "datestamped": lambda c, *_: "datestamped" in _promo_types(c),
    "etched": lambda c, *_: "etched" in _finishes(c),
    "fnm": lambda c, *_: "fnm" in _promo_types(c),
    "foil": lambda c, *_: bool(c.get("foil")),
    "full": lambda c, *_: bool(c.get("full_art")),
    "gamechanger": lambda c, *_: bool(c.get("game_changer")),
    "gameday": lambda c, *_: "gameday" in _promo_types(c),
    "giftbox": lambda c, *_: "giftbox" in _promo_types(c),
    "glossy": lambda c, *_: "glossy" in _promo_types(c),
    "hires": lambda c, *_: bool(c.get("highres_image")),
    "hybrid": lambda c, *_: bool(_HYBRID_MANA_RE.search(_front_face_mana_cost(c))),
    "indicator": lambda c, *_: _has_color_indicator(c),
    "instore": lambda c, *_: "instore" in _promo_types(c),
    "intro_pack": lambda c, *_: "intropack" in _promo_types(c),
    "judge_gift": lambda c, *_: "judgegift" in _promo_types(c),
    "league": lambda c, *_: "league" in _promo_types(c),
    "masterpiece": lambda c, *_: c.get("set_type") == "masterpiece",
    "media_insert": lambda c, *_: "mediainsert" in _promo_types(c),
    "nonfoil": lambda c, *_: bool(c.get("nonfoil")),
    "partner": lambda c, *_: _is_partner(c),
    "phyrexian": lambda _c, mana_cost_text, oracle_text: bool(
        _PHYREXIAN_MANA_RE.search((mana_cost_text or "") + (oracle_text or ""))
    ),
    "planeswalker_deck": lambda c, *_: "planeswalkerdeck" in _promo_types(c),
    "player_rewards": lambda c, *_: "playerrewards" in _promo_types(c),
    "prerelease": lambda c, *_: "prerelease" in _promo_types(c),
    "promo": lambda c, *_: bool(c.get("promo")),
    "release": lambda c, *_: "release" in _promo_types(c),
    "reprint": lambda c, *_: bool(c.get("reprint")),
    "reserved": lambda c, *_: bool(c.get("reserved")),
    "scryfallpreview": lambda c, *_: _is_scryfall_card_preview(c),
    "set_promo": lambda c, *_: "setpromo" in _promo_types(c),
    "spell": lambda c, *_: _is_spell(c),
    "spotlight": lambda c, *_: bool(c.get("story_spotlight")),
    "universesbeyond": lambda c, *_: "universesbeyond" in _promo_types(c),
}


# Tags that read printing fields (promo types, finishes, frame, set type, preview). With a
# default_cards file these are evaluated on every printing and unioned per oracle card, which is
# Scryfall's own rule (a card matches when ANY printing matches). The rest are card-level.
PRINTING_IS_TAGS = frozenset(IS_TAG_CHECKS) - {
    "commander",
    "gamechanger",
    "hybrid",
    "indicator",
    "partner",
    "phyrexian",
    "reserved",
    "spell",
}


@dataclass
class PrintingSummary:
    """What every printing of one oracle card adds, split by default visibility.

    Scryfall's default search ignores hidden printings (memorabilia, playtest, ...): parity 2026-10-03, unioning them
    made is:instore 126 vs 116. A card with no visible printing at all keeps the hidden ones' tags (include:extras).
    """

    visible_tags: set[str] = field(default_factory=set)
    hidden_tags: set[str] = field(default_factory=set)
    visible_frames: set[str] = field(default_factory=set)
    hidden_frames: set[str] = field(default_factory=set)
    any_visible: bool = False

    @property
    def is_tags(self) -> set[str]:
        """The tags this card gets: the visible printings', or all of them when none is visible."""
        return self.visible_tags if self.any_visible else self.hidden_tags

    @property
    def frame_data(self) -> set[str]:
        """Frame versions and effects of those printings (is:old, is:new and frame: match any printing)."""
        return self.visible_frames if self.any_visible else self.hidden_frames

    def add(self, printing: dict[str, Any]) -> None:
        """Fold one printing in."""
        visible = not _is_extra(printing)
        self.any_visible = self.any_visible or visible
        tags = self.visible_tags if visible else self.hidden_tags
        (self.visible_frames if visible else self.hidden_frames).update(_frame_data_array(printing))
        tags.update(tag for tag in PRINTING_IS_TAGS if IS_TAG_CHECKS[tag](printing, None, None))


def summarize_printings(printings_path: str | Path) -> dict[str, PrintingSummary]:
    """Stream a default_cards file once into one small `PrintingSummary` per oracle_id (no printing is kept)."""
    summaries: dict[str, PrintingSummary] = {}
    for printing in _open_jsonl(printings_path):
        for oracle_id in _printing_oracle_ids(printing):
            summaries.setdefault(oracle_id, PrintingSummary()).add(printing)
    return summaries


def _printing_oracle_ids(printing: dict[str, Any]) -> set[str]:
    """The oracle ids a printing belongs to: its own, or (reversible_card, 83 in 2026-10-03) its faces'."""
    if printing.get("oracle_id"):
        return {printing["oracle_id"]}
    return {face["oracle_id"] for face in printing.get("card_faces") or [] if face.get("oracle_id")}


def _open_jsonl(path: str | Path) -> Iterator[dict[str, Any]]:
    """Yield decoded JSON objects from a gzipped or plain JSONL file, one per non-blank line."""
    path = Path(path)
    opener = gzip.open if path.suffix == ".gz" else Path.open
    with opener(path, mode="rt", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped:
                yield json.loads(stripped)


def _card_faces(card: dict[str, Any]) -> list[dict[str, Any]]:
    return card.get("card_faces") or []


def _union_types_and_subtypes(card: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Union card_types/card_subtypes over every face, parsing each face's own type line.

    The combined top-level type_line ("Legendary Creature — Human Wizard // Legendary
    Planeswalker — Jace") can't be parsed as one line: parse_type_line only splits on the first
    em dash, so a face-at-a-time parse is required for any multi-face card.
    """
    faces = _card_faces(card)
    type_lines = [face.get("type_line", "") for face in faces] if faces else [card.get("type_line", "")]
    types: list[str] = []
    subtypes: list[str] = []
    for type_line in type_lines:
        face_types, face_subtypes = parse_type_line(type_line or "")
        for t in face_types:
            if t not in types:
                types.append(t)
        for st in face_subtypes:
            if st not in subtypes:
                subtypes.append(st)
    return types, subtypes


def _combined_oracle_text(card: dict[str, Any]) -> str | None:
    if card.get("oracle_text") is not None:
        return card["oracle_text"]
    faces = _card_faces(card)
    if not faces:
        return None
    return "\n//\n".join(face.get("oracle_text") or "" for face in faces)


def _strip_reminder_text(text: str | None) -> str | None:
    """Oracle text with every parenthesised reminder span removed, matching Scryfall's `o:`.

    Scryfall's docs (scryfall.com/docs/syntax, checked 2026-10-03): "This keyword [o:] usually
    checks the current Oracle text for cards... Use the fo: or fulloracle: operator to search the
    full Oracle text, which includes reminder text." So `o:` excludes it. Real example: Barren
    Moor's only mention of "draw a card" is inside its cycling reminder ("Cycling {B} ({B},
    Discard this card: Draw a card.)") -- Scryfall's `o:draw` does not return it (cached
    tests/fixtures/scryfall/ responses only list it as `only_ours` in scripts/parity.py's output
    before this fix), confirming the docs' claim against real search results, not just the text.
    """
    if text is None:
        return None
    stripped = text
    previous = None
    while stripped != previous:
        previous = stripped
        stripped = _PAREN_SPAN_RE.sub("", stripped)
    stripped = _RUN_OF_SPACES_RE.sub(" ", stripped)
    stripped = _SPACE_AROUND_NEWLINE_RE.sub("\n", stripped)
    return stripped.strip()


def _combined_mana_cost_text(card: dict[str, Any]) -> str | None:
    if card.get("mana_cost") is not None:
        return card["mana_cost"]
    faces = _card_faces(card)
    if not faces:
        return None
    return " // ".join(face.get("mana_cost") or "" for face in faces)


def _front_face_mana_cost(card: dict[str, Any]) -> str:
    """The mana cost `mana_cost_jsonb`/devotion are computed from: the front face's own cost."""
    faces = _card_faces(card)
    if faces:
        return faces[0].get("mana_cost") or ""
    return card.get("mana_cost") or ""


def _card_colors(card: dict[str, Any]) -> list[str]:
    if card.get("colors") is not None:
        return card["colors"]
    faces = _card_faces(card)
    colors: list[str] = []
    for face in faces:
        for color in face.get("colors") or []:
            if color not in colors:
                colors.append(color)
    return colors


def _mask(colors: Iterable[str] | None) -> int:
    """Colour letters to a bitmask (schema.COLOR_BITS), silently dropping anything outside WUBRGC.

    Scryfall's produced_mana is occasionally a joke value from an Un-set card (e.g. "Sole
    Performer" produces ["T"], the only non-WUBRGC value in the whole 2026-10-03 file) that this
    bitmask schema has no bit for and no `produces:` syntax could ever ask about anyway.
    """
    if colors is None:
        return 0
    mask = 0
    for color in colors:
        mask |= COLOR_BITS.get(color, 0)
    return mask


def _legal_somewhere(card: dict[str, Any]) -> bool:
    return any(status in ("legal", "restricted") for status in (card.get("legalities") or {}).values())


# Un-sets Scryfall's default search still shows although every card is legal nowhere (Unglued,
# Unhinged, Unstable, Unsanctioned, Unfinity, and the Ponies promo set). Live probe 2026-10-03,
# one card per funny set: tests/fixtures/scryfall/funny_set_default_visibility.json. Every other
# funny set (playtest cards, Happy Holidays, Heroes of the Realm, HasCon, ...) is hidden.
_VISIBLE_FUNNY_SETS = frozenset({"ugl", "unh", "ust", "und", "unf", "ptg"})


def _is_playtest_or_funny(card: dict[str, Any]) -> bool:
    return card.get("set_type") == "funny" or "playtest" in _promo_types(card)


# Games whose printings Scryfall's default search shows; the other games (astral, sega) are hidden.
_PLAYABLE_GAMES = frozenset({"paper", "mtgo", "arena"})

# Types of game objects that are not cards you play. Scryfall hides these when they are legal in no format
# (counters, Role tokens, Secret Lair mana cards, sticker sheets); Dungeons, which have their own type, stay visible.
_NON_CARD_TYPE_LINES = ("Card", "Stickers")


def _is_non_card_object(card: dict[str, Any]) -> bool:
    type_line = _front_type_line(card)
    return type_line in _NON_CARD_TYPE_LINES or type_line.startswith("Token")


def _is_dungeon(card: dict[str, Any]) -> bool:
    return "Dungeon" in _front_type_line(card)


def _is_extra(card: dict[str, Any]) -> bool:
    """True for what Scryfall's own default search hides (see `_HIDDEN_LAYOUTS` above).

    A funny-set or playtest card (the same live probe: mb2 and pf24-26 playtest promos are hidden
    though not funny) is hidden when it is legal in no format and not from `_VISIBLE_FUNNY_SETS`.
    Parity 2026-10-03: hiding every funny card made `legal:commander` 31942 vs Scryfall's 32116
    (the missing 174 were exactly the funny cards legal in commander, e.g. Atomwheel Acrobats,
    Celebr-8000), and hiding the Un-set ones too made `t:creature cmc<=2` 5000 vs 5071. Cards
    with `content_warning` (7 in the file) are hidden as well, and so are Alchemy cards legal nowhere (the 104 hbg cards: t:elf was 713 vs 698, t:dragon 449 vs 444).

    Parity 2026-10-04 (is:hires, is:nonfoil and is:spell lists against the live site): 35 more cards we showed and
    Scryfall hides, every one legal nowhere and either digital (Astral `past`, Sega `psdg`, the mtgo Gleemox promo),
    typed "Card"/"Stickers"/"Token ..." (counters, Role tokens, Secret Lair mana cards), or only in hidden printings;
    no shown card fit. A Dungeon in a double_faced_token layout (Undercity) is shown. Printings that exist
    only in another game (Astral `past`, Sega `psdg`) are hidden even for a legal card (Arden Angel's psdg printing
    was a phantom is:nonfoil), silver-border promo printings legal nowhere (the pal04 promos of Un-cards: is:arena_league 46 vs 40;
    the silver Secret Lair ponies stay visible), and so are `variation` printings (include:variations shows them); non-English printings still count (frame:1997 lists Hornet Queen via a French one).
    """
    hidden_funny = _is_playtest_or_funny(card) and not _legal_somewhere(card) and card.get("set") not in _VISIBLE_FUNNY_SETS
    hidden_alchemy = card.get("set_type") == "alchemy" and not _legal_somewhere(card)
    hidden_oddity = not _legal_somewhere(card) and (bool(card.get("digital")) or _is_non_card_object(card))
    hidden_layout = card.get("layout") in _HIDDEN_LAYOUTS and not _is_dungeon(card)
    hidden_silver = card.get("set_type") == "promo" and card.get("border_color") == "silver" and not _legal_somewhere(card)
    hidden_game = not _PLAYABLE_GAMES.intersection(card.get("games") or _PLAYABLE_GAMES)
    return bool(
        card.get("content_warning")
        or hidden_funny
        or hidden_alchemy
        or hidden_oddity
        or hidden_layout
        or hidden_silver
        or hidden_game
        or card.get("variation")
        or card.get("set_type") == "memorabilia"
    )


def _is_tags(
    card: dict[str, Any], mana_cost_text: str | None, oracle_text: str | None, summary: PrintingSummary | None = None
) -> list[str]:
    tags = {tag for tag, check in IS_TAG_CHECKS.items() if check(card, mana_cost_text, oracle_text)}
    return sorted(tags | summary.is_tags if summary else tags)


def _trim_images(image_uris: dict[str, str] | None) -> dict[str, str] | None:
    return {size: uri for size, uri in image_uris.items() if size in _KEEP_IMAGE_SIZES} if image_uris else None


def _trim_face(face: dict[str, Any]) -> dict[str, Any]:
    trimmed = {key: value for key, value in face.items() if key in _KEEP_FACE_FIELDS}
    if images := _trim_images(face.get("image_uris")):
        trimmed["image_uris"] = images
    return trimmed


def _trim_card_json(card: dict[str, Any]) -> dict[str, Any]:
    """Scryfall's card object cut down to the fields Purroxy reads (see _KEEP_CARD_FIELDS)."""
    trimmed = {key: value for key, value in card.items() if key in _KEEP_CARD_FIELDS}
    if images := _trim_images(card.get("image_uris")):
        trimmed["image_uris"] = images
    if card.get("card_faces"):
        trimmed["card_faces"] = [_trim_face(face) for face in card["card_faces"]]
    if card.get("all_parts"):
        trimmed["all_parts"] = [{k: v for k, v in part.items() if k in _KEEP_PART_FIELDS} for part in card["all_parts"]]
    if previewed_at := (card.get("preview") or {}).get("previewed_at"):
        trimmed["preview"] = {"previewed_at": previewed_at}
    return trimmed


def _frame_data_array(card: dict[str, Any]) -> list[str]:
    return sorted(extract_frame_data_from_raw_card(card).keys())


def _build_card_row(
    card: dict[str, Any],
    oracle_id_to_tag_slugs: dict[str, list[str]],
    summary: PrintingSummary | None = None,
) -> dict[str, Any]:
    card_types, card_subtypes = _union_types_and_subtypes(card)
    oracle_text = _combined_oracle_text(card)
    mana_cost_text = _combined_mana_cost_text(card)
    front_mana_cost = _front_face_mana_cost(card)
    is_permanent = bool(_PERMANENT_CARD_TYPES & set(card_types))
    faces = _card_faces(card)
    prices = card.get("prices") or {}
    rarity = card.get("rarity")
    set_code = card.get("set")

    return {
        "oracle_id": card["oracle_id"],
        "card_name": card["name"],
        "card_name_folded": fold_accents(card["name"].lower()),
        "name_sort_key": _NON_ALNUM_RE.sub("", fold_accents(card["name"].lower())),
        "type_line": card.get("type_line"),
        "card_types": json.dumps(card_types),
        "card_subtypes": json.dumps(card_subtypes),
        "oracle_text": oracle_text,
        "oracle_text_search": _strip_reminder_text(oracle_text),
        "flavor_text": card.get("flavor_text") or "",
        "mana_cost_text": mana_cost_text,
        "mana_cost_jsonb": json.dumps(mana_cost_str_to_dict(front_mana_cost)),
        "devotion": json.dumps(calculate_devotion(front_mana_cost) if is_permanent else {}),
        "cmc": maybe_float(card.get("cmc")),
        "creature_power": None if faces else maybe_float(card.get("power")),
        "creature_toughness": None if faces else maybe_float(card.get("toughness")),
        "planeswalker_loyalty": None if faces else maybe_float(card.get("loyalty")),
        "card_colors": _mask(_card_colors(card)),
        "card_color_identity": _mask(card.get("color_identity")),
        "produced_mana": _mask(card.get("produced_mana")),
        "card_keywords": json.dumps(sorted({kw.lower() for kw in card.get("keywords") or []})),
        "card_oracle_tags": json.dumps(oracle_id_to_tag_slugs.get(card["oracle_id"], [])),
        "card_art_tags": json.dumps([]),
        "card_is_tags": json.dumps(_is_tags(card, mana_cost_text, oracle_text, summary)),
        "card_legalities": json.dumps(card.get("legalities") or {}),
        "card_rarity_int": rarity_text_to_int(rarity) if rarity else None,
        "card_set_code": set_code.lower() if isinstance(set_code, str) else set_code,
        "collector_number": card.get("collector_number"),
        "collector_number_int": extract_collector_number_int(card.get("collector_number")),
        "card_layout": card.get("layout").lower() if isinstance(card.get("layout"), str) else None,
        "card_border": card.get("border_color").lower() if isinstance(card.get("border_color"), str) else None,
        "card_watermark": card.get("watermark").lower() if isinstance(card.get("watermark"), str) else None,
        "card_frame_data": json.dumps(sorted(set(_frame_data_array(card)) | (summary.frame_data if summary else set()))),
        "card_artist": card.get("artist"),
        "released_at": card.get("released_at"),
        "edhrec_rank": card.get("edhrec_rank"),
        "price_usd": maybe_float(prices.get("usd")),
        "price_eur": maybe_float(prices.get("eur")),
        "price_tix": maybe_float(prices.get("tix")),
        "game_changer": 1 if card.get("game_changer") else 0,
        "is_extra": 1 if (not summary.any_visible if summary else _is_extra(card)) else 0,
        "card_json": json.dumps(_trim_card_json(card)),
    }


def _build_face_rows(card_id: int, card: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for index, face in enumerate(_card_faces(card), start=1):
        rows.append(
            {
                "card_id": card_id,
                "face_index": index,
                "face_name": face.get("name"),
                "face_type_line": face.get("type_line"),
                "face_oracle_text": face.get("oracle_text"),
                "face_mana_cost": face.get("mana_cost"),
                "creature_power": maybe_float(face.get("power")),
                "creature_toughness": maybe_float(face.get("toughness")),
                "planeswalker_loyalty": maybe_float(face.get("loyalty")),
                "face_colors": _mask(face.get("colors")) if face.get("colors") is not None else None,
            }
        )
    return rows


def _build_uuid_to_slug(tags: list[dict[str, Any]]) -> dict[str, str]:
    """Ported verbatim from api.tag_import._build_uuid_to_slug.

    Not imported directly: api.tag_import pulls in psycopg and the live Scryfall fetcher (for its
    Postgres-writing callers), neither of which this SQLite importer needs or has installed.
    """
    return {tag["id"]: tag["slug"] for tag in tags}


def _build_all_ancestors(tags: list[dict[str, Any]], uuid_to_slug: dict[str, str]) -> dict[str, frozenset[str]]:
    """Ported verbatim from api.tag_import._build_all_ancestors (see the note on `_build_uuid_to_slug`).

    Returns a map from each slug to the set of all its ancestor slugs (parents, grandparents,
    etc.) -- a search for a parent tag should match cards tagged with any descendant, achieved by
    storing all ancestor slugs on each card at import time.
    """
    slug_to_parent_slugs: dict[str, set[str]] = {}
    for tag in tags:
        slug = uuid_to_slug.get(tag["id"])
        if not slug:
            continue
        slug_to_parent_slugs[slug] = {uuid_to_slug[pid] for pid in tag.get("parent_ids", []) if pid in uuid_to_slug}

    result: dict[str, frozenset[str]] = {}
    for slug in slug_to_parent_slugs:
        if slug in result:
            continue
        ancestors: set[str] = set()
        queue = list(slug_to_parent_slugs.get(slug, set()))
        visited: set[str] = {slug}
        while queue:
            current = queue.pop()
            if current in visited:
                continue
            visited.add(current)
            ancestors.add(current)
            queue.extend(slug_to_parent_slugs.get(current, set()) - visited)
        result[slug] = frozenset(ancestors)

    return result


def _oracle_id_to_tag_slugs(tags_path: str | Path) -> dict[str, list[str]]:
    """Map each tagged card's oracle_id to its tag slugs plus every ancestor tag's slug.

    Reuses Sylvan's ancestor-propagation walk (`_build_uuid_to_slug`/`_build_all_ancestors` above,
    ported from api.tag_import -- see their docstrings); only the "which cards does each tag
    touch" loop below is new, because Sylvan's version updates Postgres rows directly instead of
    returning a plain dict.
    """
    tags = list(_open_jsonl(tags_path))
    uuid_to_slug = _build_uuid_to_slug(tags)
    all_ancestors = _build_all_ancestors(tags, uuid_to_slug)

    oracle_id_to_tags: dict[str, set[str]] = {}
    for tag in tags:
        slug = tag["slug"]
        for tagging in tag.get("taggings", []):
            oracle_id = tagging.get("oracle_id")
            if not oracle_id:
                continue
            card_tags = oracle_id_to_tags.setdefault(oracle_id, set())
            card_tags.add(slug)
            card_tags.update(all_ancestors.get(slug, frozenset()))

    return {oracle_id: sorted(slugs) for oracle_id, slugs in oracle_id_to_tags.items()}


_CARD_COLUMNS = [
    "oracle_id",
    "card_name",
    "card_name_folded",
    "name_sort_key",
    "type_line",
    "card_types",
    "card_subtypes",
    "oracle_text",
    "oracle_text_search",
    "flavor_text",
    "mana_cost_text",
    "mana_cost_jsonb",
    "devotion",
    "cmc",
    "creature_power",
    "creature_toughness",
    "planeswalker_loyalty",
    "card_colors",
    "card_color_identity",
    "produced_mana",
    "card_keywords",
    "card_oracle_tags",
    "card_art_tags",
    "card_is_tags",
    "card_legalities",
    "card_rarity_int",
    "card_set_code",
    "collector_number",
    "collector_number_int",
    "card_layout",
    "card_border",
    "card_watermark",
    "card_frame_data",
    "card_artist",
    "released_at",
    "edhrec_rank",
    "price_usd",
    "price_eur",
    "price_tix",
    "game_changer",
    "is_extra",
    "card_json",
]

_CARD_INSERT_SQL = f"INSERT INTO cards ({', '.join(_CARD_COLUMNS)}) VALUES ({', '.join('?' for _ in _CARD_COLUMNS)})"
_FACE_COLUMNS = [
    "card_id",
    "face_index",
    "face_name",
    "face_type_line",
    "face_oracle_text",
    "face_mana_cost",
    "creature_power",
    "creature_toughness",
    "planeswalker_loyalty",
    "face_colors",
]
_FACE_INSERT_SQL = f"INSERT INTO card_faces ({', '.join(_FACE_COLUMNS)}) VALUES ({', '.join('?' for _ in _FACE_COLUMNS)})"


def build(
    cards_path: str | Path,
    tags_path: str | Path,
    out_path: str | Path,
    printings_path: str | Path | None = None,
) -> dict[str, Any]:
    """Build the SQLite card file at `out_path` from the two bulk JSONL files.

    Writes to `out_path` + ".tmp" and renames it into place at the end, so a build that raises
    partway through never leaves a half-written file at `out_path`.

    With `printings_path` (default_cards) every printing is streamed once and folded into its oracle card's row:
    the union of printing-level `is:` tags and "visible if any printing is" (`summarize_printings`). Without it
    only the representative printing counts.

    Returns a stats dict: card_count, face_count, tagged_card_count, duration_seconds.
    """
    start = time.monotonic()
    out_path = Path(out_path)
    tmp_path = out_path.with_name(out_path.name + ".tmp")
    tmp_path.unlink(missing_ok=True)

    oracle_id_to_tag_slugs = _oracle_id_to_tag_slugs(tags_path)
    summaries = summarize_printings(printings_path) if printings_path else {}

    card_count = 0
    face_count = 0
    try:
        conn = sqlite3.connect(tmp_path)
        try:
            create_schema(conn)
            for card in _open_jsonl(cards_path):
                row = _build_card_row(card, oracle_id_to_tag_slugs, summaries.get(card["oracle_id"]))
                cursor = conn.execute(_CARD_INSERT_SQL, [row[c] for c in _CARD_COLUMNS])
                card_id = cursor.lastrowid
                face_rows = _build_face_rows(card_id, card)
                conn.executemany(_FACE_INSERT_SQL, [[face[c] for c in _FACE_COLUMNS] for face in face_rows])
                card_count += 1
                face_count += len(face_rows)
            conn.execute(
                "INSERT INTO meta(key, value) VALUES ('card_count', ?), ('built_at', ?)",
                (str(card_count), time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
            )
            conn.commit()
        finally:
            conn.close()
        tmp_path.rename(out_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    return {
        "card_count": card_count,
        "face_count": face_count,
        "tagged_card_count": len(oracle_id_to_tag_slugs),
        "duration_seconds": round(time.monotonic() - start, 2),
    }


# A full oracle_cards file has ~38,700 cards (2026-10-03); a build well under this is almost
# certainly from a truncated or empty source file, not a legitimately small catalog.
_MIN_FULL_BUILD_CARD_COUNT = 30_000

# A handful of well-known, long-stable oracle cards; their absence means the import silently
# dropped rows rather than that Scryfall renamed or retired them.
_KNOWN_CARD_NAMES = ("Black Lotus", "Sol Ring", "Lightning Bolt", "Counterspell")


def check(db_path: str | Path) -> list[str]:
    """Problems found in the database at `db_path`. Empty means good. Run before publishing a build."""
    problems: list[str] = []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError as exc:
        return [f"cannot open database: {exc}"]

    try:
        (card_count,) = conn.execute("SELECT COUNT(*) FROM cards").fetchone()
    except sqlite3.DatabaseError as exc:
        return [f"cannot read cards table: {exc}"]

    if card_count < _MIN_FULL_BUILD_CARD_COUNT:
        problems.append(f"only {card_count} cards, expected at least {_MIN_FULL_BUILD_CARD_COUNT} for a full build")

    for name in _KNOWN_CARD_NAMES:
        (found,) = conn.execute("SELECT COUNT(*) FROM cards WHERE card_name = ?", (name,)).fetchone()
        if not found:
            problems.append(f"known card {name!r} is missing")

    (faceless_multi_face,) = conn.execute(
        """
        SELECT COUNT(*) FROM cards
        WHERE card_name LIKE '% // %'
          AND id NOT IN (SELECT DISTINCT card_id FROM card_faces)
        """
    ).fetchone()
    if faceless_multi_face:
        problems.append(f"{faceless_multi_face} multi-face card(s) have no rows in card_faces")

    conn.close()
    return problems


def main() -> None:
    """CLI: `python -m oracle_searcher.importer --cards X --tags Y --out Z`."""
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cards", required=True, help="path to oracle_cards.jsonl(.gz)")
    parser.add_argument("--tags", required=True, help="path to oracle_tags.jsonl(.gz)")
    parser.add_argument("--printings", help="path to default_cards.jsonl(.gz); optional")
    parser.add_argument("--out", required=True, help="path to write the SQLite card file to")
    args = parser.parse_args()

    stats = build(args.cards, args.tags, args.out, args.printings)
    logger.info("Built %s: %s", args.out, stats)

    problems = check(args.out)
    if problems:
        for problem in problems:
            logger.error("check failed: %s", problem)
        msg = f"{len(problems)} problem(s) found in {args.out}; see log above"
        raise SystemExit(msg)
    logger.info("check passed: %s", args.out)


if __name__ == "__main__":
    main()
