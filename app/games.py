"""Game lifecycle: creating and scheduling games on runners and querying
finished ones.

Creation and scheduling are separate steps so callers that prepare games ahead
of time (tournaments) can create the docs first and dispatch them as slots free
up: `create_game` resolves engines/versions and inserts the doc, `schedule_game`
reserves a runner slot and sends the start command.
"""

import logging
import re
from uuid import UUID

from machineplay import schemas

from app import runners, streaming
from app.config import settings
from app.exceptions import ConflictError, NotFoundError, RunnerBusyError
from app.models import Engine, EngineVersion, Game, User
from app.runners import RunnerConnection
from app.schemas import StartGameRequest, StartGameResponse

logger = logging.getLogger(__name__)

# Time control "base+inc" in seconds, e.g. "30+0.3" or "60".
TC_RE = re.compile(r"^\d+(\.\d+)?(\+\d+(\.\d+)?)?$")


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


async def create_game(
    white: Engine,
    black: Engine,
    white_version: EngineVersion,
    black_version: EngineVersion,
    tc: str,
    runner_id: UUID | None = None,
    tournament_id: UUID | None = None,
) -> Game:
    """Insert a Game doc with everything pinned (engines, exact versions, tc).

    The game is not scheduled yet; pass it to `schedule_game` to start it.
    """
    if not TC_RE.fullmatch(tc):
        raise ConflictError(f"invalid time control {tc!r} (expected 'base+inc')")
    doc = Game(
        white=white,
        black=black,
        white_name=white.name,
        black_name=black.name,
        white_version=white_version.version,
        black_version=black_version.version,
        white_version_id=white_version.id,
        black_version_id=black_version.id,
        runner_id=runner_id,
        tournament_id=tournament_id,
        tc=tc,
    )
    await doc.insert()
    return doc


def _start_command(
    doc: Game, white_version: EngineVersion, black_version: EngineVersion
) -> schemas.StartGame:
    return schemas.StartGame(
        game_id=doc.id,
        white=schemas.EngineConfig(
            name=doc.white_name,
            repository=white_version.image_repository,
            digest=white_version.image_digest,
        ),
        black=schemas.EngineConfig(
            name=doc.black_name,
            repository=black_version.image_repository,
            digest=black_version.image_digest,
        ),
        tc=doc.tc or settings.tc,
    )


async def schedule_game(
    doc: Game,
    runner: RunnerConnection,
    white_version: EngineVersion,
    black_version: EngineVersion,
) -> None:
    """Reserve a slot on `runner` and send it the start command.

    The capacity check and the slot reservation (`track_game`) happen with no
    await between them, so concurrent schedulers can't oversubscribe a runner.
    """
    if runner.is_full():
        raise RunnerBusyError(
            details={
                "runner_id": str(runner.runner_id),
                "active_games": runner.active_games,
                "max_games": runner.max_games,
            }
        )
    runner.track_game(doc.id)
    streaming.game_registry.register_game(doc.id)
    try:
        await runner.scheduled_commands.put(
            _start_command(doc, white_version, black_version)
        )
    except BaseException:
        runner.untrack_game(doc.id)
        streaming.game_registry.unregister(doc.id)
        raise

    # The runner may have disconnected between the capacity check and the
    # enqueue; its cleanup could then miss this game, leaving it 'playing'
    # forever. If this connection is no longer live, roll the slot back and
    # let the caller decide what to do with the doc.
    if not runners.is_current(runner):
        runner.untrack_game(doc.id)
        streaming.game_registry.unregister(doc.id)
        raise NotFoundError(
            "runner went offline", details={"runner_id": str(runner.runner_id)}
        )
    logger.info("scheduled game=%s on runner=%s", doc.id, runner.runner_id)


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
    doc = await create_game(
        white,
        black,
        white_version,
        black_version,
        tc=payload.tc or settings.tc,
        runner_id=runner.runner_id,
    )
    try:
        await schedule_game(doc, runner, white_version, black_version)
    except Exception:
        # The game never started; don't leave an orphan 'playing' doc behind.
        await doc.delete()
        raise

    return StartGameResponse(
        id=doc.id,
        white=white.id,
        black=black.id,
    )


async def cancel_game(user: User, game_id: UUID) -> None:
    """Stop a playing game: abort it server-side immediately and tell whichever
    runner is playing it to kill the engines/containers."""
    doc = await get_game(game_id)
    if doc.status != schemas.GameStatus.PLAYING:
        raise ConflictError("game is not playing")
    runner = runners.find_by_game(game_id)
    logger.info("cancel_game game=%s by user=%s", game_id, user.login)
    await streaming.abort_game(game_id, reason="cancelled")
    if runner is not None:
        await runner.scheduled_commands.put(schemas.StopGame(game_id=game_id))


async def list_games(limit: int) -> list[Game]:
    return await Game.find_all().sort("-created_at").limit(limit).to_list()


async def get_game(game_id: UUID) -> Game:
    doc = await Game.get(game_id)
    if doc is None:
        raise NotFoundError("game not found")
    return doc
