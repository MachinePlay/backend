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
    await Engine(name="stockfish", command="stockfish", description="sf").insert()
    await Engine(name="lc0", command="lc0").insert()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/engine")

    assert response.status_code == 200
    body = response.json()
    assert {e["name"] for e in body} == {"stockfish", "lc0"}
    sf = next(e for e in body if e["name"] == "stockfish")
    assert sf["command"] == "stockfish"
    assert sf["description"] == "sf"
