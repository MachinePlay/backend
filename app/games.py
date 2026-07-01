"""Game lifecycle: scheduling games on runners and querying finished ones."""

import logging
from uuid import UUID

from machineplay import schemas

from app import runners, streaming
from app.config import settings
from app.exceptions import NotFoundError, RunnerBusyError
from app.models import Engine, EngineVersion, Game, User
from app.schemas import StartGameRequest, StartGameResponse

logger = logging.getLogger(__name__)


async def recent_games(engine_ids: list[UUID], limit: int = 20) -> list[Game]:
    """The latest games where any of `engine_ids` played either side."""
    if not engine_ids:
        return []
    return (
        await Game.find(
            {
                "$or": [
                    {"white.$id": {"$in": engine_ids}},
                    {"black.$id": {"$in": engine_ids}},
                ]
            }
        )
        .sort("-created_at")
        .limit(limit)
        .to_list()
    )


async def _latest_version(engine_id: UUID) -> EngineVersion | None:
    """The most recently uploaded image version for an engine, or None."""
    return (
        await EngineVersion.find({"engine.$id": engine_id})
        .sort("-created_at")
        .first_or_none()
    )


async def _resolve_version(engine: Engine, version_id: UUID | None) -> EngineVersion:
    """The requested uploaded version of `engine`, or its latest when None."""
    if version_id is None:
        version = await _latest_version(engine.id)
        if version is None:
            raise NotFoundError(f"engine {engine.name!r} has no uploaded image to play")
        return version
    version = await EngineVersion.get(version_id)
    if version is None or version.engine.ref.id != engine.id:
        raise NotFoundError(f"engine {engine.name!r} has no such version")
    return version


async def start_game(user: User, payload: StartGameRequest) -> StartGameResponse:
    """Create a Game doc and schedule it on the requested runner."""
    logger.info("start_game requested by user=%s", user.login)
    white = await Engine.get(payload.white_engine_id)
    black = await Engine.get(payload.black_engine_id)
    if white is None or black is None:
        raise NotFoundError("engine not found")

    white_version = await _resolve_version(white, payload.white_version_id)
    black_version = await _resolve_version(black, payload.black_version_id)

    runner = runners.get_online(payload.runner_id)

    if runner.is_full():
        raise RunnerBusyError(
            details={
                "runner_id": str(runner.runner_id),
                "active_games": runner.active_games,
                "max_games": runner.max_games,
            }
        )

    doc = Game(
        white=white,
        black=black,
        white_name=white.name,
        black_name=black.name,
        white_version=white_version.version,
        black_version=black_version.version,
    )
    await doc.insert()

    streaming.game_registry.register_game(doc.id)
    runner.track_game(doc.id)

    await runner.scheduled_commands.put(
        schemas.StartGame(
            game_id=doc.id,
            white=schemas.EngineConfig(
                name=white.name,
                repository=white_version.image_repository,
                digest=white_version.image_digest,
            ),
            black=schemas.EngineConfig(
                name=black.name,
                repository=black_version.image_repository,
                digest=black_version.image_digest,
            ),
            tc=settings.tc,
        )
    )
    logger.info("scheduled game=%s on runner=%s", doc.id, runner.runner_id)

    return StartGameResponse(
        id=doc.id,
        status="started",
        white=white.id,
        black=black.id,
    )


async def list_games(limit: int) -> list[Game]:
    return await Game.find_all().sort("-created_at").limit(limit).to_list()


async def get_game(game_id: UUID) -> Game:
    doc = await Game.get(game_id)
    if doc is None:
        raise NotFoundError("game not found")
    return doc
