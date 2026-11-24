"""The runner WebSocket session: one connected runner's whole lifecycle."""

import asyncio
import logging

from fastapi import WebSocket, WebSocketDisconnect

from machineplay import schemas

from app import auth, runners
from app.exceptions import NotFoundError
from app.schemas import RunnerLiveEvent
from app.streaming.lifecycle import abort_game, finish_game, persist_event
from app.streaming.streams import game_registry, live_stream, runner_stream

logger = logging.getLogger(__name__)


async def runner_session(ws: WebSocket) -> None:
    # Authenticate before accept, so a bad token is a 403, not a closed socket.
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
        # The runner id belongs to someone else.
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
    # Tournaments enqueue their pending pairings; the sender drains them below.
    await runners.notify_connected(intro.runner_id)

    async def receiver() -> None:
        while True:
            data = await ws.receive_text()
            cmd: schemas.ClientCommandType = schemas.client_adapter.validate_json(data)
            match cmd:
                case schemas.GameEvent(game_id=game_id, event=event):
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
        # Offline first, so nothing new is scheduled onto it. A runner that
        # already reconnected stays online.
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
        for game_id in runner.game_ids():
            await abort_game(game_id, reason="runner disconnected")
        await runners.touch_last_seen(intro.runner_id)
