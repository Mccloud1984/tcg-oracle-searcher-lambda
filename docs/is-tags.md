# `is:` / `has:` inventory

Source: every `is:`/`has:` value on https://scryfall.com/docs/syntax (fetched 2026-10-03), plus the extra
aliases our `api/parsing/rewrite.py` accepts. Kinds:

- **rule**: answered from the card row. Computed once per card at import (`importer.IS_TAG_CHECKS`) and stored in
  `card_is_tags`. Part of `is_tag_rules.KNOWN_IS_TAGS`.
- **otag**: `api/parsing/rewrite.py` rewrites it into other attributes (`otag:`, `t:`, `layout:`, `frame:`, `kw:`, `o:`)
  before the compiler sees it. Never a `card_is_tags` leaf.
- **sweep**: asked of Scryfall (`is_tag_sweep.SWEEP_TAGS`), applied after import with `apply_sweep`. Part of `KNOWN_IS_TAGS`.
- **unsupported**: printing-level; one row per oracle card cannot answer it. The compiler raises `Unsupported`.

The compiler raises `Unsupported` for any `is:`/`has:` value that is neither rewritten nor in `KNOWN_IS_TAGS`
(tests: `tests/test_is_tag_rules.py`).

## Rules (`IS_TAG_CHECKS`)

| Tag | Reason |
|---|---|
| arena_league, buyabox, convention, datestamped, fnm, gameday, giftbox, glossy, instore, judge_gift, league, media_insert, planeswalker_deck, player_rewards, prerelease, release, set_promo | `promo_types` of the card's printing contains it |
| intro_pack | promo type `intropack` |
| universesbeyond | promo type `universesbeyond` |
| booster, foil, nonfoil, full, hires, promo, reprint, reserved | boolean field on the printing |
| etched | `finishes` contains etched |
| gamechanger | `game_changer` flag |
| spotlight | `story_spotlight` flag |
| scryfallpreview | `preview.source` is Scryfall |
| masterpiece | `set_type` is masterpiece |
| commander | front-face check (`_is_commander_eligible`) |
| hybrid, phyrexian | mana-symbol regex on cost (and oracle text for phyrexian) |
| indicator (`has:`) | card has a color indicator |
| partner | legendary card with a partner-like keyword (Partner, Partner with, Friends forever, Choose a background, Doctor's companion), a Background, or a Time Lord Doctor (224 vs live 228; rest are extras we hide) |
| spell | not a permanent type (kept a leaf because Sylvan's tests require it) |

## Sweep (`SWEEP_TAGS`)

| Tag | Reason |
|---|---|
| alchemy, funny | representative printing's `set_type` undercounts |
| digital | representative printing's `games` undercounts (~3.7x) |
| watermark (`has:`) | representative printing often predates the watermark |
| brawler, duelcommander, oathbreaker | per-format eligibility Scryfall already evaluates |
| meldpart, meldresult | tiny (14 / 7); simplest to sweep |
| unique | needs the whole print run |

## Otag / rewrite (`_DERIVED_EXPANSIONS`, `is`)

| Tag | Reason |
|---|---|
| adventure, split, flip, transform, mdfc, meld, leveler, dfc | `layout:` (dfc is the union of the gameplay DFC layouts) |
| colorshifted | `frame:colorshifted` |
| old, new | `frame:` unions |
| historic, permanent, party, outlaw, class, companion | `t:`/`kw:` expressions |
| vanilla | creature with empty oracle text |
| bear | 2/2 creature for 2 (deliberately not Scryfall's exact set) |
| creatureland, manland | oracle-text heuristic |
| shadowland, snarl | reveal-a-basic-land-type text |
| battleland, tangoland | `otag:cycle-tangoland` |
| bikeland, bondland, bounceland, canopyland, checkland, dual, fastland, fetchland, filterland, gainland, painland, pathway, scryland, shockland, slowland, storageland, surveilland, tricycleland, trikeland, triland, triome | `otag:` cycle tag |
| frenchvanilla, modal | community otag |

## Unsupported (printing-level)

| Tag | Reason |
|---|---|
| atypical | frame treatment of a printing (not on an oracle card) |
| default | "default" frame treatment of a printing |

## Extras (`search.EXTRA_REVEALING_IS_TAGS`)

Cards flagged `is_extra` are hidden unless the query names an extra type (`t:token`, ...) or `is:funny`. Live 2026-10-03
(`tests/fixtures/scryfall/is_tag_extras_reveal.json`): `is:funny` is 1476 with and without `include:extras`, so it
reveals. `is:digital` (7154 vs 7386 with extras), `is:alchemy` (824 vs 966) and `is:unique` (16115 vs 20516) keep
hiding extras on Scryfall, so they do not reveal here either. `is:brawler`, `is:oathbreaker` and `has:watermark` have
no hidden cards in either mode. The remaining digital (-2), brawler (-1), duelcommander (-4) and unique (-646) gaps
are not explained by extras gating; not investigated further.

## Parity 2026-10-04 (`scripts/is_tag_parity.py`, full build with `default_cards`, live sweep rerun the same day)

Our count vs Scryfall's `total_cards`, found by diffing card lists (ours vs Scryfall's results paged at 1 request/second).
Fixed in `importer.py` (each with a regression test cut from real bulk rows):

| Tag | Before | After | Cause |
|---|---|---|---|
| spell | +200 | 0 | not a Land front face was too loose: Scryfall's `-is:spell` is every Land plus Attractions, Contraptions, Stickers, Conspiracies and Dungeons, and a Land // Adventure card is a spell |
| old, new | -2831 / -333 | 0 / +1 | `is:old`/`is:new` are `frame:` unions and `card_frame_data` was the representative printing's frame only; it is now the union over every visible printing (also fixes `frame:1993` and friends) |
| full | -23 | +1 | 28 reversible_card printings (83 in the file) have no top-level `oracle_id`, only one per face, and were skipped |
| hires, nonfoil | +23, +30 | 0 | 35 cards Scryfall's default search hides that we showed, all legal nowhere: digital-only (Astral `past`, Sega `psdg`, the mtgo Gleemox), typed `Card`/`Stickers`/`Token ...`, or only in hidden printings. A printing that exists only in Astral or Sega is hidden even for a legal card (Arden Angel). Dungeons stay visible, incl. the double_faced_token Undercity |
| promo, arena_league, release, datestamped, judge_gift | +10, +6, +1, +1, +1 | 0 | silver-border promo printings legal nowhere (the pal04 Un-card promos) are hidden. The shown cards now match Scryfall's 33603 exactly |
| hybrid | +7 | 0 | Prepare-layout cards cost `{B/G}` only on the second face; Scryfall reads the front face |
| commander | +4 | +1 | meld results are never commanders (5 cards); Grist, the Hunger Tide is (a 1/1 creature in the command zone) |

Also: `variation` printings are hidden (Scryfall's `include:variations`), which removes one phantom `frame:2015` match.

Left as they are (not worth a rule, or Scryfall's own behaviour):

- **dfc -2378**: Scryfall's `is:dfc` also counts art_series (2243), double_faced_token (80) and reversible_card (72) entries,
  which are not in our corpus on purpose (the rewrite in `api/parsing/rewrite.py` documents it).
- **has:watermark -49** and **reserved -4** (also permanent -4): these queries make Scryfall show cards its default
  search hides. All 49 missing `has:watermark` cards are extras (tokens, memorabilia, Un-cards) and the 4 `is:reserved`
  cards are the content-warning ones (Cleanse, Imprison, Invoke Prejudice, Jihad; `!"Cleanse"` is 0, `!"Cleanse"
  is:reserved` is 1). Fix is in `search.EXTRA_REVEALING_IS_TAGS` (add `watermark`, `reserved`), not in the importer.
- **foil, full, new, reprint, universesbeyond +1 each, new +1**: one card, Blacker Lotus. Its Secret Lair printing (a
  full-art, foil, reprint, Universes Beyond printing; legal nowhere, borderless) is in none of Scryfall's lists although
  the ugl printing shows the card. No rule found that does not also hide Pinkie Pie (same set type, legal nowhere).
- **scryfallpreview 4 vs 6**: Dig Through Time and Goblin Cratermaker have a link-less or no preview in the bulk file;
  counting link-less previews gives 35. Probably newer than the bulk file.
- **vanilla -11, bear +10, modal -4, party/outlaw -4, creatureland/manland -2, filterland -2, storageland +3,
  gainland +28, frenchvanilla +259**: otag/oracle-text approximations, documented in `api/parsing/rewrite.py`; party and
  outlaw each miss four Zendikar Rising / Baldur's Gate cards that Scryfall tags; not investigated further.
- **frame:2015 +3, frame:2003 +1 (before the pal04 rule)**: Force Spike (j21) and Stonybrook Schoolmaster (yecl) are
  Arena-only printings the live site does not list; no rule found.
