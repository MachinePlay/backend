"""Mark tournaments from before the rated checkbox, and their games, rated. The
backend rates their ended games when it next starts."""

import asyncio

from app import db
from app.models import Game, Tournament


async def main() -> None:
    client = await db.connect()
    try:
        tours = await Tournament.get_pymongo_collection().update_many(
            {"rated": {"$exists": False}}, {"$set": {"rated": True}}
        )
        games = await Game.get_pymongo_collection().update_many(
            {"tournament_id": {"$ne": None}, "rated": {"$exists": False}},
            {"$set": {"rated": True}},
        )
        print(
            f"marked {tours.modified_count} tournament(s) and "
            f"{games.modified_count} game(s) rated"
        )
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
