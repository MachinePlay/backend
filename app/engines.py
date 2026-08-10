"""Engine catalog: registering pushed images, listing, deletion, and page
assembly."""

import logging
import re
from uuid import UUID

from machineplay.schemas import GameStatus

from app import registry
from app.config import settings
from app.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.games import recent_games
from app.models import Engine, EngineVersion, Game, User
from app.schemas import (
    EngineDetailOut,
    EngineOut,
    EngineRegisterRequest,
    EngineRegisterResponse,
    EngineUpdateRequest,
    EngineVersionOut,
    EngineVersionUpdateRequest,
    GameOut,
)

logger = logging.getLogger(__name__)

# Engine names live in URLs (machineplay.org/{login}/{engine}) and docker
# repository paths, so they are lowercase slugs: a-z/0-9 with single interior
# separators (. _ -), max 64 chars.
ENGINE_NAME_RE = re.compile(r"^[a-z0-9](?:[._-]?[a-z0-9]){0,63}$")

# Tags are keywords describing an engine ("python", "mcts", "rust"). Lowercase
# like every other user-facing slug, but `+`/`#` are allowed so "c++" and "c#"
# work. Capped in count and length so they stay renderable as pills.
TAG_RE = re.compile(r"^[a-z0-9+#](?:[._-]?[a-z0-9+#]){0,23}$")
MAX_TAGS = 10

ENGINE_NAME_ERROR = (
    "engine name must be 1-64 characters: a-z, 0-9 and single "
    "interior separators (. _ -)"
)

# A version string labels one uploaded image and becomes its docker tag, so it
# follows docker's tag grammar — mixed case allowed, unlike engine names — and
# is capped at 64 chars so version rows stay renderable. `machineplay upload`
# checks the same shape before it pushes.
ENGINE_VERSION_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$")

ENGINE_VERSION_ERROR = (
    "version must be 1-64 characters of letters, digits, '.', '_' and '-', "
    "starting with a letter, digit or '_'"
)


def normalize_tags(raw: list[str]) -> list[str]:
    """Lowercase, drop blanks, de-duplicate (keeping order) and validate."""
    tags: list[str] = []
    for entry in raw:
        tag = entry.strip().lower()
        if not tag:
            continue
        if not TAG_RE.fullmatch(tag):
            raise ConflictError(
                f"invalid tag {tag!r}: tags are 1-24 characters of a-z, 0-9, "
                "+ and #, with single interior separators (. _ -)"
            )
        if tag not in tags:
            tags.append(tag)
    if len(tags) > MAX_TAGS:
        raise ConflictError(f"at most {MAX_TAGS} tags per engine")
    return tags


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
            tags=e.tags,
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
        tags=engine.tags,
        owner_login=engine.owner_login,
        created_at=engine.created_at,
        versions=[EngineVersionOut.model_validate(v) for v in versions],
        games=[GameOut.model_validate(g) for g in games],
    )


def _require_owner(user: User, engine: Engine, action: str) -> None:
    """Owner-or-admin gate for everything that mutates an engine."""
    if engine.owner.ref.id != user.id and not user.is_admin:
        raise ForbiddenError(f"only the owner can {action}")


async def version_by_id(engine: Engine, version_id: UUID) -> EngineVersion:
    """One of `engine`'s uploaded versions, by id.

    A version id belonging to some *other* engine is a 404 here, not a 403: the
    URL names an engine and a version that don't go together.
    """
    version = await EngineVersion.get(version_id)
    if version is None or version.engine.ref.id != engine.id:
        raise NotFoundError("engine version not found")
    return version


async def edit_engine(user: User, engine: Engine, patch: EngineUpdateRequest) -> Engine:
    """Owner/admin-only edit of an engine's name, description and tags.

    Renaming only moves the engine's URL: uploaded versions keep pointing at
    the registry repository they were pushed to, and a digest-pinned pull does
    not care what the engine is called. The next `machineplay upload` under the
    *old* name would find-or-create a fresh engine, though.
    """
    _require_owner(user, engine, "edit an engine")

    if patch.name is not None:
        name = patch.name.strip().lower()
        if not ENGINE_NAME_RE.fullmatch(name):
            raise ConflictError(ENGINE_NAME_ERROR)
        if name != engine.name:
            clash = await Engine.find_one(
                {"owner.$id": engine.owner.ref.id}, Engine.name == name
            )
            if clash is not None:
                raise ConflictError(f"{engine.owner_login}/{name} already exists")
            engine.name = name
    if patch.description is not None:
        engine.description = patch.description.strip()
    if patch.tags is not None:
        engine.tags = normalize_tags(patch.tags)

    await engine.save()
    logger.info("edited engine %s/%s id=%s", engine.owner_login, engine.name, engine.id)
    return engine


async def edit_version(
    user: User, engine: Engine, version_id: UUID, patch: EngineVersionUpdateRequest
) -> EngineVersion:
    """Owner/admin-only rename of one uploaded version's label.

    Only the label moves. The image stays pinned by repository+digest (the
    registry tag written at upload is never read back), and games and
    tournament participants keep the denormalized version string they recorded,
    so old history keeps reading the way it was played.
    """
    _require_owner(user, engine, "edit a version")
    version = await version_by_id(engine, version_id)

    if patch.version is not None:
        label = patch.version.strip()
        if not ENGINE_VERSION_RE.fullmatch(label):
            raise ConflictError(ENGINE_VERSION_ERROR)
        if label != version.version:
            clash = await EngineVersion.find_one(
                {"engine.$id": engine.id}, EngineVersion.version == label
            )
            if clash is not None:
                raise ConflictError(
                    f"version {label!r} already exists for "
                    f"{engine.owner_login}/{engine.name}"
                )
            logger.info(
                "renamed %s/%s version %s -> %s (id=%s)",
                engine.owner_login,
                engine.name,
                version.version,
                label,
                version.id,
            )
            version.version = label
            await version.save()

    return version


async def delete_version(user: User, engine: Engine, version_id: UUID) -> None:
    """Delete one uploaded version and (best-effort) its registry image.

    Owner or admin only, and refused while that exact version is in a pending
    or playing game. That also covers a running tournament: its pairings all
    exist as PENDING games from creation, so an entered version can't vanish
    mid-tournament.

    Finished games and tournament participant snapshots survive — they carry
    denormalized engine/version strings, so history still renders — but the
    version is gone from the engine page and can't be picked for new games.

    Deleting the last version is allowed: the engine stays, marked as having
    nothing to play, and starting a game against it fails until something is
    uploaded again. Emptiness is recoverable (upload another version), while
    auto-deleting the engine would silently take its description, tags and URL
    with it.
    """
    _require_owner(user, engine, "delete a version")
    version = await version_by_id(engine, version_id)

    # Matched on engine *and* version id so the query rides the white/black
    # engine indexes; a version belongs to exactly one engine, so that's the
    # same set of games as matching the version id alone.
    active = await Game.find(
        {
            "$or": [
                {"white.$id": engine.id, "white_version_id": version.id},
                {"black.$id": engine.id, "black_version_id": version.id},
            ],
            "status": {"$in": [GameStatus.PENDING, GameStatus.PLAYING]},
        }
    ).count()
    if active:
        raise ConflictError(
            f"version {version.version!r} has {active} game(s) pending or "
            "playing; cancel them or wait for them to finish"
        )

    await version.delete()
    logger.info(
        "deleted %s/%s version=%s id=%s",
        engine.owner_login,
        engine.name,
        version.version,
        version.id,
    )
    await _delete_manifests([version])


async def _delete_manifests(versions: list[EngineVersion]) -> None:
    """Drop deleted versions' images from the registry, best effort.

    Call *after* the EngineVersion docs are gone: each distinct
    repository+digest is removed unless some surviving version still references
    it (an identical re-upload, or the same image registered under two
    engines). Failures are logged, not raised — an orphaned manifest only costs
    disk until GC, while a half-done delete would be user-visible.
    """
    for repository, digest in {(v.image_repository, v.image_digest) for v in versions}:
        shared = await EngineVersion.find_one(
            EngineVersion.image_repository == repository,
            EngineVersion.image_digest == digest,
        )
        if shared is not None:
            logger.info(
                "keeping manifest %s@%s: still referenced by another engine version",
                repository,
                digest,
            )
            continue
        try:
            await registry.delete_manifest(repository, digest)
        except Exception:
            logger.exception(
                "registry cleanup failed for %s@%s (image orphaned until GC)",
                repository,
                digest,
            )


async def delete_engine(user: User, engine: Engine) -> None:
    """Delete an engine, its versions, and (best-effort) its registry images.

    Owner or admin only, and refused while the engine has games pending or
    playing. Finished games are kept — they carry denormalized names, so
    history still renders after the engine is gone.
    """
    _require_owner(user, engine, "delete an engine")
    active = await Game.find(
        {
            "$or": [{"white.$id": engine.id}, {"black.$id": engine.id}],
            "status": {"$in": [GameStatus.PENDING, GameStatus.PLAYING]},
        }
    ).count()
    if active:
        raise ConflictError(
            f"engine has {active} game(s) pending or playing; "
            "cancel them or wait for them to finish"
        )

    versions = await EngineVersion.find({"engine.$id": engine.id}).to_list()
    await EngineVersion.find({"engine.$id": engine.id}).delete()
    await engine.delete()
    logger.info(
        "deleted engine %s/%s id=%s (%d versions)",
        engine.owner_login,
        engine.name,
        engine.id,
        len(versions),
    )
    await _delete_manifests(versions)


async def delete_owned_engines(user: User) -> int:
    """Delete every engine `user` owns, with its versions and registry images.

    The account-deletion path, so unlike `delete_engine` it asks no questions:
    the caller has already ended the account's active games, and anything still
    referencing these engines (finished games, tournament participants) carries
    denormalized names that keep rendering without them. Returns the number of
    engines deleted.
    """
    owned = await Engine.find({"owner.$id": user.id}).to_list()
    if not owned:
        return 0
    engine_ids = [e.id for e in owned]
    versions = await EngineVersion.find({"engine.$id": {"$in": engine_ids}}).to_list()
    await EngineVersion.find({"engine.$id": {"$in": engine_ids}}).delete()
    await Engine.find({"owner.$id": user.id}).delete()
    logger.info(
        "deleted %d engine(s) and %d version(s) owned by %s",
        len(owned),
        len(versions),
        user.login,
    )
    await _delete_manifests(versions)
    return len(owned)


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
        raise ConflictError(ENGINE_NAME_ERROR)
    if not ENGINE_VERSION_RE.fullmatch(version):
        raise ConflictError(ENGINE_VERSION_ERROR)

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
