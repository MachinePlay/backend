from uuid import uuid4

import pytest

from machineplay import schemas
from machineplay.schemas import GameStatus
from app.models import Game
from app.streaming import persist_event


async def _make_game() -> Game:
    g = Game(
        white_id=uuid4(),
        black_id=uuid4(),
        white_name="white",
        black_name="black",
    )
    await g.insert()
    return g


async def test_move_event_pushes_san_atomically() -> None:
    g = await _make_game()

    await persist_event(
        g.id,
        schemas.MoveEvent(
            uci="e2e4",
            san="e4",
            from_square="e2",
            to_square="e4",
            fen="fen-after-e4",
            ply=1,
            white_clock=29.5,
            black_clock=30.0,
        ),
    )
    await persist_event(
        g.id,
        schemas.MoveEvent(
            uci="e7e5",
            san="e5",
            from_square="e7",
            to_square="e5",
            fen="fen-after-e5",
            ply=2,
            white_clock=29.5,
            black_clock=29.4,
        ),
    )

    refreshed = await Game.get(g.id)
    assert refreshed is not None
    assert refreshed.moves == ["e4", "e5"]
    assert refreshed.fen == "fen-after-e5"
    assert refreshed.white_clock == 29.5
    assert refreshed.black_clock == 29.4


async def test_game_end_event_sets_status_and_pgn() -> None:
    g = await _make_game()

    await persist_event(
        g.id,
        schemas.GameEndEvent(result="1-0", pgn="1. e4 e5 1-0"),
    )

    refreshed = await Game.get(g.id)
    assert refreshed is not None
    assert refreshed.status == GameStatus.ENDED
    assert refreshed.result == "1-0"
    assert refreshed.pgn == "1. e4 e5 1-0"
    assert refreshed.ended_at is not None


async def test_unknown_game_id_is_a_noop(caplog: pytest.LogCaptureFixture) -> None:
    missing_id = uuid4()
    with caplog.at_level("WARNING"):
        await persist_event(
            missing_id,
            schemas.MoveEvent(
                uci="e2e4",
                san="e4",
                from_square="e2",
                to_square="e4",
                fen="x",
                ply=1,
                white_clock=0.0,
                black_clock=0.0,
            ),
        )
    assert any("unknown game_id" in r.message for r in caplog.records)
