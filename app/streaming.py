import asyncio
import logging
from uuid import UUID

from machineplay import schemas
from machineplay.schemas import GameStatus

from app.exceptions import NotFoundError
from app.models import Game as GameDoc, utcnow

logger = logging.getLogger(__name__)


async def abort_game(game_id: UUID) -> None:
    """Mark a playing game as aborted in DB and notify subscribers."""
    doc = await GameDoc.get(game_id)
    if doc is not None and doc.status == GameStatus.PLAYING:
        doc.status = GameStatus.ABORTED
        doc.result = "*"
        doc.ended_at = utcnow()
        await doc.save()
        logger.info("aborted game=%s", game_id)

    game = game_registry.registry.pop(game_id, None)
    if game is not None:
        end_event = schemas.GameEndEvent(result="*", pgn=None)
        await game.broadcast(end_event)
        await live_stream.broadcast(game_id, end_event)


async def abort_orphan_games() -> None:
    """Mark any DB games still in PLAYING as aborted (e.g. after backend restart)."""
    orphans = await GameDoc.find(GameDoc.status == GameStatus.PLAYING).to_list()
    for doc in orphans:
        doc.status = GameStatus.ABORTED
        doc.result = "*"
        doc.ended_at = utcnow()
        await doc.save()
    if orphans:
        logger.info("aborted %d orphan game(s) on startup", len(orphans))


async def persist_event(game_id: UUID, event: schemas.GameStreamEvent) -> None:
    update: dict[str, dict[str, object]] = {}
    match event:
        case schemas.GameStartEvent():
            update["$set"] = {"status": GameStatus.PLAYING}
        case schemas.FenEvent(fen=fen, moves=moves, white_clock=wc, black_clock=bc):
            update["$set"] = {
                "fen": fen,
                "moves": list(moves),
                "white_clock": wc,
                "black_clock": bc,
            }
        case schemas.MoveEvent(san=san, fen=fen, white_clock=wc, black_clock=bc):
            update["$push"] = {"moves": san}
            update["$set"] = {"fen": fen, "white_clock": wc, "black_clock": bc}
        case schemas.GameEndEvent(result=result, pgn=pgn):
            set_fields: dict[str, object] = {
                "status": GameStatus.ENDED,
                "result": result,
                "ended_at": utcnow(),
            }
            if pgn is not None:
                set_fields["pgn"] = pgn
            update["$set"] = set_fields

    result = await GameDoc.find_one(GameDoc.id == game_id).update(update)
    if result.matched_count == 0:
        logger.warning("event for unknown game_id=%s", game_id)


class Game:
    def __init__(self, game_id: UUID):
        self.game_id = game_id
        self.subscribers: set[asyncio.Queue[schemas.GameStreamEvent]] = set()

    def subscribe(self) -> asyncio.Queue[schemas.GameStreamEvent]:
        q: asyncio.Queue[schemas.GameStreamEvent] = asyncio.Queue(maxsize=256)
        self.subscribers.add(q)
        logger.info(
            "game=%s subscriber added, total=%d", self.game_id, len(self.subscribers)
        )
        return q

    def unsubscribe(self, q: asyncio.Queue[schemas.GameStreamEvent]) -> None:
        self.subscribers.discard(q)
        logger.info(
            "game=%s subscriber removed, total=%d", self.game_id, len(self.subscribers)
        )

    async def broadcast(self, event: schemas.GameStreamEvent) -> None:
        for q in self.subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning(
                    "subscriber queue full, dropping event type=%s", event.type
                )


class LiveStream:
    """Broadcasts events from up to LIMIT live games to all subscribers.

    Maintains a system-wide set of tracked game ids: events for tracked
    games fan out to every subscriber, others are dropped. When a tracked
    game ends, its slot is filled by another currently-playing game (if any)
    and a synthetic FenEvent is emitted so subscribers can bootstrap it.
    """

    LIMIT = 8

    def __init__(self) -> None:
        self.subscribers: set[asyncio.Queue[tuple[UUID, schemas.GameStreamEvent]]] = (
            set()
        )
        self.tracked: set[UUID] = set()

    def subscribe(self) -> asyncio.Queue[tuple[UUID, schemas.GameStreamEvent]]:
        q: asyncio.Queue[tuple[UUID, schemas.GameStreamEvent]] = asyncio.Queue(
            maxsize=512
        )
        self.subscribers.add(q)
        logger.info("live stream subscriber added, total=%d", len(self.subscribers))
        return q

    def unsubscribe(
        self, q: asyncio.Queue[tuple[UUID, schemas.GameStreamEvent]]
    ) -> None:
        self.subscribers.discard(q)
        logger.info("live stream subscriber removed, total=%d", len(self.subscribers))

    async def broadcast(self, game_id: UUID, event: schemas.GameStreamEvent) -> None:
        if game_id in self.tracked:
            self._fanout(game_id, event)
            if isinstance(event, schemas.GameEndEvent):
                self.tracked.discard(game_id)
                await self._promote()
        elif (
            isinstance(event, schemas.GameStartEvent) and len(self.tracked) < self.LIMIT
        ):
            self.tracked.add(game_id)
            self._fanout(game_id, event)
        elif isinstance(event, schemas.GameEndEvent):
            # Untracked game ended — still forward so any subscriber that knows
            # about it (e.g. via the initial /game fetch) can mark it ended.
            self._fanout(game_id, event)

    async def _promote(self) -> None:
        for gid in list(game_registry.registry.keys()):
            if gid in self.tracked:
                continue
            doc = await GameDoc.get(gid)
            if doc is None or doc.status != GameStatus.PLAYING:
                continue
            self.tracked.add(gid)
            self._fanout(
                gid,
                schemas.FenEvent(
                    fen=doc.fen,
                    ply=len(doc.moves),
                    white_name=doc.white_name,
                    black_name=doc.black_name,
                    moves=doc.moves,
                    white_clock=doc.white_clock,
                    black_clock=doc.black_clock,
                    result=doc.result,
                    status=GameStatus.PLAYING,
                    game_id=gid,
                ),
            )
            return

    def _fanout(self, game_id: UUID, event: schemas.GameStreamEvent) -> None:
        for q in self.subscribers:
            try:
                q.put_nowait((game_id, event))
            except asyncio.QueueFull:
                logger.warning(
                    "live stream queue full, dropping event type=%s", event.type
                )


class GameRegistry:
    def __init__(self) -> None:
        self.registry: dict[UUID, Game] = {}

    def register_game(self, game_id: UUID) -> Game:
        new_game = Game(game_id)
        self.registry[game_id] = new_game
        return new_game

    def get_game(self, game_id: UUID) -> Game:
        try:
            return self.registry[game_id]
        except KeyError:
            raise NotFoundError(
                "game with this id not found", details={"game_id": str(game_id)}
            )


class Runner:
    def __init__(self, runner_id: UUID, name: str, max_games: int):
        self.runner_id = runner_id
        self.name = name
        self.max_games = max_games
        self.scheduled_commands: asyncio.Queue[schemas.ServerCommand] = asyncio.Queue()
        self._game_ids: set[UUID] = set()

    @property
    def active_games(self) -> int:
        return len(self._game_ids)

    def is_full(self) -> bool:
        return len(self._game_ids) >= self.max_games

    def track_game(self, game_id: UUID) -> None:
        self._game_ids.add(game_id)

    def untrack_game(self, game_id: UUID) -> None:
        self._game_ids.discard(game_id)

    async def abort_games(self) -> None:
        for game_id in list(self._game_ids):
            await abort_game(game_id)
        self._game_ids.clear()


class Runners:
    def __init__(self) -> None:
        self.data: dict[UUID, Runner] = {}

    def register_runner(self, runner_id: UUID, name: str, max_games: int) -> Runner:
        new_runner = Runner(runner_id, name, max_games=max_games)
        self.data[runner_id] = new_runner
        return new_runner

    def unregister_runner(self, runner_id: UUID) -> None:
        self.data.pop(runner_id, None)

    def get_runner(self, runner_id: UUID) -> Runner:
        try:
            return self.data[runner_id]
        except KeyError:
            raise NotFoundError(
                "no runner with this id is running",
                details={"runner_id": str(runner_id)},
            )

    def list_runners(self) -> list[Runner]:
        return list(self.data.values())


game_registry = GameRegistry()
live_stream = LiveStream()
runners = Runners()
