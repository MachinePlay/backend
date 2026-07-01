import json
from base64 import b64encode
from typing import Any

from httpx import ASGITransport, AsyncClient
from itsdangerous import TimestampSigner

from app.auth import mint_token
from app.config import settings
from app.main import app
from app.models import Engine, Game, User


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _session_cookie(data: dict[str, Any]) -> str:
    """Forge a session cookie the way Starlette's SessionMiddleware signs it."""
    payload = b64encode(json.dumps(data).encode("utf-8"))
    return TimestampSigner(settings.secret_key).sign(payload).decode("utf-8")


PENDING = {
    "pending_signup": {
        "github_id": 4242,
        "login": "NewUser",
        "name": "New User",
        "avatar_url": "https://example.com/a.png",
    }
}


async def test_pending_requires_signup_in_progress() -> None:
    async with _client() as client:
        assert (await client.get("/auth/pending")).status_code == 401
        resp = await client.post("/auth/register", json={"login": "foo"})
    assert resp.status_code == 401


async def test_pending_suggests_lowercased_github_login() -> None:
    async with _client() as client:
        client.cookies.set("session", _session_cookie(PENDING))
        resp = await client.get("/auth/pending")
    assert resp.status_code == 200
    assert resp.json()["suggested_login"] == "newuser"


async def test_register_creates_user_and_logs_in() -> None:
    async with _client() as client:
        client.cookies.set("session", _session_cookie(PENDING))
        resp = await client.post("/auth/register", json={"login": "My-Handle"})
        assert resp.status_code == 200
        assert resp.json()["login"] == "my-handle"
        # The session now carries user_id, so /me works.
        me = await client.get("/me")
    assert me.status_code == 200
    assert me.json()["github_id"] == 4242

    user = await User.find_one(User.github_id == 4242)
    assert user is not None and user.login == "my-handle"


async def test_register_rejects_invalid_and_taken_handles() -> None:
    await User(github_id=1, login="taken").insert()
    await User(github_id=2, login="Cased").insert()

    for bad in ["", "-foo", "foo-", "fo--o", "Foo!", "a" * 33, "machineplay"]:
        async with _client() as client:
            client.cookies.set("session", _session_cookie(PENDING))
            resp = await client.post("/auth/register", json={"login": bad})
        assert resp.status_code == 409, bad

    for taken in ["taken", "cased"]:  # case-insensitive collision
        async with _client() as client:
            client.cookies.set("session", _session_cookie(PENDING))
            resp = await client.post("/auth/register", json={"login": taken})
        assert resp.status_code == 409, taken


async def test_token_list_and_revoke() -> None:
    user = await User(github_id=7, login="tokenuser").insert()
    plaintext = await mint_token(user)
    headers = {"Authorization": f"Bearer {plaintext}"}

    async with _client() as client:
        listed = await client.get("/me/tokens", headers=headers)
        assert listed.status_code == 200
        tokens = listed.json()
        assert len(tokens) == 1
        assert plaintext.startswith(tokens[0]["prefix"])

        # Another user's token must be invisible / unrevokable.
        other = await User(github_id=8, login="other").insert()
        other_token = await mint_token(other)
        other_headers = {"Authorization": f"Bearer {other_token}"}
        denied = await client.delete(
            f"/me/tokens/{tokens[0]['id']}", headers=other_headers
        )
        assert denied.status_code == 404

        revoked = await client.delete(f"/me/tokens/{tokens[0]['id']}", headers=headers)
        assert revoked.status_code == 200
        # The revoked token no longer authenticates.
        assert (await client.get("/me", headers=headers)).status_code == 401


async def test_user_profile() -> None:
    user = await User(github_id=9, login="profiled").insert()
    engine = await Engine(name="bot", owner=user, owner_login=user.login).insert()
    await Game(
        white=engine,
        black=engine,
        white_name="bot",
        black_name="bot",
        white_version="v1",
        black_version="v1",
    ).insert()

    async with _client() as client:
        missing = await client.get("/user/nobody")
        assert missing.status_code == 404
        resp = await client.get("/user/PROFILED")  # case-insensitive
    assert resp.status_code == 200
    body = resp.json()
    assert body["login"] == "profiled"
    assert [e["name"] for e in body["engines"]] == ["bot"]
    assert len(body["games"]) == 1


async def test_engine_by_name() -> None:
    user = await User(github_id=10, login="owner").insert()
    engine = await Engine(name="my-bot", owner=user, owner_login=user.login).insert()
    await Game(
        white=engine,
        black=engine,
        white_name="my-bot",
        black_name="my-bot",
        white_version="v1",
        black_version="v1",
    ).insert()

    async with _client() as client:
        resp = await client.get("/user/OWNER/MY-BOT")  # case-insensitive
        missing = await client.get("/user/owner/no-such-engine")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "my-bot"
    assert body["owner_login"] == "owner"
    assert len(body["games"]) == 1
    assert missing.status_code == 404


async def test_register_engine_rejects_bad_names() -> None:
    user = await User(github_id=11, login="uploader").insert()
    token = await mint_token(user)
    headers = {"Authorization": f"Bearer {token}"}

    async with _client() as client:
        for bad in ["-bot", "bot-", "bo..t", "bot!", "a" * 65]:
            resp = await client.post(
                "/engine/register",
                headers=headers,
                json={
                    "name": bad,
                    "version": "v1",
                    "repository": "uploader/bot",
                    "digest": "sha256:abc",
                },
            )
            assert resp.status_code == 409, bad

        # Names are lowercased, not rejected, on case mismatch.
        ok = await client.post(
            "/engine/register",
            headers=headers,
            json={
                "name": "MyBot",
                "version": "v1",
                "repository": "uploader/mybot",
                "digest": "sha256:abc",
            },
        )
    assert ok.status_code == 200
    body = ok.json()
    assert body["name"] == "mybot"
    assert body["url"].endswith("/uploader/mybot")
