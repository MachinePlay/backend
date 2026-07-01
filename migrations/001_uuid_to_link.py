"""One-off migration: convert raw UUID foreign-key fields to Beanie Link DBRefs.

The backend's relations moved from ``<name>_id: UUID`` columns to
``<name>: Link[...]`` fields, which MongoDB stores as DBRefs (``{$ref, $id}``).
This rewrites existing documents in place and drops the now-stale unique indexes
that were built on the old id fields (beanie now indexes ``<field>.$id``).

Idempotent: a document that already carries the new field (or is missing the old
one) is left untouched. It targets whatever ``MONGO_URL`` / ``MONGO_DB`` resolve
to via the environment / ``.env`` — point those at the database you mean to
migrate, and take a backup first.

Run from ``backend/`` (``PYTHONPATH=.`` so ``app`` imports resolve)::

    PYTHONPATH=. uv run python migrations/001_uuid_to_link.py
"""

import asyncio
from typing import Any

from bson import DBRef
from pymongo import AsyncMongoClient

from app.config import settings

# collection -> (old UUID field, new Link field, target collection / $ref).
# Target collections are the beanie class names (== their collection names).
FIELD_MIGRATIONS: dict[str, list[tuple[str, str, str]]] = {
    "Engine": [("owner_id", "owner", "User")],
    "EngineVersion": [("engine_id", "engine", "Engine")],
    "ApiToken": [("user_id", "user", "User")],
    "Game": [("white_id", "white", "Engine"), ("black_id", "black", "Engine")],
}

# Unique indexes on the removed id fields; beanie recreates them on "<field>.$id".
STALE_INDEXES: dict[str, list[str]] = {
    "Engine": ["owner_id_1_name_1"],
    "EngineVersion": ["engine_id_1_version_1"],
}


async def main() -> None:
    client: AsyncMongoClient[dict[str, Any]] = AsyncMongoClient(
        settings.mongo_url, uuidRepresentation="standard"
    )
    db = client[settings.mongo_db]
    print(f"migrating {settings.mongo_db!r} at {settings.mongo_url}")

    for coll, mappings in FIELD_MIGRATIONS.items():
        converted = 0
        async for doc in db[coll].find():
            set_fields: dict[str, DBRef] = {}
            unset_fields: dict[str, str] = {}
            for old, new, target in mappings:
                if old in doc and new not in doc:
                    set_fields[new] = DBRef(target, doc[old])
                    unset_fields[old] = ""
            if set_fields:
                await db[coll].update_one(
                    {"_id": doc["_id"]},
                    {"$set": set_fields, "$unset": unset_fields},
                )
                converted += 1
        print(f"  {coll}: converted {converted} document(s)")

    for coll, names in STALE_INDEXES.items():
        existing = set(await db[coll].index_information())
        for name in names:
            if name in existing:
                await db[coll].drop_index(name)
                print(f"  {coll}: dropped stale index {name!r}")

    await client.close()
    print("done")


if __name__ == "__main__":
    asyncio.run(main())
