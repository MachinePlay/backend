"""Drop Game's ``white.$id`` and ``black.$id`` indexes: the compound side
indexes start with them, and beanie never drops indexes a model stopped
declaring."""

import asyncio

from app import db
from app.models import Game

OLD_INDEXES = ["white.$id_1", "black.$id_1"]


async def main() -> None:
    client = await db.connect()
    try:
        collection = Game.get_pymongo_collection()
        existing = await collection.index_information()
        for name in OLD_INDEXES:
            if name not in existing:
                print(f"{name} is already gone, nothing to do")
                continue
            await collection.drop_index(name)
            print(f"dropped {name}")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
