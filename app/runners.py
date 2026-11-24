"""Runners: the durable ``Runner`` document plus in-memory live connections.
"Online" means present in ``_online``."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from uuid import UUID

from machineplay import schemas

from app.exceptions import ForbiddenError, NotFoundError
from app.models import Game, Runner as RunnerDoc, User, utcnow
from app.schemas import RunnerLiveEvent, RunnerOut, RunnerUpdateRequest

logger = logging.getLogger(__name__)


class RunnerConnection:
    """Live state of one connected runner: its command queue and in-flight games."""

    def __init__(self, runner_id: UUID, max_games: int):
        self.runner_id = runner_id
        self.max_games = max_games
        self.scheduled_commands: asyncio.Queue[schemas.ServerCommand] = asyncio.Queue()
        self._game_ids: set[UUID] = set()
        self.latest_telemetry: schemas.Telemetry | None = None

    @property
    def active_games(self) -> int:
        return len(self._game_ids)

    def is_full(self) -> bool:
        return len(self._game_ids) >= self.max_games

    def is_playing(self, game_id: UUID) -> bool:
        return game_id in self._game_ids

    def track_game(self, game_id: UUID) -> None:
        self._game_ids.add(game_id)

    def untrack_game(self, game_id: UUID) -> None:
        self._game_ids.discard(game_id)

    def game_ids(self) -> list[UUID]:
        return list(self._game_ids)

    def update_telemetry(self, telemetry: schemas.Telemetry) -> None:
        self.latest_telemetry = telemetry


_online: dict[UUID, RunnerConnection] = {}


def live_event(conn: RunnerConnection) -> RunnerLiveEvent:
    return RunnerLiveEvent(
        runner_id=conn.runner_id,
        online=True,
        active_games=conn.active_games,
        telemetry=conn.latest_telemetry,
    )


def live_snapshot() -> list[RunnerLiveEvent]:
    """Current live status of every online runner (SSE bootstrap)."""
    return [live_event(conn) for conn in _online.values()]


def mark_online(runner_id: UUID, max_games: int) -> RunnerConnection:
    conn = RunnerConnection(runner_id, max_games)
    _online[runner_id] = conn
    return conn


def mark_offline(conn: RunnerConnection) -> bool:
    """False when a newer connection already replaced `conn`, whose stale
    cleanup must not knock the live one offline."""
    if _online.get(conn.runner_id) is conn:
        del _online[conn.runner_id]
        return True
    return False


def get_online(runner_id: UUID) -> RunnerConnection:
    try:
        return _online[runner_id]
    except KeyError:
        raise NotFoundError(
            "runner is not online", details={"runner_id": str(runner_id)}
        )


def find_online(runner_id: UUID) -> RunnerConnection | None:
    return _online.get(runner_id)


# Lets tournaments resume pending pairings when their runner reconnects.
RunnerConnectedHook = Callable[[UUID], Awaitable[None]]
_connected_hooks: list[RunnerConnectedHook] = []


def on_runner_connected(hook: RunnerConnectedHook) -> None:
    _connected_hooks.append(hook)


async def notify_connected(runner_id: UUID) -> None:
    for hook in _connected_hooks:
        try:
            await hook(runner_id)
        except Exception:
            logger.exception("runner-connected hook failed for runner=%s", runner_id)


def is_current(conn: RunnerConnection) -> bool:
    return _online.get(conn.runner_id) is conn


def find_by_game(game_id: UUID) -> RunnerConnection | None:
    """The online runner currently playing `game_id`, if any."""
    for conn in _online.values():
        if conn.is_playing(game_id):
            return conn
    return None


def untrack_game(game_id: UUID) -> None:
    """Free `game_id`'s slot on whichever runner tracks it."""
    for conn in _online.values():
        conn.untrack_game(game_id)


async def upsert_on_connect(
    user: User,
    runner_id: UUID,
    name: str,
    max_games: int,
    hardware: schemas.HardwareInfo,
) -> RunnerDoc | None:
    """Create the runner's doc on first connect, or refresh what it reports.

    Name and description are only seeded, so the owner's edits survive. None if
    the id belongs to another owner.
    """
    doc = await RunnerDoc.get(runner_id)
    now = utcnow()
    if doc is None:
        doc = RunnerDoc(
            id=runner_id,
            owner=user,
            owner_login=user.login,
            name=name,
            max_games=max_games,
            hardware=hardware,
            last_seen_at=now,
        )
        await doc.insert()
        logger.info("registered new runner id=%s owner=%s", runner_id, user.login)
        return doc
    if doc.owner.ref.id != user.id:
        logger.warning(
            "runner id=%s owned by %s; rejecting connect from %s",
            runner_id,
            doc.owner_login,
            user.login,
        )
        return None
    doc.max_games = max_games
    doc.hardware = hardware
    doc.last_seen_at = now
    await doc.save()
    return doc


async def touch_last_seen(runner_id: UUID) -> None:
    await RunnerDoc.find_one(RunnerDoc.id == runner_id).update(
        {"$set": {"last_seen_at": utcnow()}}
    )


async def _games_played(runner_ids: list[UUID]) -> dict[UUID, int]:
    rows = await Game.aggregate(
        [
            {
                "$match": {
                    "runner_id": {"$in": runner_ids},
                    "status": schemas.GameStatus.ENDED,
                }
            },
            {"$group": {"_id": "$runner_id", "n": {"$sum": 1}}},
        ]
    ).to_list()
    return {row["_id"]: row["n"] for row in rows}


def _to_out(doc: RunnerDoc, games_played: int = 0) -> RunnerOut:
    conn = _online.get(doc.id)
    return RunnerOut(
        runner_id=doc.id,
        name=doc.name,
        description=doc.description,
        owner_login=doc.owner_login,
        online=conn is not None,
        max_games=doc.max_games,
        active_games=conn.active_games if conn is not None else 0,
        last_seen_at=doc.last_seen_at,
        hardware=doc.hardware,
        telemetry=conn.latest_telemetry if conn is not None else None,
        games_played=games_played,
    )


async def to_out(docs: list[RunnerDoc]) -> list[RunnerOut]:
    played = await _games_played([d.id for d in docs])
    return [_to_out(doc, played.get(doc.id, 0)) for doc in docs]


async def list_runners() -> list[RunnerOut]:
    return await to_out(await RunnerDoc.find_all().sort("+created_at").to_list())


async def get_runner(runner_id: UUID) -> RunnerDoc:
    doc = await RunnerDoc.get(runner_id)
    if doc is None:
        raise NotFoundError("runner not found", details={"runner_id": str(runner_id)})
    return doc


async def edit_runner(
    user: User, runner_id: UUID, patch: RunnerUpdateRequest
) -> RunnerOut:
    doc = await RunnerDoc.get(runner_id)
    if doc is None:
        raise NotFoundError("runner not found", details={"runner_id": str(runner_id)})
    if doc.owner.ref.id != user.id:
        raise ForbiddenError("only the runner's owner can edit it")
    if patch.name is not None:
        doc.name = patch.name
    if patch.description is not None:
        doc.description = patch.description
    await doc.save()
    [out] = await to_out([doc])
    return out
