"""Engine catalog: registering pushed images, editing, deletion, pages."""

import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, NamedTuple
from uuid import UUID

from pydantic import BaseModel

from machineplay.schemas import GameStatus

from app import games, ratings, registry
from app.config import settings
from app.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.models import (
    Engine,
    EngineVersion,
    Game,
    Rating,
    RatingCategory,
    RatingChange,
    User,
    utcnow,
)
from app.schemas import (
    EngineDetailOut,
    EngineOut,
    EngineRegisterRequest,
    EngineRegisterResponse,
    EngineUpdateRequest,
    EngineVersionOut,
    EngineVersionUpdateRequest,
)

logger = logging.getLogger(__name__)

# Engine names live in URLs and docker repository paths.
ENGINE_NAME_RE = re.compile(r"^[a-z0-9](?:[._-]?[a-z0-9]){0,63}$")

# `+`/`#` allowed for "c++" and "c#".
TAG_RE = re.compile(r"^[a-z0-9+#](?:[._-]?[a-z0-9+#]){0,23}$")
MAX_TAGS = 10

ENGINE_NAME_ERROR = (
    "engine name must be 1-64 characters: a-z, 0-9 and single "
    "interior separators (. _ -)"
)

# Docker's tag grammar, capped at 64 chars. `machineplay upload` checks the same.
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


class _VersionSummary(NamedTuple):
    version_count: int
    latest_id: UUID
    latest_ratings: dict[RatingCategory, Rating]


async def _version_summaries(engine_ids: list[UUID]) -> dict[UUID, _VersionSummary]:
    if not engine_ids:
        return {}
    rows = await EngineVersion.aggregate(
        [
            {"$match": {"engine.$id": {"$in": engine_ids}}},
            {"$sort": {"created_at": -1}},
            {
                "$group": {
                    "_id": "$engine.$id",
                    "count": {"$sum": 1},
                    "latest": {"$first": "$_id"},
                    "ratings": {"$first": "$ratings"},
                }
            },
        ]
    ).to_list()
    return {
        row["_id"]: _VersionSummary(
            row["count"],
            row["latest"],
            {
                RatingCategory(cat): Rating.model_validate(r)
                for cat, r in (row.get("ratings") or {}).items()
            },
        )
        for row in rows
    }


class _GameStats(NamedTuple):
    played: int = 0
    wins: int = 0
    draws: int = 0
    losses: int = 0
    last_game_at: datetime | None = None


def _outcome(win: str, loss: str) -> dict[str, Any]:
    return {
        "$switch": {
            "branches": [
                {"case": {"$eq": ["$result", win]}, "then": 1},
                {"case": {"$eq": ["$result", loss]}, "then": -1},
                {"case": {"$eq": ["$result", "1/2-1/2"]}, "then": 0},
            ],
            "default": None,
        }
    }


async def _game_stats(engine_ids: list[UUID]) -> dict[UUID, _GameStats]:
    """One pass per colour, each covered by its (side, status, result,
    created_at) index."""
    if not engine_ids:
        return {}
    totals: dict[UUID, dict[str, Any]] = defaultdict(
        lambda: {"games": 0, "wins": 0, "draws": 0, "losses": 0, "last": None}
    )
    for side, win, loss in (("white", "1-0", "0-1"), ("black", "0-1", "1-0")):
        rows = await Game.aggregate(
            [
                {
                    "$match": {
                        f"{side}.$id": {"$in": engine_ids},
                        "status": {"$in": [GameStatus.ENDED, GameStatus.PLAYING]},
                    }
                },
                {
                    "$group": {
                        "_id": {"e": f"${side}.$id", "o": _outcome(win, loss)},
                        "ended": {
                            "$sum": {
                                "$cond": [{"$eq": ["$status", GameStatus.ENDED]}, 1, 0]
                            }
                        },
                        "last": {"$max": "$created_at"},
                    }
                },
            ]
        ).to_list()
        for row in rows:
            t = totals[row["_id"]["e"]]
            outcome = row["_id"].get("o")
            t["games"] += row["ended"]
            if outcome == 1:
                t["wins"] += row["ended"]
            elif outcome == -1:
                t["losses"] += row["ended"]
            elif outcome == 0:
                t["draws"] += row["ended"]
            if t["last"] is None or row["last"] > t["last"]:
                t["last"] = row["last"]
    return {
        eid: _GameStats(t["games"], t["wins"], t["draws"], t["losses"], t["last"])
        for eid, t in totals.items()
    }


WEEK = timedelta(days=7)


class _RatedGame(BaseModel):
    white_version_id: UUID | None = None
    black_version_id: UUID | None = None
    white_rating: RatingChange | None = None
    black_rating: RatingChange | None = None
    tc: str | None = None


async def _week_deltas(
    version_ids: list[UUID],
) -> dict[UUID, dict[RatingCategory, float]]:
    """Each version's rating movement over the last week: the sum of its rated
    games' changes, so no rating history needs keeping."""
    if not version_ids:
        return {}
    rows = (
        await Game.find(
            {
                "rated": True,
                "rated_at": {"$ne": None},
                "status": GameStatus.ENDED,
                "ended_at": {"$gte": utcnow() - WEEK},
            }
        )
        .project(_RatedGame)
        .to_list()
    )
    wanted = set(version_ids)
    out: dict[UUID, dict[RatingCategory, float]] = defaultdict(dict)
    for g in rows:
        cat = ratings.category(g.tc or settings.tc)
        for vid, change in (
            (g.white_version_id, g.white_rating),
            (g.black_version_id, g.black_rating),
        ):
            if vid in wanted and change is not None:
                out[vid][cat] = out[vid].get(cat, 0.0) + change.after - change.before
    return out


async def to_out(engines: list[Engine]) -> list[EngineOut]:
    ids = [e.id for e in engines]
    summaries = await _version_summaries(ids)
    stats = await _game_stats(ids)
    deltas = await _week_deltas([s.latest_id for s in summaries.values()])
    out = []
    for e in engines:
        summary = summaries.get(e.id)
        st = stats.get(e.id, _GameStats())
        out.append(
            EngineOut(
                id=e.id,
                name=e.name,
                description=e.description,
                tags=e.tags,
                owner_login=e.owner_login,
                created_at=e.created_at,
                version_count=summary.version_count if summary else 0,
                ratings=summary.latest_ratings if summary else {},
                week_delta=deltas.get(summary.latest_id, {}) if summary else {},
                games_played=st.played,
                wins=st.wins,
                draws=st.draws,
                losses=st.losses,
                last_game_at=st.last_game_at,
            )
        )
    return out


async def list_engines() -> list[EngineOut]:
    return await to_out(await Engine.find_all().to_list())


async def by_name(owner: User, name: str) -> Engine:
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
    [summary] = await to_out([engine])
    return EngineDetailOut(
        **summary.model_dump(),
        versions=[EngineVersionOut.model_validate(v) for v in versions],
        games=await games.to_out(await games.recent_games([engine.id])),
    )


def _require_owner(user: User, engine: Engine, action: str) -> None:
    if engine.owner.ref.id != user.id and not user.is_admin:
        raise ForbiddenError(f"only the owner can {action}")


async def version_by_id(engine: Engine, version_id: UUID) -> EngineVersion:
    version = await EngineVersion.get(version_id)
    if version is None or version.engine.ref.id != engine.id:
        raise NotFoundError("engine version not found")
    return version


async def edit_engine(user: User, engine: Engine, patch: EngineUpdateRequest) -> Engine:
    """Renaming moves only the URL: versions keep their pushed repository, and
    an upload under the old name creates a fresh engine."""
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
    """Delete one version and, best effort, its registry image.

    Refused while the version has pending or playing games, which covers
    running tournaments. Deleting the last version leaves an empty engine
    rather than silently taking its description, tags and URL with it.
    """
    _require_owner(user, engine, "delete a version")
    version = await version_by_id(engine, version_id)

    # Engine id included so the query rides the white/black indexes.
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

    Call *after* the EngineVersion docs are gone, so a digest some surviving
    version still references is kept. Failures only log: an orphaned manifest
    costs disk until GC.
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
    """Delete an engine, its versions and, best effort, its registry images.
    Refused while it has games pending or playing."""
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
    """Account deletion's unchecked `delete_engine`: the caller has already
    ended the account's active games."""
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
    """Record a version the CLI pushed, creating the Engine on first upload."""
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

    # The registry already limits push to the user's namespace; this stops a
    # user registering someone else's image.
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
