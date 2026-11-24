"""Persisting stream events, and the single terminal path for ending games."""

import logging
from collections.abc import Awaitable, Callable
from uuid import UUID

from machineplay import schemas
from machineplay.schemas import GameStatus

from app import runners
from app.models import Game as GameDoc, utcnow
from app.streaming.streams import game_registry, live_stream

logger = logging.getLogger(__name__)


# Lets tournaments advance when a game ends.
GameFinishedHook = Callable[[UUID, schemas.GameEndEvent], Awaitable[None]]
_game_finished_hooks: list[GameFinishedHook] = []


def on_game_finished(hook: GameFinishedHook) -> None:
    _game_finished_hooks.append(hook)


async def finish_game(game_id: UUID, event: schemas.GameEndEvent) -> None:
    """Single terminal path for runner-reported ends and server-side aborts."""
    runners.untrack_game(game_id)
    game = game_registry.unregister(game_id)
    await persist_event(game_id, event)
    if game is not None:
        if game.uci_lines:
            await GameDoc.find_one(GameDoc.id == game_id).update(
                {"$set": {"uci_tail": [line.model_dump() for line in game.uci_lines]}}
            )
        await game.broadcast(event)
    await live_stream.broadcast(game_id, event)
    for hook in _game_finished_hooks:
        try:
            await hook(game_id, event)
        except Exception:
            logger.exception("game-finished hook failed for game=%s", game_id)


async def abort_game(game_id: UUID, reason: str = "aborted") -> None:
    """Abort a playing game; no-op for one that already ended."""
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
    """Abort games left PLAYING by a backend restart. PENDING ones are left for
    their tournament to dispatch."""
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
        case schemas.EngineInfoEvent() | schemas.UciLogEvent():
            # Live-only; the transcript tail is saved when the game ends.
            return
        case schemas.GameStartEvent():
            update["$set"] = {"status": GameStatus.PLAYING}
        case schemas.FenEvent(
            fen=fen, moves=moves, evals=evals, white_clock=wc, black_clock=bc
        ):
            update["$set"] = {
                "fen": fen,
                "moves": list(moves),
                "evals": [e.model_dump() if e else None for e in evals],
                "white_clock": wc,
                "black_clock": bc,
            }
        case schemas.MoveEvent(
            san=san, fen=fen, analysis=analysis, white_clock=wc, black_clock=bc
        ):
            update["$push"] = {
                "moves": san,
                "evals": analysis.model_dump() if analysis else None,
            }
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
