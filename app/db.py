from datetime import timezone
from typing import Any

from beanie import init_beanie
from pymongo import AsyncMongoClient

from app.config import settings
from app.models import (
    ApiToken,
    Book,
    Engine,
    EngineVersion,
    Game,
    Runner,
    Tournament,
    User,
)


async def connect() -> AsyncMongoClient[dict[str, Any]]:
    # Link fields need uuidRepresentation="standard": beanie encodes the UUID
    # _id itself, but pymongo encodes the one inside a Link's DBRef.
    client: AsyncMongoClient[dict[str, Any]] = AsyncMongoClient(
        settings.mongo_url,
        tz_aware=True,
        tzinfo=timezone.utc,
        uuidRepresentation="standard",
    )
    await init_beanie(
        database=client[settings.mongo_db],
        document_models=[
            Book,
            Engine,
            EngineVersion,
            ApiToken,
            Game,
            Runner,
            Tournament,
            User,
        ],
    )
    return client
