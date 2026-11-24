"""SSE event sources: one per game, the home-page aggregate, and runners."""

import asyncio
import logging
from collections.abc import AsyncIterator
from uuid import UUID

from machineplay import schemas

from app import runners
from app.exceptions import NotFoundError
from app.models import Game as GameDoc
from app.schemas import LiveStreamEvent, RunnerLiveEvent
from app.streaming.streams import (
    _snapshot_event,
    game_registry,
    live_stream,
    runner_stream,
)

logger = logging.getLogger(__name__)


async def game_event_stream(game_id: UUID) -> AsyncIterator[schemas.GameStreamEvent]:
    """Snapshot, then live events."""
    try:
        game = game_registry.get_game(game_id)
    except NotFoundError:
        # Not live anymore: a finished game gets a single end event, not a 404.
        doc = await GameDoc.get(game_id)
        if doc is None:
            raise
        yield schemas.GameEndEvent(result=doc.result, pgn=doc.pgn)
        return

    q = game.subscribe()
    try:
        # Subscribe, then snapshot: the receiver writes the DB before
        # broadcasting, so queued events up to the snapshot's ply are dupes.
        doc = await GameDoc.get(game_id)
        snapshot_ply = -1
        if doc is not None:
            snapshot_ply = len(doc.moves)
            yield _snapshot_event(doc)
        if game.uci_lines:
            yield schemas.UciLogEvent(lines=list(game.uci_lines))

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
    q = live_stream.subscribe()
    try:
        while True:
            game_id, event = await q.get()
            yield LiveStreamEvent(game_id=game_id, event=event)
    finally:
        live_stream.unsubscribe(q)


async def runner_event_stream() -> AsyncIterator[RunnerLiveEvent]:
    """Snapshot of online runners, then live events. Subscribing first may
    deliver a runner twice; the client merges by id."""
    q = runner_stream.subscribe()
    try:
        for event in runners.live_snapshot():
            yield event
        while True:
            yield await q.get()
    finally:
        runner_stream.unsubscribe(q)
