"""Live streaming, re-exported as one namespace: fan-out buses (``streams``),
game persistence and ending (``lifecycle``), the runner WebSocket
(``session``) and SSE sources (``sse``)."""

from app.streaming.lifecycle import (
    GameFinishedHook,
    abort_game,
    abort_orphan_games,
    finish_game,
    on_game_finished,
    persist_event,
)
from app.streaming.session import runner_session
from app.streaming.sse import (
    game_event_stream,
    live_event_stream,
    runner_event_stream,
)
from app.streaming.streams import (
    Broadcast,
    Game,
    GameRegistry,
    LiveStream,
    RunnerStream,
    game_registry,
    live_stream,
    runner_stream,
)

# For tests that remove the hooks they register.
from app.streaming.lifecycle import _game_finished_hooks as _game_finished_hooks

__all__ = [
    "Broadcast",
    "Game",
    "GameFinishedHook",
    "GameRegistry",
    "LiveStream",
    "RunnerStream",
    "abort_game",
    "abort_orphan_games",
    "finish_game",
    "game_event_stream",
    "game_registry",
    "live_event_stream",
    "live_stream",
    "on_game_finished",
    "persist_event",
    "runner_event_stream",
    "runner_session",
    "runner_stream",
]
