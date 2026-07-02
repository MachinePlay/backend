"""The SSE event sources the API routes yield from: one per game, the
home-page live aggregate, and the runners feed."""

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
