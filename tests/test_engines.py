from uuid import uuid4

from httpx import ASGITransport, AsyncClient

from app.main import app
from app.models import Engine


async def test_list_engines_empty() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/engine")
    assert response.status_code == 200
    assert response.json() == []


async def test_list_engines_returns_inserted() -> None:
    owner = uuid4()
    await Engine(
        name="stockfish", description="sf", owner_id=owner, owner_login="alice"
    ).insert()
    await Engine(name="lc0", owner_id=owner, owner_login="alice").insert()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/engine")

    assert response.status_code == 200
    body = response.json()
    assert {e["name"] for e in body} == {"stockfish", "lc0"}
    sf = next(e for e in body if e["name"] == "stockfish")
    assert sf["description"] == "sf"
