"""Opening books: where a game's starting line comes from.

Two deterministic engines given the same start position play the same game
every time, so a tournament between them is one game replayed N times — the
"forty draws out of forty" the starter tutorial uses to show a broken
experiment. Dealing each game a different opening is the standard fix.

The books themselves ship with the backend as files under `books/` and are
seeded into Mongo on startup; `seed()` is idempotent, so a wiped database (or a
test's clean slate) refills itself. Everything downstream refers to a book by
`slug` rather than document id, since a re-seed preserves the former.

The shipped file is a Polyglot `.bin`, which is an *engine* book — a map from
position to weighted moves, meant to be consulted move by move during play.
fastchess cannot use one (it takes only `-openings format=(epd|pgn)`), and it
isn't what we want anyway: what we want is the set of lines the book can reach
in its first few plies, which is a plain walk of its own entries.
"""

import logging
import random
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import chess
import chess.polyglot

from app.exceptions import NotFoundError
from app.models import Book

logger = logging.getLogger(__name__)

BOOKS_DIR = Path(__file__).resolve().parent.parent / "books"

# How deep a line is walked: eight plies (four moves a side) is deep enough to
# reach a real opening and shallow enough to leave the engines a game to play.
PLIES = 8

# Shortest line worth starting from. Below this the engines are effectively
# still on move one and the book has bought nothing.
MIN_PLIES = 4

# The book a tournament (or a one-off game) uses unless told otherwise. Playing
# from the initial position is the explicit choice, not the default: with
# deterministic engines it makes a tournament one game replayed N times.
DEFAULT_SLUG = "perfect2021"

# What a caller passes instead of a slug to play from the initial position. A
# sentinel rather than null so "no book" and "field omitted" stay distinct —
# omitting it has to mean the default.
NO_BOOK = "none"


def enumerate_polyglot(path: Path, plies: int) -> list[tuple[str, ...]]:
    """Every line the Polyglot book at `path` reaches within `plies`.

    Exhaustive rather than sampled: the walk only follows the book's own
    entries, so it terminates by itself, is fast (~60ms for a 5000-entry book
    at 8 plies), and gives the same complete set every time — where sampling
    gave a smaller, seed-dependent subset. Entry weights are ignored: they
    order an engine's *choice* during play, whereas here every line the book's
    author included is one worth starting from.

    A branch that runs out of book, or reaches a finished position, contributes
    what it got to; the caller drops the ones too short to be openings.
    """
    lines: list[tuple[str, ...]] = []

    def walk(
        reader: chess.polyglot.MemoryMappedReader,
        board: chess.Board,
        line: list[str],
    ) -> None:
        entries = [] if len(line) == plies else list(reader.find_all(board))
        if not entries or board.is_game_over():
            lines.append(tuple(line))
            return
        for entry in entries:
            board.push(entry.move)
            line.append(entry.move.uci())
            walk(reader, board, line)
            line.pop()
            board.pop()

    with chess.polyglot.open_reader(path) as reader:
        walk(reader, chess.Board(), [])
    return lines


@dataclass(frozen=True)
class ShippedBook:
    """One book as it ships: its identity plus how to build its lines."""

    slug: str
    name: str
    description: str
    source: str
    path: Path

    def lines(self) -> list[list[str]]:
        walked = enumerate_polyglot(self.path, PLIES)
        return [
            list(line) for line in sorted({ln for ln in walked if len(ln) >= MIN_PLIES})
        ]


SHIPPED = [
    ShippedBook(
        slug="perfect2021",
        name="Perfect 2021",
        description=(
            f"Hand-tuned engine-match openings, walked {PLIES} plies deep. "
            "Mainline theory, deliberately a shade White-favouring."
        ),
        source="Perfect 2021 by Sedat Canbaz (freeware), Polyglot edition.",
        path=BOOKS_DIR / "perfect2021" / "Perfect2021.bin",
    ),
]


@cache
def _lines(slug: str) -> list[list[str]]:
    """A shipped book's lines, walked once per process."""
    return next(b for b in SHIPPED if b.slug == slug).lines()


async def seed() -> None:
    """Insert any shipped book the database doesn't have yet.

    Called on startup (and by the test fixtures, which wipe every collection
    between tests). Existing books are left alone unless their line count has
    changed, which is the cheap way to notice a re-walked book.
    """
    for shipped in SHIPPED:
        lines = _lines(shipped.slug)
        doc = await Book.find_one(Book.slug == shipped.slug)
        if doc is not None and len(doc.lines) == len(lines):
            continue
        if doc is None:
            doc = Book(
                slug=shipped.slug,
                name=shipped.name,
                plies=PLIES,
                description=shipped.description,
                source=shipped.source,
            )
        doc.name = shipped.name
        doc.description = shipped.description
        doc.source = shipped.source
        doc.plies = PLIES
        doc.lines = lines
        await doc.save()
        logger.info("seeded book=%s (%d lines)", shipped.slug, len(lines))


async def get(slug: str) -> Book:
    doc = await Book.find_one(Book.slug == slug)
    if doc is None:
        raise NotFoundError(f"unknown opening book {slug!r}")
    return doc


async def resolve(slug: str) -> Book | None:
    """The requested book, or None for the "play from the initial position"
    sentinel. Raises NotFoundError for anything else."""
    return None if slug == NO_BOOK else await get(slug)


async def list_books() -> list[Book]:
    return await Book.find_all().sort("+slug").to_list()


def order(book: Book, seed_value: int) -> list[list[str]]:
    """The book's lines in one tournament's own order.

    A tournament shuffles once with its stored seed and then deals openings by
    index, so two tournaments of the same shape don't play the same openings in
    the same sequence while either one stays reproducible from its seed.
    """
    shuffled = list(book.lines)
    random.Random(seed_value).shuffle(shuffled)
    return shuffled
