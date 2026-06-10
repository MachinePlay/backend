from uuid import uuid4

from httpx import ASGITransport, AsyncClient

from app.auth import mint_token
from app.main import app
from app.models import Engine, EngineVersion, User


async def _setup() -> tuple[dict[str, str], Engine, EngineVersion]:
    user = await User(github_id=20, login="player").insert()
    token = await mint_token(user)
    engine = await Engine(name="bot", owner_id=user.id, owner_login=user.login).insert()
    version = await EngineVersion(
        engine_id=engine.id,
        version="v1",
        image_repository="player/bot",
        image_digest="sha256:abc",
    ).insert()
    return {"Authorization": f"Bearer {token}"}, engine, version


async def test_start_game_rejects_foreign_version() -> None:
    headers, engine, _ = await _setup()
    other = await Engine(
        name="other", owner_id=engine.owner_id, owner_login="player"
    ).insert()
    other_version = await EngineVersion(
        engine_id=other.id,
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
    headers, engine, version = await _setup()

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
