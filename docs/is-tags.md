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
