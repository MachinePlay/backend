"""The fan-out buses and the live-game registry."""

import asyncio
import logging
from collections import deque
from uuid import UUID

from machineplay import schemas
from machineplay.schemas import GameStatus

from app.exceptions import NotFoundError
from app.models import Game as GameDoc
from app.schemas import RunnerLiveEvent

logger = logging.getLogger(__name__)


def _snapshot_event(doc: GameDoc) -> schemas.FenEvent:
    """The full current state of `doc`, for bootstrapping a subscriber."""
    return schemas.FenEvent(
        fen=doc.fen,
        ply=len(doc.moves),
        white_name=doc.white_name,
        black_name=doc.black_name,
        moves=doc.moves,
        evals=doc.evals,
        white_clock=doc.white_clock,
        black_clock=doc.black_clock,
        result=doc.result,
        status=doc.status,
        game_id=doc.id,
    )


class Broadcast[T]:
    """Fan-out bus with a bounded queue per subscriber; a full queue drops the
    event rather than blocking the publisher."""

    def __init__(self, label: str, maxsize: int) -> None:
        self._label = label
        self._maxsize = maxsize
        self.subscribers: set[asyncio.Queue[T]] = set()

    def subscribe(self) -> asyncio.Queue[T]:
        q: asyncio.Queue[T] = asyncio.Queue(maxsize=self._maxsize)
        self.subscribers.add(q)
        logger.info("%s subscriber added, total=%d", self._label, len(self.subscribers))
        return q

    def unsubscribe(self, q: asyncio.Queue[T]) -> None:
        self.subscribers.discard(q)
        logger.info(
            "%s subscriber removed, total=%d", self._label, len(self.subscribers)
        )

    def _publish(self, item: T) -> None:
        for q in self.subscribers:
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                logger.warning("%s queue full, dropping event", self._label)


class Game(Broadcast[schemas.GameStreamEvent]):
    # A ring buffer: an engine can print per node.
    UCI_TAIL = 400

    def __init__(self, game_id: UUID):
        super().__init__(label=f"game={game_id}", maxsize=256)
        self.game_id = game_id
        self.uci_lines: deque[schemas.UciLine] = deque(maxlen=self.UCI_TAIL)

    async def broadcast(self, event: schemas.GameStreamEvent) -> None:
        if isinstance(event, schemas.UciLogEvent):
            self.uci_lines.extend(event.lines)
        self._publish(event)


class LiveStream(Broadcast[tuple[UUID, schemas.GameStreamEvent]]):
    """The home page's feed of up to LIMIT live games. When one ends, another
    playing game is promoted with a snapshot FenEvent."""

    LIMIT = 8

    def __init__(self) -> None:
        super().__init__(label="live stream", maxsize=512)
        self.tracked: set[UUID] = set()

    async def broadcast(self, game_id: UUID, event: schemas.GameStreamEvent) -> None:
        # The home page shows only boards and clocks.
        if isinstance(event, schemas.EngineInfoEvent | schemas.UciLogEvent):
            return
        if game_id in self.tracked:
            self._publish((game_id, event))
            if isinstance(event, schemas.GameEndEvent):
                self.tracked.discard(game_id)
                await self._promote()
        elif (
            isinstance(event, schemas.GameStartEvent) and len(self.tracked) < self.LIMIT
        ):
            self.tracked.add(game_id)
            self._publish((game_id, event))
        elif isinstance(event, schemas.GameEndEvent):
            # A subscriber may know it from the initial /game fetch.
            self._publish((game_id, event))

    async def _promote(self) -> None:
        for gid in list(game_registry.registry.keys()):
            if gid in self.tracked:
                continue
            doc = await GameDoc.get(gid)
            if doc is None or doc.status != GameStatus.PLAYING:
                continue
            self.tracked.add(gid)
            self._publish((gid, _snapshot_event(doc)))
            return


class RunnerStream(Broadcast[RunnerLiveEvent]):
    def __init__(self) -> None:
        super().__init__(label="runner stream", maxsize=256)

    def broadcast(self, event: RunnerLiveEvent) -> None:
        self._publish(event)


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

    def unregister(self, game_id: UUID) -> Game | None:
        return self.registry.pop(game_id, None)


game_registry = GameRegistry()
live_stream = LiveStream()
runner_stream = RunnerStream()
