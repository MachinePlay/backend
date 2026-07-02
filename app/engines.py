"""Engine catalog: registering pushed images, listing, and page assembly."""

import logging
import re
from uuid import UUID

from app.config import settings
from app.exceptions import ConflictError, NotFoundError
from app.games import recent_games
from app.models import Engine, EngineVersion, User
from app.schemas import (
    EngineDetailOut,
    EngineOut,
    EngineRegisterRequest,
    EngineRegisterResponse,
    EngineVersionOut,
    GameOut,
)

logger = logging.getLogger(__name__)

# Engine names live in URLs (machineplay.org/{login}/{engine}) and docker
# repository paths, so they are lowercase slugs: a-z/0-9 with single interior
# separators (. _ -), max 64 chars.
ENGINE_NAME_RE = re.compile(r"^[a-z0-9](?:[._-]?[a-z0-9]){0,63}$")


async def _version_counts(engine_ids: list[UUID]) -> dict[UUID, int]:
    """Uploaded-version count per engine, in a single query."""
    if not engine_ids:
        return {}
    rows = await EngineVersion.aggregate(
        [
            {"$match": {"engine.$id": {"$in": engine_ids}}},
            {"$group": {"_id": "$engine.$id", "count": {"$sum": 1}}},
        ]
    ).to_list()
    return {row["_id"]: row["count"] for row in rows}


async def to_out(engines: list[Engine]) -> list[EngineOut]:
    counts = await _version_counts([e.id for e in engines])
    return [
        EngineOut(
            id=e.id,
            name=e.name,
            description=e.description,
            owner_login=e.owner_login,
            version_count=counts.get(e.id, 0),
        )
        for e in engines
    ]


async def list_engines() -> list[EngineOut]:
    return await to_out(await Engine.find_all().to_list())


async def by_name(owner: User, name: str) -> Engine:
    """Case-insensitive engine lookup within an owner's namespace: names are
    stored lowercase (validated at registration), so lowercase and match."""
    engine = await Engine.find_one({"owner.$id": owner.id}, Engine.name == name.lower())
    if engine is None:
        raise NotFoundError("engine not found")
    return engine


async def detail(engine: Engine) -> EngineDetailOut:
    versions = (
        await EngineVersion.find({"engine.$id": engine.id})
        .sort("-created_at")
        .to_list()
    )
    games = await recent_games([engine.id])
    return EngineDetailOut(
        id=engine.id,
        name=engine.name,
        description=engine.description,
        owner_login=engine.owner_login,
        created_at=engine.created_at,
        versions=[EngineVersionOut.model_validate(v) for v in versions],
        games=[GameOut.model_validate(g) for g in games],
    )


async def register_version(
    user: User, payload: EngineRegisterRequest
) -> EngineRegisterResponse:
    """Record an engine version after the CLI pushed its image to the registry.

    The push itself was authorized per-scope by the registry token endpoint;
    this just find-or-creates the Engine and stores the image coordinates
    (repository + digest) as an EngineVersion.
    """
    name = payload.name.strip().lower()
    version = payload.version.strip()
    repository = payload.repository.strip().lower()
    digest = payload.digest.strip()
    if not (name and version and repository and digest):
        raise ConflictError("name, version, repository and digest are required")
    if not ENGINE_NAME_RE.fullmatch(name):
        raise ConflictError(
            "engine name must be 1-64 characters: a-z, 0-9 and single "
            "interior separators (. _ -)"
        )

    # Defense in depth: the token issuer only grants push under the user's own
    # namespace, but re-check here so a leaked token can't register an image
    # under someone else's name.
    namespace = user.login.lower()
    if repository.split("/", 1)[0] != namespace:
        raise ConflictError(f"repository must be namespaced under {namespace!r}")

    engine = await Engine.find_one({"owner.$id": user.id}, Engine.name == name)
    if engine is None:
        engine = Engine(name=name, owner=user, owner_login=user.login)
        await engine.insert()
        logger.info("created engine %s/%s id=%s", user.login, name, engine.id)

    existing = await EngineVersion.find_one(
        {"engine.$id": engine.id}, EngineVersion.version == version
    )
    if existing is not None:
        raise ConflictError(
            f"version {version!r} already exists for {user.login}/{name}"
        )

    doc = EngineVersion(
        engine=engine,
        version=version,
        image_repository=repository,
        image_digest=digest,
        size_bytes=payload.size_bytes,
    )
    await doc.insert()
    logger.info(
        "registered %s/%s version=%s image=%s@%s",
        user.login,
        name,
        version,
        repository,
        digest,
    )

    return EngineRegisterResponse(
        engine_id=engine.id,
        name=name,
        owner_login=user.login,
        version=version,
        url=f"{settings.frontend_url}/{user.login}/{engine.name}",
    )
