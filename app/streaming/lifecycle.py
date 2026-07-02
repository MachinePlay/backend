"""Game lifecycle: persisting stream events and the single terminal path that
runs when a game ends or is aborted, plus the hook registry schedulers use to
react to finished games."""

import logging
from collections.abc import Awaitable, Callable
from uuid import UUID

from machineplay import schemas
from machineplay.schemas import GameStatus

from app import runners
from app.models import Game as GameDoc, utcnow
from app.streaming.streams import game_registry, live_stream

logger = logging.getLogger(__name__)


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
    """Mark any DB games still in PLAYING as aborted (e.g. after backend restart).

    Only PLAYING games are orphaned — their runner is gone. PENDING games were
    never dispatched, so they're left for a scheduler to pick up (tournaments).
    """
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
