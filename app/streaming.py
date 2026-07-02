import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from uuid import UUID

from fastapi import WebSocket, WebSocketDisconnect

from machineplay import schemas
from machineplay.schemas import GameStatus

from app import auth, runners
from app.exceptions import NotFoundError
from app.models import Game as GameDoc, utcnow
from app.schemas import LiveStreamEvent, RunnerLiveEvent

logger = logging.getLogger(__name__)


def _snapshot_event(doc: GameDoc) -> schemas.FenEvent:
    """A FenEvent carrying the full current state of `doc`, for bootstrapping."""
    return schemas.FenEvent(
        fen=doc.fen,
        ply=len(doc.moves),
        white_name=doc.white_name,
        black_name=doc.black_name,
        moves=doc.moves,
        white_clock=doc.white_clock,
        black_clock=doc.black_clock,
        result=doc.result,
        status=doc.status,
        game_id=doc.id,
    )


# Called with (game_id, end event) after a game reaches a terminal state and
# is persisted. This is the hook point for schedulers that react to finished
# games (e.g. a tournament advancing to its next pairing).
GameFinishedHook = Callable[[UUID, schemas.GameEndEvent], Awaitable[None]]
_game_finished_hooks: list[GameFinishedHook] = []


def on_game_finished(hook: GameFinishedHook) -> None:
    _game_finished_hooks.append(hook)


async def finish_game(game_id: UUID, event: schemas.GameEndEvent) -> None:
    """Single terminal path for a game: free its runner slot, persist the end
    state, drop the live stream, fan out to subscribers, and notify hooks.

    Used both for runner-reported ends and server-side aborts.
    """
    runners.untrack_game(game_id)
    game = game_registry.unregister(game_id)
    await persist_event(game_id, event)
    if game is not None:
        await game.broadcast(event)
    await live_stream.broadcast(game_id, event)
    for hook in _game_finished_hooks:
        try:
            await hook(game_id, event)
        except Exception:
            logger.exception("game-finished hook failed for game=%s", game_id)


async def abort_game(game_id: UUID, reason: str = "aborted") -> None:
    """Mark a playing game as aborted in DB and notify subscribers. Games that
    already reached a terminal state are left untouched (idempotent)."""
    doc = await GameDoc.get(game_id)
    if doc is None or doc.status != GameStatus.PLAYING:
        game_registry.unregister(game_id)
        runners.untrack_game(game_id)
        return
    logger.info("aborting game=%s (%s)", game_id, reason)
    await finish_game(
        game_id,
        schemas.GameEndEvent(
            result="*", pgn=None, status=GameStatus.ABORTED, reason=reason
        ),
    )


async def abort_orphan_games() -> None:
    """Mark any DB games still in PLAYING as aborted (e.g. after backend restart)."""
    orphans = await GameDoc.find(GameDoc.status == GameStatus.PLAYING).to_list()
    for doc in orphans:
        doc.status = GameStatus.ABORTED
        doc.result = "*"
        doc.reason = "backend restarted"
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
        case schemas.GameEndEvent(result=result, pgn=pgn, status=status, reason=reason):
            set_fields: dict[str, object] = {
                "status": status,
                "result": result,
                "reason": reason,
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
            self._fanout(gid, _snapshot_event(doc))
            return

    def _fanout(self, game_id: UUID, event: schemas.GameStreamEvent) -> None:
        for q in self.subscribers:
            try:
                q.put_nowait((game_id, event))
            except asyncio.QueueFull:
                logger.warning(
                    "live stream queue full, dropping event type=%s", event.type
                )


class RunnerStream:
    """Fans out runner live-status events (RunnerLiveEvent) to all subscribers.

    Simpler than LiveStream: no tracking/promotion, just a broadcast bus. The
    runner_session pushes an event on connect, on each telemetry sample, and on
    disconnect; every SSE subscriber gets a copy keyed by runner id.
    """

    def __init__(self) -> None:
        self.subscribers: set[asyncio.Queue[RunnerLiveEvent]] = set()

    def subscribe(self) -> asyncio.Queue[RunnerLiveEvent]:
        q: asyncio.Queue[RunnerLiveEvent] = asyncio.Queue(maxsize=256)
        self.subscribers.add(q)
        logger.info("runner stream subscriber added, total=%d", len(self.subscribers))
        return q

    def unsubscribe(self, q: asyncio.Queue[RunnerLiveEvent]) -> None:
        self.subscribers.discard(q)
        logger.info("runner stream subscriber removed, total=%d", len(self.subscribers))

    def broadcast(self, event: RunnerLiveEvent) -> None:
        for q in self.subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("runner stream queue full, dropping event")


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


async def runner_session(ws: WebSocket) -> None:
    """Lifecycle of one connected runner: authenticate it, upsert its durable
    record, mark it online, pump game events from the socket into persistence +
    fan-out, pump scheduled commands back, and abort its games + mark it offline
    when it disconnects."""
    # Authenticate on the handshake (before accept) so an unknown token is a 403
    # rejection rather than an accepted-then-closed socket.
    user = await auth.user_from_auth_header(ws.headers.get("Authorization", ""))
    if user is None:
        logger.warning("runner WS rejected: missing/invalid bearer token")
        await ws.close(code=1008)
        return

    await ws.accept()

    intro = schemas.Introduction.model_validate_json(await ws.receive_text())
    doc = await runners.upsert_on_connect(
        user, intro.runner_id, intro.name, intro.max_games, intro.hardware
    )
    if doc is None:
        # Runner id belongs to someone else — refuse to let them impersonate it.
        await ws.close(code=1008)
        return

    runner = runners.mark_online(intro.runner_id, intro.max_games)
    runner_stream.broadcast(runners.live_event(runner))
    logger.info(
        "runner connected id=%s name=%s owner=%s max_games=%d",
        intro.runner_id,
        doc.name,
        user.login,
        intro.max_games,
    )

    async def receiver() -> None:
        while True:
            data = await ws.receive_text()
            cmd: schemas.ClientCommandType = schemas.client_adapter.validate_json(data)
            match cmd:
                case schemas.GameEvent(game_id=game_id, event=event):
                    # Only accept events for games scheduled on this runner —
                    # one runner must not be able to write another's games.
                    if not runner.is_playing(game_id):
                        logger.warning(
                            "runner=%s sent event for game=%s it doesn't play",
                            runner.runner_id,
                            game_id,
                        )
                        continue
                    if isinstance(event, schemas.GameEndEvent):
                        await finish_game(game_id, event)
                        continue
                    try:
                        game = game_registry.get_game(game_id)
                    except NotFoundError:
                        logger.warning("event for unregistered game_id=%s", game_id)
                        continue
                    await persist_event(game_id, event)
                    await game.broadcast(event)
                    await live_stream.broadcast(game_id, event)
                case schemas.Telemetry() as telemetry:
                    runner.update_telemetry(telemetry)
                    runner_stream.broadcast(runners.live_event(runner))
                case schemas.Introduction():
                    logger.warning("unexpected duplicate intro from runner")

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
        # Take the runner offline first so nothing new gets scheduled onto it,
        # then abort whatever it was mid-game on. The durable doc stays; only
        # the live connection goes away. If the runner already reconnected
        # (mark_offline returns False because a newer connection replaced this
        # one), the runner is still online — don't broadcast it offline.
        went_offline = runners.mark_offline(runner)
        if went_offline:
            runner_stream.broadcast(
                RunnerLiveEvent(
                    runner_id=intro.runner_id,
                    online=False,
                    active_games=0,
                    telemetry=None,
                )
            )
        # This session's games died with its runner process either way.
        for game_id in runner.game_ids():
            await abort_game(game_id, reason="runner disconnected")
        await runners.touch_last_seen(intro.runner_id)


async def game_event_stream(game_id: UUID) -> AsyncIterator[schemas.GameStreamEvent]:
    """Event source for one game's SSE endpoint: snapshot, then live events."""
    try:
        game = game_registry.get_game(game_id)
    except NotFoundError:
        # Game is no longer live; if it exists in the DB, emit a single
        # terminal event so late subscribers see a clean end rather than 404.
        doc = await GameDoc.get(game_id)
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
        doc = await GameDoc.get(game_id)
        snapshot_ply = -1
        if doc is not None:
            snapshot_ply = len(doc.moves)
            yield _snapshot_event(doc)

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


async def live_event_stream() -> AsyncIterator[LiveStreamEvent]:
    """Event source for the all-games SSE endpoint."""
    q = live_stream.subscribe()
    try:
        while True:
            game_id, event = await q.get()
            yield LiveStreamEvent(game_id=game_id, event=event)
    finally:
        live_stream.unsubscribe(q)


async def runner_event_stream() -> AsyncIterator[RunnerLiveEvent]:
    """Event source for the runners SSE endpoint: a snapshot of every online
    runner's current status, then live connect/telemetry/disconnect events.

    Subscribe before snapshotting so nothing is missed in the gap; a runner that
    both appears in the snapshot and emits a live event just gets merged twice on
    the client (keyed by runner id), which is harmless.
    """
    q = runner_stream.subscribe()
    try:
        for event in runners.live_snapshot():
            yield event
        while True:
            yield await q.get()
    finally:
        runner_stream.unsubscribe(q)
