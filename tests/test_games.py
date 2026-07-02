from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from machineplay import schemas as ws_schemas

from app import games, runners, streaming
from app.auth import mint_token
from app.main import app
from app.models import Engine, EngineVersion, Game, Runner, User


async def _setup() -> tuple[dict[str, str], User, Engine, EngineVersion]:
    user = await User(github_id=20, login="player").insert()
    token = await mint_token(user)
    engine = await Engine(name="bot", owner=user, owner_login=user.login).insert()
    version = await EngineVersion(
        engine=engine,
        version="v1",
        image_repository="player/bot",
        image_digest="sha256:abc",
    ).insert()
    return {"Authorization": f"Bearer {token}"}, user, engine, version


async def test_start_game_rejects_foreign_version() -> None:
    headers, user, engine, _ = await _setup()
    other = await Engine(name="other", owner=user, owner_login="player").insert()
    other_version = await EngineVersion(
        engine=other,
        version="v1",
        image_repository="player/other",
        image_digest="sha256:def",
    ).insert()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/game",
            headers=headers,
            json={
                "white_engine_id": str(engine.id),
                "black_engine_id": str(engine.id),
                "runner_id": str(uuid4()),
                # A real version, but of a different engine.
                "white_version_id": str(other_version.id),
            },
        )
    assert resp.status_code == 404
    assert "no such version" in resp.json()["error"]["message"]


async def test_start_game_resolves_versions_before_runner() -> None:
    headers, _, engine, version = await _setup()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/game",
            headers=headers,
            json={
                "white_engine_id": str(engine.id),
                "black_engine_id": str(engine.id),
                "runner_id": str(uuid4()),
                "white_version_id": str(version.id),
            },
        )
    # Versions resolved fine; the request dies on the (nonexistent) runner.
    assert resp.status_code == 404
    assert "runner" in resp.json()["error"]["message"]


async def _online_runner(user: User, max_games: int) -> runners.RunnerConnection:
    doc = await Runner(
        id=uuid4(),
        owner=user,
        owner_login=user.login,
        name="box",
        max_games=max_games,
    ).insert()
    return runners.mark_online(doc.id, max_games=max_games)


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_start_game_schedules_and_persists() -> None:
    headers, user, engine, version = await _setup()
    conn = await _online_runner(user, max_games=2)
    doc = None
    try:
        async with _client() as client:
            resp = await client.post(
                "/game",
                headers=headers,
                json={
                    "white_engine_id": str(engine.id),
                    "black_engine_id": str(engine.id),
                    "runner_id": str(conn.runner_id),
                    "tc": "10+0.1",
                },
            )
        assert resp.status_code == 200
        game_id = resp.json()["id"]

        doc = await Game.get(game_id)
        assert doc is not None
        assert doc.tc == "10+0.1"
        assert doc.runner_id == conn.runner_id
        assert doc.white_version_id == version.id
        assert doc.black_version_id == version.id
        # Scheduling flipped the game from PENDING to PLAYING.
        assert doc.status == ws_schemas.GameStatus.PLAYING

        # The slot is reserved and the start command is queued with the tc.
        assert conn.active_games == 1
        cmd = conn.scheduled_commands.get_nowait()
        assert isinstance(cmd, ws_schemas.StartGame)
        assert cmd.tc == "10+0.1"
    finally:
        if doc is not None:
            streaming.game_registry.unregister(doc.id)
            conn.untrack_game(doc.id)
        runners.mark_offline(conn)


async def test_create_game_pending_until_scheduled() -> None:
    _, user, engine, version = await _setup()
    # create_game pins the pairing but leaves it PENDING — not on any runner yet.
    doc = await games.create_game(engine, engine, version, version, tc="10+0.1")
    try:
        assert doc.status == ws_schemas.GameStatus.PENDING
        fetched = await Game.get(doc.id)
        assert fetched is not None
        assert fetched.status == ws_schemas.GameStatus.PENDING

        conn = await _online_runner(user, max_games=1)
        try:
            await games.schedule_game(doc, conn, version, version)
            # The slot is reserved and the doc is flipped to PLAYING.
            assert conn.active_games == 1
            scheduled = await Game.get(doc.id)
            assert scheduled is not None
            assert scheduled.status == ws_schemas.GameStatus.PLAYING
        finally:
            streaming.game_registry.unregister(doc.id)
            conn.untrack_game(doc.id)
            runners.mark_offline(conn)
    finally:
        await doc.delete()


async def test_start_game_full_runner_rejected_without_orphan_doc() -> None:
    headers, user, engine, _ = await _setup()
    conn = await _online_runner(user, max_games=0)
    try:
        async with _client() as client:
            resp = await client.post(
                "/game",
                headers=headers,
                json={
                    "white_engine_id": str(engine.id),
                    "black_engine_id": str(engine.id),
                    "runner_id": str(conn.runner_id),
                },
            )
        assert resp.status_code == 503
        # The rejected game must not linger as a forever-'playing' doc.
        assert await Game.find_all().count() == 0
    finally:
        runners.mark_offline(conn)


async def test_start_game_rejects_bad_tc() -> None:
    headers, user, engine, _ = await _setup()
    conn = await _online_runner(user, max_games=2)
    try:
        async with _client() as client:
            resp = await client.post(
                "/game",
                headers=headers,
                json={
                    "white_engine_id": str(engine.id),
                    "black_engine_id": str(engine.id),
                    "runner_id": str(conn.runner_id),
                    "tc": "banana",
                },
            )
        assert resp.status_code == 409
        assert await Game.find_all().count() == 0
    finally:
        runners.mark_offline(conn)


async def test_cancel_game_aborts_and_tells_runner() -> None:
    headers, user, engine, _ = await _setup()
    conn = await _online_runner(user, max_games=2)
    try:
        async with _client() as client:
            resp = await client.post(
                "/game",
                headers=headers,
                json={
                    "white_engine_id": str(engine.id),
                    "black_engine_id": str(engine.id),
                    "runner_id": str(conn.runner_id),
                },
            )
            assert resp.status_code == 200
            game_id = resp.json()["id"]
            conn.scheduled_commands.get_nowait()  # drop the StartGame

            resp = await client.post(f"/game/{game_id}/cancel", headers=headers)
            assert resp.status_code == 200

            doc = await Game.get(game_id)
            assert doc is not None
            assert doc.status == ws_schemas.GameStatus.ABORTED
            assert doc.reason == "cancelled"
            # Slot freed and the runner was told to kill the game.
            assert conn.active_games == 0
            stop = conn.scheduled_commands.get_nowait()
            assert isinstance(stop, ws_schemas.StopGame)
            assert str(stop.game_id) == game_id

            # Cancelling a game that isn't playing is a conflict.
            resp = await client.post(f"/game/{game_id}/cancel", headers=headers)
            assert resp.status_code == 409
    finally:
        runners.mark_offline(conn)
