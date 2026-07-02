from uuid import uuid4

from fastapi import WebSocketDisconnect
from httpx import ASGITransport, AsyncClient
from machineplay import schemas as ws_schemas
from machineplay.schemas import HardwareInfo

from app import runners, streaming
from app.auth import mint_token
from app.main import app
from app.models import Engine, Game, Runner, User
from app.schemas import RunnerUpdateRequest

_HW = HardwareInfo(
    cpu_model="Test CPU",
    cpu_physical_cores=8,
    cpu_logical_cores=16,
    ram_total_bytes=32 * 1024**3,
)


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_list_runners_online_status() -> None:
    owner = await User(github_id=1, login="alice").insert()
    doc = await Runner(
        id=uuid4(), owner=owner, owner_login="alice", name="box", max_games=4
    ).insert()

    async with _client() as client:
        resp = await client.get("/runners")
    assert resp.status_code == 200
    [row] = resp.json()
    assert row["runner_id"] == str(doc.id)
    assert row["online"] is False
    assert row["active_games"] == 0

    # A live connection flips it online.
    conn = runners.mark_online(doc.id, max_games=4)
    try:
        async with _client() as client:
            resp = await client.get("/runners")
        [row] = resp.json()
        assert row["online"] is True
        assert row["max_games"] == 4
    finally:
        runners.mark_offline(conn)


async def test_stale_connection_cleanup_keeps_reconnected_runner_online() -> None:
    """A runner that reconnects before its old session's cleanup runs must stay
    online: the stale session's mark_offline must not pop the new connection."""
    runner_id = uuid4()
    old = runners.mark_online(runner_id, max_games=4)
    new = runners.mark_online(runner_id, max_games=4)  # reconnect supersedes

    assert runners.mark_offline(old) is False  # stale cleanup is a no-op
    assert runners.get_online(runner_id) is new
    assert runners.is_current(new)
    assert not runners.is_current(old)

    assert runners.mark_offline(new) is True
    assert not runners.is_current(new)


async def test_get_runner_detail() -> None:
    owner = await User(github_id=20, login="ada").insert()
    doc = await Runner(
        id=uuid4(), owner=owner, owner_login="ada", name="adabox", description="hi"
    ).insert()

    async with _client() as client:
        resp = await client.get(f"/runner/{doc.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["runner_id"] == str(doc.id)
    assert body["name"] == "adabox"
    assert body["description"] == "hi"
    assert body["online"] is False


async def test_get_unknown_runner_404() -> None:
    async with _client() as client:
        resp = await client.get(f"/runner/{uuid4()}")
    assert resp.status_code == 404


async def test_owner_can_edit_description() -> None:
    owner = await User(github_id=2, login="bob").insert()
    token = await mint_token(owner)
    doc = await Runner(
        id=uuid4(), owner=owner, owner_login="bob", name="bobbox"
    ).insert()

    async with _client() as client:
        resp = await client.patch(
            f"/runner/{doc.id}",
            headers={"Authorization": f"Bearer {token}"},
            json={"description": "my home server"},
        )
    assert resp.status_code == 200
    assert resp.json()["description"] == "my home server"
    refreshed = await Runner.get(doc.id)
    assert refreshed is not None and refreshed.description == "my home server"


async def test_non_owner_cannot_edit() -> None:
    owner = await User(github_id=3, login="carol").insert()
    other = await User(github_id=4, login="dave").insert()
    other_token = await mint_token(other)
    doc = await Runner(
        id=uuid4(), owner=owner, owner_login="carol", name="carolbox"
    ).insert()

    async with _client() as client:
        resp = await client.patch(
            f"/runner/{doc.id}",
            headers={"Authorization": f"Bearer {other_token}"},
            json={"description": "hijack"},
        )
    assert resp.status_code == 403


async def test_edit_unknown_runner_404() -> None:
    owner = await User(github_id=5, login="erin").insert()
    token = await mint_token(owner)

    async with _client() as client:
        resp = await client.patch(
            f"/runner/{uuid4()}",
            headers={"Authorization": f"Bearer {token}"},
            json={"description": "x"},
        )
    assert resp.status_code == 404


async def test_upsert_on_connect_creates_then_is_idempotent() -> None:
    owner = await User(github_id=6, login="frank").insert()
    runner_id = uuid4()

    created = await runners.upsert_on_connect(owner, runner_id, "franks-host", 8, _HW)
    assert created is not None
    assert created.owner_login == "frank"
    assert created.name == "franks-host"
    assert created.description == ""
    assert created.hardware == _HW
    first_seen = created.last_seen_at
    assert first_seen is not None

    # Owner customises the metadata.
    await runners.edit_runner(
        owner, runner_id, RunnerUpdateRequest(name="renamed", description="desc")
    )

    # Reconnecting keeps owner-managed fields and only refreshes liveness/capacity
    # and runner-reported hardware.
    new_hw = _HW.model_copy(update={"cpu_model": "Upgraded CPU"})
    again = await runners.upsert_on_connect(owner, runner_id, "franks-host", 16, new_hw)
    assert again is not None
    assert again.name == "renamed"
    assert again.description == "desc"
    assert again.max_games == 16
    assert again.hardware == new_hw
    assert again.last_seen_at is not None
    assert again.last_seen_at >= first_seen


async def test_upsert_rejects_owner_mismatch() -> None:
    owner = await User(github_id=7, login="grace").insert()
    intruder = await User(github_id=8, login="mallory").insert()
    runner_id = uuid4()

    assert await runners.upsert_on_connect(owner, runner_id, "host", 4, _HW) is not None
    # A different user claiming the same id is refused.
    assert await runners.upsert_on_connect(intruder, runner_id, "host", 4, _HW) is None
    doc = await Runner.get(runner_id)
    assert doc is not None and doc.owner_login == "grace"


class _FakeWS:
    """Minimal WebSocket double: hands back queued client messages in order,
    then raises WebSocketDisconnect (as Starlette does) to end the session."""

    def __init__(self, messages: list[str], authorization: str) -> None:
        self._messages = list(messages)
        self.headers = {"Authorization": authorization}
        self.sent: list[str] = []
        self.closed: int | None = None

    async def accept(self) -> None:
        pass

    async def receive_text(self) -> str:
        if self._messages:
            return self._messages.pop(0)
        raise WebSocketDisconnect(code=1006)

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000) -> None:
        self.closed = code


async def test_runner_ws_persists_hardware_and_streams_telemetry() -> None:
    owner = await User(github_id=30, login="heidi").insert()
    token = await mint_token(owner)
    runner_id = uuid4()

    intro = ws_schemas.Introduction(
        runner_id=runner_id, name="heidi-box", max_games=4, hardware=_HW
    )
    telemetry = ws_schemas.Telemetry(
        cpu_percent=42.5, ram_used_bytes=8 * 1024**3, ram_percent=25.0
    )
    ws = _FakeWS(
        [intro.model_dump_json(), telemetry.model_dump_json()],
        f"Bearer {token}",
    )

    feed = streaming.runner_stream.subscribe()
    try:
        await streaming.runner_session(ws)  # type: ignore[arg-type]
    finally:
        streaming.runner_stream.unsubscribe(feed)

    # Static hardware is persisted on the durable doc (survives offline).
    doc = await Runner.get(runner_id)
    assert doc is not None and doc.hardware == _HW

    # The live feed saw: connect (online, no telemetry), the telemetry sample,
    # then offline on disconnect.
    events = []
    while not feed.empty():
        events.append(feed.get_nowait())
    assert [(e.online, e.telemetry) for e in events] == [
        (True, None),
        (True, telemetry),
        (False, None),
    ]

    # Runner went offline, so it no longer appears in the live snapshot.
    assert runner_id not in {e.runner_id for e in runners.live_snapshot()}


async def _playing_game(owner: User) -> Game:
    engine = await Engine(name="e", owner=owner, owner_login=owner.login).insert()
    return await Game(
        white=engine,
        black=engine,
        white_name="e",
        black_name="e",
        white_version="v1",
        black_version="v1",
        status=ws_schemas.GameStatus.PLAYING,
    ).insert()


async def test_runner_ws_ignores_events_for_foreign_games() -> None:
    """A runner must not be able to write games it wasn't scheduled to play."""
    owner = await User(github_id=31, login="ivan").insert()
    token = await mint_token(owner)
    game = await _playing_game(owner)
    # The game is live, but scheduled on some *other* runner.
    streaming.game_registry.register_game(game.id)

    intro = ws_schemas.Introduction(
        runner_id=uuid4(), name="ivan-box", max_games=4, hardware=_HW
    )
    foreign_end = ws_schemas.GameEvent(
        game_id=game.id,
        event=ws_schemas.GameEndEvent(result="1-0", pgn="fake"),
    )
    ws = _FakeWS(
        [intro.model_dump_json(), foreign_end.model_dump_json()], f"Bearer {token}"
    )
    try:
        await streaming.runner_session(ws)  # type: ignore[arg-type]

        refreshed = await Game.get(game.id)
        assert refreshed is not None
        assert refreshed.status == ws_schemas.GameStatus.PLAYING
        assert refreshed.pgn is None
    finally:
        streaming.game_registry.unregister(game.id)


async def test_finish_game_persists_frees_slot_and_notifies_hooks() -> None:
    owner = await User(github_id=32, login="judy").insert()
    game = await _playing_game(owner)
    conn = runners.mark_online(uuid4(), max_games=2)
    conn.track_game(game.id)
    streaming.game_registry.register_game(game.id)

    seen: list[tuple[object, str | None]] = []

    async def hook(game_id: object, event: ws_schemas.GameEndEvent) -> None:
        seen.append((game_id, event.reason))

    streaming.on_game_finished(hook)
    try:
        await streaming.finish_game(
            game.id,
            ws_schemas.GameEndEvent(result="0-1", pgn="1. e4 0-1", reason="checkmate"),
        )
    finally:
        streaming._game_finished_hooks.remove(hook)
        runners.mark_offline(conn)

    refreshed = await Game.get(game.id)
    assert refreshed is not None
    assert refreshed.status == ws_schemas.GameStatus.ENDED
    assert refreshed.result == "0-1"
    assert refreshed.reason == "checkmate"
    assert conn.active_games == 0  # slot freed
    assert seen == [(game.id, "checkmate")]
