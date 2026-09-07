"""One-off migration: replace fastchess's "normal" reason with the real one.

Finished games recorded before the runner learned to read the termination out
of the PGN carry ``reason: "normal"`` (or nothing) — so the game page showed a
bare ``1/2-1/2`` with no hint of whether it was repetition, the fifty-move rule
or insufficient material. The PGN on each doc still holds the detail in its
final move's comment, so re-derive it with the runner's own parser.

Idempotent: a game whose stored reason already matches what the PGN says is
left untouched.

Run from ``backend/`` (``PYTHONPATH=.`` so ``app`` imports resolve)::

    PYTHONPATH=. uv run python migrations/003_termination_from_pgn.py
"""

import asyncio
import io

import chess.pgn
from machineplay.game import termination
from machineplay.schemas import GameStatus

from app import db
from app.models import Game


async def main() -> None:
    client = await db.connect()
    try:
        changed = 0
        # Only finished games: an aborted one's reason is the backend's own
        # ("runner disconnected"), and tournament retries key off it.
        async for game in Game.find(Game.status == GameStatus.ENDED):
            if not (game.pgn or "").strip():
                continue
            parsed = chess.pgn.read_game(io.StringIO(game.pgn))
            reason = termination(parsed) if parsed else None
            # A PGN without move comments yields nothing — clear the stored
            # "normal" anyway, since it says nothing either.
            if reason == game.reason or (reason is None and game.reason != "normal"):
                continue
            print(f"game {game.id}: {game.reason!r} -> {reason!r}")
            game.reason = reason
            await game.save()
            changed += 1
        print(f"done, {changed} game(s) updated")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
