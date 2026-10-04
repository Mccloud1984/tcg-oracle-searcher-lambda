r"""The SQLite card file's schema: the contract between the importer (which writes it) and the search (which reads it).

Tables:
- `cards`: one row per Scryfall oracle card (the `oracle_cards` bulk file), so a search returns each card once, as
  Scryfall's default `unique=cards` does. Column names follow Sylvan Librarian's (`api/parsing/db_info.py`) so its
  parser's attribute names map one to one.
- `card_faces`: one row per face of multi-face cards.
- `meta`: schema version and build metadata.

The printings live in a second file (`PRINTINGS_DDL`), so a search never pays for them: 345 MB with printings in the
cards file against 169 MB without (measured 2026-10-04, close to the Lambda's 512 MB /tmp). The search Lambda
ATTACHes it as `PRINTINGS_ALIAS` on the first op that needs it. Its `printings` table has one row per printing (the
`default_cards` bulk file): id (Scryfall printing id), oracle_id (links to cards.oracle_id), set_code,
collector_number, released_at, set_type, games (JSON array) and card_json (the printing as an overlay on its oracle
card's card_json, see importer.merge_overlay). Indexed on oracle_id and (set_code, collector_number). Its own `meta`
table carries `printings_schema_version`.

Value conventions:
- Colour masks: W=1, U=2, B=4, R=8, G=16; `produced_mana` also uses C=32. Colourless is 0.
- JSON columns hold JSON text (arrays or objects) and are read with SQLite's json functions (`json_each`).
- Card types and subtypes are title-cased, as Sylvan's `parse_type_line` makes them.
- Double-faced and other multi-face cards (`card_faces` in Scryfall's data): a card matches when ANY face matches,
  which is Scryfall's rule. So the card-level text columns hold every face: `oracle_text` is the faces' texts joined
  with "\\n//\\n" when the card has none of its own, and `card_types`/`card_subtypes`/`card_keywords` are the union over
  the faces. Numeric face values (power, toughness, loyalty) live per face in `card_faces`; a numeric predicate
  matches when the card's own value or any face's value matches.
- Prices and numbers that Scryfall gives as strings ("1.50", "*", "X") are stored as REAL when they parse, else NULL.
- `oracle_text_search` holds `oracle_text` with reminder text (the parenthesised explanations) stripped: Scryfall's
  `o:`/`oracle:` searches the current Oracle text WITHOUT reminder text (its own docs: "Use the fo: or fulloracle:
  operator to search the full Oracle text, which includes reminder text" -- scryfall.com/docs/syntax, checked
  2026-10-03), so `o:` compiles against this column, never `oracle_text` itself. `fo:`/`fulloracle:` aren't
  parseable attributes here (Sylvan's hand parser has no alias for them), so `oracle_text` is kept only as the
  full-text source `oracle_text_search` is derived from.
"""

import sqlite3

SCHEMA_VERSION = 3  # the cards file; the printings file has its own PRINTINGS_SCHEMA_VERSION
PRINTINGS_SCHEMA_VERSION = 2
PRINTINGS_ALIAS = "pr"  # the schema name the printings file is ATTACHed under
PRINTINGS_TABLE = f"{PRINTINGS_ALIAS}.printings"  # how queries over the attached file name the table

COLOR_BITS = {"W": 1, "U": 2, "B": 4, "R": 8, "G": 16, "C": 32}

DDL = """
CREATE TABLE cards (
    id                    INTEGER PRIMARY KEY,
    oracle_id             TEXT NOT NULL UNIQUE,
    card_name             TEXT NOT NULL,        -- full name, "Front // Back" for multi-face cards
    card_name_folded      TEXT NOT NULL,        -- lower-cased, accents folded (Sylvan's fold_accents)
    name_sort_key         TEXT NOT NULL,        -- folded name's letters and digits only: Scryfall's name order
    type_line             TEXT,
    card_types            TEXT NOT NULL,        -- JSON array, union over faces
    card_subtypes         TEXT NOT NULL,        -- JSON array, union over faces
    oracle_text           TEXT,                 -- see the module docstring for multi-face cards
    oracle_text_search    TEXT,                 -- oracle_text with reminder text stripped; what o: searches
    flavor_text           TEXT,
    mana_cost_text        TEXT,                 -- as printed, faces joined with " // "
    mana_cost_jsonb       TEXT,                 -- JSON object, Sylvan's mana_cost_str_to_dict of the front face
    devotion              TEXT,                 -- JSON object, Sylvan's calculate_devotion (permanents only)
    cmc                   REAL,
    creature_power        REAL,
    creature_toughness    REAL,
    planeswalker_loyalty  REAL,
    card_colors           INTEGER NOT NULL,     -- mask, union over faces
    card_color_identity   INTEGER NOT NULL,     -- mask
    produced_mana         INTEGER NOT NULL,     -- mask incl. C=32
    card_keywords         TEXT NOT NULL,        -- JSON array, lower-cased
    card_oracle_tags      TEXT NOT NULL,        -- JSON array of tag slugs, with every ancestor tag's slug too
    card_art_tags         TEXT NOT NULL,        -- JSON array (not imported in version 1: always [])
    card_is_tags          TEXT NOT NULL,        -- JSON array, as Sylvan computes is: tags
    card_legalities       TEXT NOT NULL,        -- JSON object {format: "legal"|"not_legal"|"banned"|"restricted"}
    card_rarity_int       INTEGER,              -- Sylvan's rarity_text_to_int
    card_set_code         TEXT,
    collector_number      TEXT,
    collector_number_int  INTEGER,
    card_layout           TEXT,
    card_border           TEXT,
    card_watermark        TEXT,
    card_frame_data       TEXT NOT NULL,        -- JSON array (frame and frame_effects)
    card_artist           TEXT,
    released_at           TEXT,                 -- ISO date of the printing Scryfall chose for this oracle card
    edhrec_rank           INTEGER,
    price_usd             REAL,
    price_eur             REAL,
    price_tix             REAL,
    game_changer          INTEGER NOT NULL DEFAULT 0,
    is_extra              INTEGER NOT NULL DEFAULT 0,  -- 1 for what Scryfall hides unless asked (tokens, emblems, funny cards, ...)
    card_json             TEXT NOT NULL         -- the Scryfall card object, trimmed (see importer), returned as is
);

CREATE TABLE card_faces (
    card_id               INTEGER NOT NULL REFERENCES cards(id),
    face_index            INTEGER NOT NULL,
    face_name             TEXT NOT NULL,
    face_type_line        TEXT,
    face_oracle_text      TEXT,
    face_mana_cost        TEXT,
    creature_power        REAL,
    creature_toughness    REAL,
    planeswalker_loyalty  REAL,
    face_colors           INTEGER,
    PRIMARY KEY (card_id, face_index)
);

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL   -- schema_version, source_updated_at, built_at, card_count
);

CREATE INDEX cards_name ON cards(card_name_folded);
CREATE INDEX cards_visible_name ON cards(is_extra, card_name_folded);  -- the lookups' name matching reads these two only,
CREATE INDEX cards_visible_sort_key ON cards(is_extra, name_sort_key);  -- never the 3 KB rows
CREATE INDEX cards_edhrec ON cards(edhrec_rank);
CREATE INDEX cards_released ON cards(released_at);
CREATE INDEX cards_cmc ON cards(cmc);
CREATE INDEX cards_usd ON cards(price_usd);
CREATE INDEX cards_identity ON cards(card_color_identity);
"""


def create_schema(conn: sqlite3.Connection) -> None:
    """Creates every table and index on an empty database, and records the schema version."""
    conn.executescript(DDL)
    conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))


PRINTINGS_DDL = """
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL   -- printings_schema_version, built_at, printing_count
);

CREATE TABLE printings (
    id                    TEXT PRIMARY KEY,
    oracle_id             TEXT NOT NULL,
    set_code              TEXT,
    collector_number      TEXT,
    released_at           TEXT,
    set_type              TEXT,
    games                 TEXT NOT NULL,        -- JSON array
    is_extra              INTEGER NOT NULL DEFAULT 0,  -- 1 for what Scryfall hides (memorabilia, playtests, variations, etc.)
    card_json             TEXT NOT NULL         -- trimmed Scryfall printing object
);

CREATE INDEX printings_oracle_id ON printings(oracle_id);
CREATE INDEX printings_set_collector ON printings(set_code, collector_number);
"""


def create_printings_schema(conn: sqlite3.Connection) -> None:
    """Creates the printings table, its indexes and meta on an empty database (the second file)."""
    conn.executescript(PRINTINGS_DDL)
    conn.execute("INSERT INTO meta(key, value) VALUES ('printings_schema_version', ?)", (str(PRINTINGS_SCHEMA_VERSION),))
