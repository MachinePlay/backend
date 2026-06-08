import asyncio
import logging
from collections.abc import AsyncIterable
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.sse import EventSourceResponse

from machineplay import schemas

from app import streaming
from app.auth import require_token_user, require_user
from app.config import settings
from app.exceptions import (
    ConflictError,
    NotFoundError,
    RunnerBusyError,
)
from app.models import Engine, EngineVersion, Game, User
from app.schemas import (
    EngineDetailOut,
    EngineOut,
    EngineRegisterRequest,
    EngineRegisterResponse,
    EngineVersionOut,
    GameOut,
    LiveStreamEvent,
    RunnerOut,
    StartGameRequest,
    StartGameResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter()


async def _engine_to_out(engine: Engine) -> EngineOut:
    count = await EngineVersion.find(EngineVersion.engine_id == engine.id).count()
    return EngineOut(
        id=engine.id,
        name=engine.name,
        command=engine.command,
        description=engine.description,
        owner_login=engine.owner_login,
        version_count=count,
    )


@router.get("/engine", response_model=list[EngineOut])
async def list_engines() -> list[EngineOut]:
    engines = await Engine.find_all().to_list()
    return [await _engine_to_out(e) for e in engines]


@router.get("/engine/{engine_id}", response_model=EngineDetailOut)
async def get_engine(engine_id: UUID) -> EngineDetailOut:
    engine = await Engine.get(engine_id)
    if engine is None:
        raise NotFoundError("engine not found")
    versions = (
        await EngineVersion.find(EngineVersion.engine_id == engine_id)
        .sort("-created_at")
        .to_list()
    )
    return EngineDetailOut(
        id=engine.id,
        name=engine.name,
        description=engine.description,
        owner_login=engine.owner_login,
        created_at=engine.created_at,
        versions=[EngineVersionOut.model_validate(v) for v in versions],
    )


@router.post("/engine/register", response_model=EngineRegisterResponse)
async def register_engine(
    payload: EngineRegisterRequest,
    user: User = Depends(require_token_user),
) -> EngineRegisterResponse:
    """Record an engine version after the CLI pushed its image to the registry.

    The push itself was authorized per-scope by the registry token endpoint;
    this just find-or-creates the Engine and stores the image coordinates
    (repository + digest) as an EngineVersion. Loading/running the image during
    games is the M6 follow-up.
    """
    name = payload.name.strip()
    version = payload.version.strip()
    repository = payload.repository.strip().lower()
    digest = payload.digest.strip()
    if not (name and version and repository and digest):
        raise ConflictError("name, version, repository and digest are required")

    # Defense in depth: the token issuer only grants push under the user's own
    # namespace, but re-check here so a leaked token can't register an image
    # under someone else's name.
    namespace = user.login.lower()
    if repository.split("/", 1)[0] != namespace:
        raise ConflictError(f"repository must be namespaced under {namespace!r}")

    engine = await Engine.find_one(Engine.owner_id == user.id, Engine.name == name)
    if engine is None:
        engine = Engine(name=name, owner_id=user.id, owner_login=user.login)
        await engine.insert()
        logger.info("created engine %s/%s id=%s", user.login, name, engine.id)

    existing = await EngineVersion.find_one(
        EngineVersion.engine_id == engine.id, EngineVersion.version == version
    )
    if existing is not None:
        raise ConflictError(
            f"version {version!r} already exists for {user.login}/{name}"
        )

    doc = EngineVersion(
        engine_id=engine.id,
        version=version,
        image_repository=repository,
        image_digest=digest,
        size_bytes=payload.size_bytes,
        image_name=name,
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
        url=f"{settings.frontend_url}/engine/{engine.id}",
    )


@router.get("/runners", response_model=list[RunnerOut])
async def list_runners() -> list[streaming.Runner]:
    return streaming.runners.list_runners()


@router.post("/game")
async def start_game(
    payload: StartGameRequest, user: User = Depends(require_user)
) -> StartGameResponse:
    logger.info("start_game requested by user=%s", user.login)
    white = await Engine.get(payload.white_engine_id)
    black = await Engine.get(payload.black_engine_id)
    if white is None or black is None:
        raise NotFoundError("engine not found")

    runner = streaming.runners.get_runner(payload.runner_id)

    if runner.is_full():
        raise RunnerBusyError(
            details={
                "runner_id": str(runner.runner_id),
                "active_games": runner.active_games,
                "max_games": runner.max_games,
            }
        )

    doc = Game(
        white_id=white.id,
        black_id=black.id,
        white_name=white.name,
        black_name=black.name,
    )
    await doc.insert()

    streaming.game_registry.register_game(doc.id)
    runner.track_game(doc.id)

    await runner.scheduled_commands.put(
        schemas.StartGame(
            game_id=doc.id,
            white=schemas.EngineConfig(name=white.name, command=white.command),
            black=schemas.EngineConfig(name=black.name, command=black.command),
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


@router.get("/game", response_model=list[GameOut])
async def list_games(limit: int = 50) -> list[Game]:
    limit = max(1, min(limit, 200))
    return await Game.find_all().sort("-created_at").limit(limit).to_list()


@router.get("/game/{game_id}", response_model=GameOut)
async def get_game(game_id: UUID) -> Game:
    doc = await Game.get(game_id)
    if doc is None:
        raise NotFoundError("game not found")
    return doc


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()

    intro = schemas.Introduction.model_validate_json(await ws.receive_text())
    runner = streaming.runners.register_runner(
        intro.runner_id, intro.name, max_games=intro.max_games
    )
    logger.info(
        "runner connected id=%s name=%s max_games=%d",
        intro.runner_id,
        intro.name,
        intro.max_games,
    )

    async def receiver() -> None:
        while True:
            data = await ws.receive_text()
            cmd: schemas.ClientCommandType = schemas.client_adapter.validate_json(data)
            match cmd:
                case schemas.GameEvent(game_id=game_id, event=event):
                    try:
                        game = streaming.game_registry.get_game(game_id)
                    except NotFoundError:
                        logger.warning("event for unregistered game_id=%s", game_id)
                        continue
                    if isinstance(event, schemas.GameEndEvent):
                        runner.untrack_game(game_id)
                        streaming.game_registry.registry.pop(game_id, None)
                    await streaming.persist_event(game_id, event)
                    await game.broadcast(event)
                    await streaming.live_stream.broadcast(game_id, event)

    async def sender() -> None:
        while True:
            command = await runner.scheduled_commands.get()
            await ws.send_text(command.model_dump_json())

    recv_task = asyncio.create_task(receiver())
    send_task = asyncio.create_task(sender())

    try:
        done, _ = await asyncio.wait(
            {recv_task, send_task},
            return_when=asyncio.FIRST_EXCEPTION,
        )
        for task in done:
            task.result()  # re-raise exceptions
    except WebSocketDisconnect:
        logger.info("runner disconnected id=%s", intro.runner_id)
    finally:
        recv_task.cancel()
        send_task.cancel()
        await runner.abort_games()
        streaming.runners.unregister_runner(intro.runner_id)


@router.get(
    "/stream/game/{game_id}",
    response_class=EventSourceResponse,
    # SSE responses are opaque to FastAPI's auto-schema; declaring the
    # per-message payload here makes the event type appear in OpenAPI so
    # the generated TS client can reference it.
    responses={200: {"model": schemas.GameStreamEvent}},
)
async def sse_stream(game_id: UUID) -> AsyncIterable[schemas.GameStreamEvent]:
    try:
        game = streaming.game_registry.get_game(game_id)
    except NotFoundError:
        # Game is no longer live; if it exists in the DB, emit a single
        # terminal event so late subscribers see a clean end rather than 404.
        doc = await Game.get(game_id)
        if doc is None:
            raise
        yield schemas.GameEndEvent(result=doc.result, pgn=doc.pgn)
        return

    q = game.subscribe()
    try:
        # Subscribe-then-snapshot: the WS receiver writes the DB before
        # broadcasting, so anything already in the snapshot is also (or about
        # to be) in our queue. Dedup queued events by ply against the snapshot
        # so the client sees each event exactly once.
        doc = await Game.get(game_id)
        snapshot_ply = -1
        if doc is not None:
            snapshot_ply = len(doc.moves)
            yield schemas.FenEvent(
                fen=doc.fen,
                ply=snapshot_ply,
                white_name=doc.white_name,
                black_name=doc.black_name,
                moves=doc.moves,
                white_clock=doc.white_clock,
                black_clock=doc.black_clock,
                result=doc.result,
                status=doc.status,
                game_id=game_id,
            )

        while True:
            event = await q.get()
            match event:
                case schemas.GameStartEvent() if snapshot_ply >= 0:
                    continue
                case schemas.MoveEvent(ply=ply) if ply <= snapshot_ply:
                    continue
                case schemas.FenEvent(ply=ply) if ply <= snapshot_ply:
                    continue
            yield event
            if isinstance(event, schemas.GameEndEvent):
                return
    except asyncio.CancelledError:
        logger.info("SSE cancelled game=%s", game_id)
        raise
    finally:
        game.unsubscribe(q)


@router.get(
    "/stream/live",
    response_class=EventSourceResponse,
    responses={200: {"model": LiveStreamEvent}},
)
async def sse_live_stream() -> AsyncIterable[LiveStreamEvent]:
    q = streaming.live_stream.subscribe()
    try:
        while True:
            game_id, event = await q.get()
            yield LiveStreamEvent(game_id=game_id, event=event)
    finally:
        streaming.live_stream.unsubscribe(q)
