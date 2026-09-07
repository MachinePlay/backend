"""One-off migration: drop the Game index that the tournament page outgrew.

``Game`` used to carry a single-field ``tournament_id`` index. Paging the
pairings list and filtering it by status need the sort key too, so the model now
declares two compound indexes — ``(tournament_id, tournament_seq, created_at)``
and ``(tournament_id, status, tournament_seq, created_at)``. The first has
``tournament_id`` as its prefix, so it answers everything the old index did and
the old one is dead weight: pure write cost on the collection that grows
fastest, now that a tournament can run to 2000 games.

Beanie creates the indexes a model declares but never drops the ones it stopped
declaring, so this has to be done by hand.

Idempotent: a database without the old index is left alone.

Run from ``backend/`` (``PYTHONPATH=.`` so ``app`` imports resolve)::

    PYTHONPATH=. uv run python migrations/004_drop_redundant_tournament_index.py
"""

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
