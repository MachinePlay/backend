"""Live streaming: the fan-out buses, game lifecycle/persistence, the runner
WebSocket session, and the SSE event sources.

Split across submodules but presented as one ``streaming`` namespace so callers
keep using ``streaming.game_registry``, ``streaming.finish_game``, etc.:

- ``streams``   — the fan-out buses (``Game``, ``LiveStream``, ``RunnerStream``),
                  the game registry, and their singletons.
- ``lifecycle`` — persisting game events and the terminal path (finish/abort),
                  plus the game-finished hook registry.
- ``session``   — the runner WebSocket lifecycle.
- ``sse``       — the SSE event generators the API routes yield from.
"""

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

# Re-exported for the test suite, which registers a hook and removes it in
# cleanup (`streaming._game_finished_hooks`); it shares the same list object.
# Redundant alias marks the re-export as intentional for the linter.
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
