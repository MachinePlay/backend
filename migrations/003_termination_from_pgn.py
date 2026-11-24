"""Replace fastchess's "normal" reason with the termination the PGN's final
move comment records, using the runner's own parser."""

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
