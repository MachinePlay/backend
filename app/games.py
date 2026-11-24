"""Game lifecycle. Creating and scheduling are separate steps so tournaments
can insert games up front and dispatch them as runner slots free up."""

import logging
import random
import re
from uuid import UUID

from beanie.operators import In
from machineplay import schemas

from app import books, ratings, runners, streaming
from app.config import settings
from app.exceptions import ConflictError, NotFoundError, RunnerBusyError
from app.models import Engine, EngineVersion, Game, Runner, Tournament, User
from app.runners import RunnerConnection
from app.schemas import (
    GameDetailOut,
    GameOut,
    RunnerDetailOut,
    Score,
    StartGameRequest,
    StartGameResponse,
)

logger = logging.getLogger(__name__)

# Time control "base+inc" in seconds, e.g. "30+0.3" or "60".
TC_RE = re.compile(r"^\d+(\.\d+)?(\+\d+(\.\d+)?)?$")


def latest_score(game: Game) -> Score | None:
    for ply in range(len(game.evals) - 1, -1, -1):
        info = game.evals[ply]
        if info is None:
            continue
        # Even plies are White's moves; UCI scores from the mover's side.
        sign = 1 if ply % 2 == 0 else -1
        if info.score_mate is not None:
            return Score(mate=sign * info.score_mate)
        if info.score_cp is not None:
            return Score(cp=sign * info.score_cp)
    return None


async def to_out(docs: list[Game]) -> list[GameOut]:
    ids = list({g.tournament_id for g in docs if g.tournament_id is not None})
    names = (
        {t.id: t.name for t in await Tournament.find({"_id": {"$in": ids}}).to_list()}
        if ids
        else {}
    )
    out = []
    for g in docs:
        row = GameOut.model_validate(g)
        row.tournament_name = names.get(g.tournament_id) if g.tournament_id else None
        row.score = latest_score(g)
        out.append(row)
    return out


async def recent_games(engine_ids: list[UUID], limit: int = 20) -> list[Game]:
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
    return (
        await EngineVersion.find({"engine.$id": engine_id})
        .sort("-created_at")
        .first_or_none()
    )


async def _resolve_version(engine: Engine, version_id: UUID | None) -> EngineVersion:
    if version_id is None:
        version = await _latest_version(engine.id)
        if version is None:
            raise NotFoundError(f"engine {engine.name!r} has no uploaded image to play")
        return version
    version = await EngineVersion.get(version_id)
    if version is None or version.engine.ref.id != engine.id:
        raise NotFoundError(f"engine {engine.name!r} has no such version")
    return version


def build_game(
    white: Engine,
    black: Engine,
    white_version: EngineVersion,
    black_version: EngineVersion,
    tc: str,
    runner_id: UUID | None = None,
    tournament_id: UUID | None = None,
    tournament_seq: int | None = None,
    rated: bool = False,
    book: str | None = None,
    opening_moves: list[str] | None = None,
) -> Game:
    """An unsaved PENDING Game, so a tournament can insert its schedule in one
    batch."""
    if not TC_RE.fullmatch(tc):
        raise ConflictError(f"invalid time control {tc!r} (expected 'base+inc')")
    return Game(
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
        tournament_seq=tournament_seq,
        rated=rated,
        book=book,
        opening_moves=opening_moves or [],
        tc=tc,
    )


async def create_game(
    white: Engine,
    black: Engine,
    white_version: EngineVersion,
    black_version: EngineVersion,
    tc: str,
    runner_id: UUID | None = None,
    book: str | None = None,
    opening_moves: list[str] | None = None,
) -> Game:
    doc = build_game(
        white,
        black,
        white_version,
        black_version,
        tc,
        runner_id,
        book=book,
        opening_moves=opening_moves,
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
        opening=list(doc.opening_moves),
    )


async def schedule_game(
    doc: Game,
    runner: RunnerConnection,
    white_version: EngineVersion,
    black_version: EngineVersion,
) -> None:
    """Reserve a slot on `runner`, send it the start command, mark it PLAYING.

    No await between the capacity check and `track_game`, so concurrent
    schedulers can't oversubscribe a runner.
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

    # A runner that disconnected during the enqueue had its cleanup miss this
    # game; roll the slot back and let the caller handle the doc.
    if not runners.is_current(runner):
        runner.untrack_game(doc.id)
        streaming.game_registry.unregister(doc.id)
        raise NotFoundError(
            "runner went offline", details={"runner_id": str(runner.runner_id)}
        )

    # Guarded on PENDING so an end that raced ahead (an instant pull failure)
    # isn't clobbered back to PLAYING.
    await Game.find_one(
        Game.id == doc.id, Game.status == schemas.GameStatus.PENDING
    ).update({"$set": {"status": schemas.GameStatus.PLAYING}})
    doc.status = schemas.GameStatus.PLAYING
    logger.info("scheduled game=%s on runner=%s", doc.id, runner.runner_id)


async def start_game(user: User, payload: StartGameRequest) -> StartGameResponse:
    logger.info("start_game requested by user=%s", user.login)
    white = await Engine.get(payload.white_engine_id)
    black = await Engine.get(payload.black_engine_id)
    if white is None or black is None:
        raise NotFoundError("engine not found")

    white_version = await _resolve_version(white, payload.white_version_id)
    black_version = await _resolve_version(black, payload.black_version_id)

    runner = runners.get_online(payload.runner_id)
    # A one-off game has no colour-reversed twin, so any random line will do.
    book = await books.resolve(payload.book)
    opening = random.choice(book.lines) if book is not None and book.lines else []
    doc = await create_game(
        white,
        black,
        white_version,
        black_version,
        tc=payload.tc or settings.tc,
        runner_id=runner.runner_id,
        book=book.slug if book is not None else None,
        opening_moves=opening,
    )
    try:
        await schedule_game(doc, runner, white_version, black_version)
    except Exception:
        await doc.delete()
        raise

    return StartGameResponse(
        id=doc.id,
        white=white.id,
        black=black.id,
    )


async def cancel_game(user: User, game_id: UUID) -> None:
    """Abort a playing game and tell its runner to kill the containers."""
    doc = await get_game(game_id)
    if doc.status != schemas.GameStatus.PLAYING:
        raise ConflictError("game is not playing")
    runner = runners.find_by_game(game_id)
    logger.info("cancel_game game=%s by user=%s", game_id, user.login)
    await streaming.abort_game(game_id, reason="cancelled")
    if runner is not None:
        await runner.scheduled_commands.put(schemas.StopGame(game_id=game_id))


async def list_games(limit: int) -> list[Game]:
    """Every playing game, then the `limit` most recently finished ones."""
    playing = (
        await Game.find(Game.status == schemas.GameStatus.PLAYING)
        .sort("-created_at")
        .to_list()
    )
    finished = (
        await Game.find(
            In(Game.status, [schemas.GameStatus.ENDED, schemas.GameStatus.ABORTED])
        )
        .sort("-ended_at")
        .limit(limit)
        .to_list()
    )
    return playing + finished


async def get_game(game_id: UUID) -> Game:
    doc = await Game.get(game_id)
    if doc is None:
        raise NotFoundError("game not found")
    return doc


async def game_detail(game_id: UUID) -> GameDetailOut:
    doc = await get_game(game_id)
    [row] = await to_out([doc])
    detail = GameDetailOut.model_validate(
        {**row.model_dump(), "evals": doc.evals, "uci_tail": doc.uci_tail}
    )
    if doc.runner_id is not None:
        runner = await Runner.get(doc.runner_id)
        detail.runner_name = runner.name if runner is not None else None
    cat = ratings.category(doc.tc or settings.tc)
    for side, vid in (("white", doc.white_version_id), ("black", doc.black_version_id)):
        version = await EngineVersion.get(vid) if vid is not None else None
        if version is not None:
            setattr(detail, f"{side}_current", version.ratings.get(cat))
    return detail


async def runner_detail(runner_id: UUID) -> RunnerDetailOut:
    doc = await runners.get_runner(runner_id)
    [row] = await runners.to_out([doc])
    playing = (
        await Game.find(
            Game.runner_id == runner_id, Game.status == schemas.GameStatus.PLAYING
        )
        .sort("+created_at")
        .to_list()
    )
    recent = (
        await Game.find(
            Game.runner_id == runner_id, Game.status == schemas.GameStatus.ENDED
        )
        .sort("-ended_at")
        .limit(10)
        .to_list()
    )
    return RunnerDetailOut(
        **row.model_dump(),
        playing=await to_out(playing),
        recent=await to_out(recent),
    )
