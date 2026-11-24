"""Trim every stored ``uci_tail`` to the current tail length, then compact so
the freed space goes back to the disk."""

import asyncio

from app import db
from app.models import Game
from app.streaming.streams import Game as LiveGame


async def main() -> None:
    client = await db.connect()
    try:
        collection = Game.get_pymongo_collection()
        n = LiveGame.UCI_TAIL
        result = await collection.update_many(
            {f"uci_tail.{n}": {"$exists": True}},
            [{"$set": {"uci_tail": {"$slice": ["$uci_tail", -n]}}}],
        )
        print(f"trimmed uci_tail to {n} lines on {result.modified_count} game(s)")
        await collection.database.command({"compact": collection.name, "force": True})
        print("compacted")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
