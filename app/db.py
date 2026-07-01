from datetime import timezone
from typing import Any

from beanie import init_beanie
from pymongo import AsyncMongoClient

from app.config import settings
from app.models import ApiToken, Engine, EngineVersion, Game, User


# `dict[str, Any]` is pymongo's _DocumentType — shape of raw BSON results.
# Irrelevant here since all reads/writes go through beanie's ODM.
async def connect() -> AsyncMongoClient[dict[str, Any]]:
    # uuidRepresentation="standard" is required for Link fields: beanie's codec
    # rescues the top-level UUID _id, but the UUID nested in a Link's DBRef
    # ($id) is encoded by pymongo and would otherwise be rejected.
    client: AsyncMongoClient[dict[str, Any]] = AsyncMongoClient(
        settings.mongo_url,
        tz_aware=True,
        tzinfo=timezone.utc,
        uuidRepresentation="standard",
    )
    await init_beanie(
        database=client[settings.mongo_db],
        document_models=[Engine, EngineVersion, ApiToken, Game, User],
    )
    return client
