"""Opening books, so two deterministic engines don't replay one game.

Books ship under `books/` as Polyglot `.bin` files and are seeded into Mongo on
startup. fastchess can't read Polyglot, so we walk each book's first plies into
plain move lines.
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

# Deep enough to reach a real opening, shallow enough to leave a game to play.
PLIES = 8
MIN_PLIES = 4

DEFAULT_SLUG = "perfect2021"

# Not null, so "no book" stays distinct from an omitted field (the default).
NO_BOOK = "none"


def enumerate_polyglot(path: Path, plies: int) -> list[tuple[str, ...]]:
    """Every line the Polyglot book at `path` reaches within `plies`.

    Exhaustive (~60ms at 8 plies) and weight-blind. A branch that runs out of
    book stops short; the caller drops lines too short to be openings.
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
    return next(b for b in SHIPPED if b.slug == slug).lines()


async def seed() -> None:
    """Insert missing shipped books; re-save one whose line count changed."""
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
    """The book, or None for `NO_BOOK`."""
    return None if slug == NO_BOOK else await get(slug)


async def list_books() -> list[Book]:
    return await Book.find_all().sort("+slug").to_list()


def order(book: Book, seed_value: int) -> list[list[str]]:
    """The book's lines shuffled by a tournament's seed: different per
    tournament, reproducible within one."""
    shuffled = list(book.lines)
    random.Random(seed_value).shuffle(shuffled)
    return shuffled
