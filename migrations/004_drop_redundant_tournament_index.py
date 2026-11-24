"""Drop Game's ``tournament_id`` index: the compound tournament indexes start
with it, and beanie never drops indexes a model stopped declaring."""

import asyncio

from app import db
from app.models import Game

OLD_INDEX = "tournament_id_1"


async def main() -> None:
    client = await db.connect()
    try:
        collection = Game.get_pymongo_collection()
        existing = await collection.index_information()
        if OLD_INDEX not in existing:
            print(f"{OLD_INDEX} is already gone, nothing to do")
            return
        await collection.drop_index(OLD_INDEX)
        print(f"dropped {OLD_INDEX}")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
