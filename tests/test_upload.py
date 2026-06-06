from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.auth import mint_token
from app.config import settings
from app.main import app
from app.models import Engine, EngineVersion, User


@pytest.fixture
def storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    return tmp_path


async def _user_token() -> tuple[User, str]:
    user = User(github_id=1, login="alice")
    await user.insert()
    token = await mint_token(user)
    return user, token


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_upload_requires_token(storage: Path) -> None:
    async with _client() as client:
        resp = await client.post(
            "/engine/upload",
            data={"name": "my-engine", "version": "v1"},
            files={"image": ("engine.tar", b"tarball", "application/x-tar")},
        )
    assert resp.status_code == 401


async def test_upload_creates_engine_and_version(storage: Path) -> None:
    _, token = await _user_token()
    async with _client() as client:
        resp = await client.post(
            "/engine/upload",
            data={"name": "my-engine", "version": "2026-06-06-12-00"},
            files={"image": ("engine.tar", b"hello-tarball", "application/x-tar")},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["owner_login"] == "alice"
    assert body["name"] == "my-engine"
    assert body["version"] == "2026-06-06-12-00"

    engine = await Engine.find_one(Engine.name == "my-engine")
    assert engine is not None
    assert engine.owner_login == "alice"

    versions = await EngineVersion.find(EngineVersion.engine_id == engine.id).to_list()
    assert len(versions) == 1
    assert versions[0].size_bytes == len(b"hello-tarball")
    # tarball landed on disk
    assert (storage / versions[0].file_path).read_bytes() == b"hello-tarball"


async def test_upload_duplicate_version_conflicts(storage: Path) -> None:
    _, token = await _user_token()
    headers = {"Authorization": f"Bearer {token}"}
    async with _client() as client:
        first = await client.post(
            "/engine/upload",
            data={"name": "eng", "version": "v1"},
            files={"image": ("e.tar", b"a", "application/x-tar")},
            headers=headers,
        )
        second = await client.post(
            "/engine/upload",
            data={"name": "eng", "version": "v1"},
            files={"image": ("e.tar", b"b", "application/x-tar")},
            headers=headers,
        )
    assert first.status_code == 200
    assert second.status_code == 409


async def test_second_version_reuses_engine(storage: Path) -> None:
    _, token = await _user_token()
    headers = {"Authorization": f"Bearer {token}"}
    async with _client() as client:
        await client.post(
            "/engine/upload",
            data={"name": "eng", "version": "v1"},
            files={"image": ("e.tar", b"a", "application/x-tar")},
            headers=headers,
        )
        await client.post(
            "/engine/upload",
            data={"name": "eng", "version": "v2"},
            files={"image": ("e.tar", b"bb", "application/x-tar")},
            headers=headers,
        )

    engines = await Engine.find(Engine.name == "eng").to_list()
    assert len(engines) == 1
    versions = await EngineVersion.find(
        EngineVersion.engine_id == engines[0].id
    ).to_list()
    assert {v.version for v in versions} == {"v1", "v2"}


async def test_get_engine_detail(storage: Path) -> None:
    _, token = await _user_token()
    headers = {"Authorization": f"Bearer {token}"}
    async with _client() as client:
        up = await client.post(
            "/engine/upload",
            data={"name": "eng", "version": "v1"},
            files={"image": ("e.tar", b"a", "application/x-tar")},
            headers=headers,
        )
        engine_id = up.json()["engine_id"]
        detail = await client.get(f"/engine/{engine_id}")
        listing = await client.get("/engine")

    assert detail.status_code == 200
    body = detail.json()
    assert body["owner_login"] == "alice"
    assert [v["version"] for v in body["versions"]] == ["v1"]

    found = next(e for e in listing.json() if e["id"] == engine_id)
    assert found["version_count"] == 1
    assert found["owner_login"] == "alice"


async def test_get_engine_detail_missing(storage: Path) -> None:
    async with _client() as client:
        resp = await client.get("/engine/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


async def test_create_token_requires_session() -> None:
    async with _client() as client:
        resp = await client.post("/me/tokens")
    assert resp.status_code == 401
