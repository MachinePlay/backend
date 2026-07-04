import json
from base64 import b64encode

import pytest
from httpx import ASGITransport, AsyncClient

from machineplay.schemas import GameStatus

from app.config import settings
from app.main import app
from app.models import Engine, EngineVersion, Game, User
from itsdangerous import TimestampSigner


def _session_cookie(user: User) -> str:
    """Forge a logged-in session cookie the way SessionMiddleware signs it."""
    payload = b64encode(json.dumps({"user_id": str(user.id)}).encode("utf-8"))
    return TimestampSigner(settings.secret_key).sign(payload).decode("utf-8")


def _client(as_user: User | None = None) -> AsyncClient:
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    if as_user is not None:
        client.cookies.set("session", _session_cookie(as_user))
    return client


async def _engine_with_version(
    owner: User, name: str, digest: str = "sha256:aaa", repository: str | None = None
) -> Engine:
    engine = await Engine(name=name, owner=owner, owner_login=owner.login).insert()
    await EngineVersion(
        engine=engine,
        version="v1",
        image_repository=repository or f"{owner.login}/{name}",
        image_digest=digest,
    ).insert()
    return engine


def _game(white: Engine, black: Engine, status: GameStatus) -> Game:
    return Game(
        white=white,
        black=black,
        white_name=white.name,
        black_name=black.name,
        white_version="v1",
        black_version="v1",
        status=status,
    )


@pytest.fixture
def manifest_deletes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Record registry manifest deletes instead of calling a real registry."""
    calls: list[tuple[str, str]] = []

    async def fake(repository: str, digest: str) -> None:
        calls.append((repository, digest))

    monkeypatch.setattr("app.registry.delete_manifest", fake)
    return calls


async def test_list_engines_empty() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/engine")
    assert response.status_code == 200
    assert response.json() == []


async def test_list_engines_returns_inserted() -> None:
    owner = await User(github_id=100, login="alice").insert()
    await Engine(
        name="stockfish", description="sf", owner=owner, owner_login="alice"
    ).insert()
    await Engine(name="lc0", owner=owner, owner_login="alice").insert()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/engine")

    assert response.status_code == 200
    body = response.json()
    assert {e["name"] for e in body} == {"stockfish", "lc0"}
    sf = next(e for e in body if e["name"] == "stockfish")
    assert sf["description"] == "sf"


async def test_delete_engine_owner(manifest_deletes: list[tuple[str, str]]) -> None:
    alice = await User(github_id=1, login="alice").insert()
    engine = await _engine_with_version(alice, "sf", digest="sha256:aaa")
    other = await _engine_with_version(alice, "lc0", digest="sha256:bbb")
    # A finished game survives the delete: it renders from denormalized names.
    game = await _game(engine, other, GameStatus.ENDED).insert()

    async with _client(as_user=alice) as client:
        resp = await client.delete("/user/alice/sf")

    assert resp.status_code == 200
    assert await Engine.get(engine.id) is None
    assert await EngineVersion.find({"engine.$id": engine.id}).count() == 0
    assert await Game.get(game.id) is not None
    assert manifest_deletes == [("alice/sf", "sha256:aaa")]


async def test_delete_engine_requires_owner_or_admin(
    manifest_deletes: list[tuple[str, str]],
) -> None:
    alice = await User(github_id=1, login="alice").insert()
    bob = await User(github_id=2, login="bob").insert()
    admin = await User(github_id=3, login="root", is_admin=True).insert()
    await _engine_with_version(alice, "sf")

    async with _client() as client:
        assert (await client.delete("/user/alice/sf")).status_code == 401
    async with _client(as_user=bob) as client:
        assert (await client.delete("/user/alice/sf")).status_code == 403
    async with _client(as_user=admin) as client:
        assert (await client.delete("/user/alice/sf")).status_code == 200
    assert await Engine.find_all().count() == 0


async def test_delete_engine_blocked_while_games_active(
    manifest_deletes: list[tuple[str, str]],
) -> None:
    alice = await User(github_id=1, login="alice").insert()
    engine = await _engine_with_version(alice, "sf")
    other = await _engine_with_version(alice, "lc0")
    await _game(other, engine, GameStatus.PLAYING).insert()

    async with _client(as_user=alice) as client:
        resp = await client.delete("/user/alice/sf")

    assert resp.status_code == 409
    assert await Engine.get(engine.id) is not None
    assert manifest_deletes == []


async def test_delete_engine_keeps_manifests_other_versions_still_reference(
    manifest_deletes: list[tuple[str, str]],
) -> None:
    alice = await User(github_id=1, login="alice").insert()
    # Both engines' versions point at the same image; only the digest unique to
    # the deleted engine may be removed from the registry.
    await _engine_with_version(
        alice, "sf", digest="sha256:shared", repository="alice/sf"
    )
    keeper = await Engine(name="sf2", owner=alice, owner_login="alice").insert()
    await EngineVersion(
        engine=keeper,
        version="v1",
        image_repository="alice/sf",
        image_digest="sha256:shared",
    ).insert()
    await EngineVersion(
        engine=(await Engine.find_one(Engine.name == "sf")),
        version="v2",
        image_repository="alice/sf",
        image_digest="sha256:unique",
    ).insert()

    async with _client(as_user=alice) as client:
        resp = await client.delete("/user/alice/sf")

    assert resp.status_code == 200
    assert manifest_deletes == [("alice/sf", "sha256:unique")]
