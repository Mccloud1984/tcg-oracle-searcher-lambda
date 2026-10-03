# tcg-oracle-searcher-lambda

A self-hosted card search for trading card games, built to run on AWS Lambda for free-tier cost. It answers searches written in [Scryfall's search syntax](https://scryfall.com/docs/syntax) from a local copy of Scryfall's bulk card data, so an app can search cards without calling Scryfall's API for every query (Scryfall allows `/cards/search` only 2 requests a second).

Version 1 covers Magic: The Gathering. It is being built for [Purroxy](https://purroxy.app), a deck builder, and is public for anyone with the same problem.

## Status

Early work. The plan:

- **Search:** a query parser for Scryfall's syntax, compiled to SQLite. A query it can't answer says so, and the caller asks Scryfall instead; it never guesses.
- **Ordering and paging** as Scryfall does them, with results in Scryfall's card-object shape.
- **Double-faced cards:** a card matches when any of its faces matches.
- **Data:** a nightly import from Scryfall's bulk data files, built into one SQLite file and published to S3.
- **Deployment:** a search Lambda and an import Lambda, with a Terraform module you can use from your own stack.

## Credits

- **Based on [Sylvan Librarian](https://github.com/jbylund/sylvan_librarian)** by Joseph Bylund (ISC licence): an open-source implementation of Scryfall's search. This repo keeps its query parser and card processing, with their history, and replaces the PostgreSQL, Rust engine and web app with SQLite on Lambda. Fixes that apply to Sylvan Librarian are offered back to it.
- **Card data from [Scryfall](https://scryfall.com).** This project is not produced by or endorsed by Scryfall.
- **Magic: The Gathering** is a trademark of Wizards of the Coast LLC. This is unofficial Fan Content permitted under the [Wizards of the Coast Fan Content Policy](https://company.wizards.com/en/legal/fancontentpolicy). It is not approved or endorsed by Wizards. Portions of the materials used are property of Wizards of the Coast. © Wizards of the Coast LLC. See `docs/legal/`.

## Licence

ISC (see `LICENSE`). It keeps Sylvan Librarian's copyright notice, as its licence requires.
