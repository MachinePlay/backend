from uuid import uuid4

from httpx import ASGITransport, AsyncClient

from app import runners
from app.auth import mint_token
from app.main import app
from app.models import Runner, User
from app.schemas import RunnerUpdateRequest


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
    runners.mark_online(doc.id, max_games=4)
    try:
        async with _client() as client:
            resp = await client.get("/runners")
        [row] = resp.json()
        assert row["online"] is True
        assert row["max_games"] == 4
    finally:
        runners.mark_offline(doc.id)


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

    created = await runners.upsert_on_connect(owner, runner_id, "franks-host", 8)
    assert created is not None
    assert created.owner_login == "frank"
    assert created.name == "franks-host"
    assert created.description == ""
    first_seen = created.last_seen_at
    assert first_seen is not None

    # Owner customises the metadata.
    await runners.edit_runner(
        owner, runner_id, RunnerUpdateRequest(name="renamed", description="desc")
    )

    # Reconnecting keeps owner-managed fields and only refreshes liveness/capacity.
    again = await runners.upsert_on_connect(owner, runner_id, "franks-host", 16)
    assert again is not None
    assert again.name == "renamed"
    assert again.description == "desc"
    assert again.max_games == 16
    assert again.last_seen_at is not None
    assert again.last_seen_at >= first_seen


async def test_upsert_rejects_owner_mismatch() -> None:
    owner = await User(github_id=7, login="grace").insert()
    intruder = await User(github_id=8, login="mallory").insert()
    runner_id = uuid4()

    assert await runners.upsert_on_connect(owner, runner_id, "host", 4) is not None
    # A different user claiming the same id is refused.
    assert await runners.upsert_on_connect(intruder, runner_id, "host", 4) is None
    doc = await Runner.get(runner_id)
    assert doc is not None and doc.owner_login == "grace"
