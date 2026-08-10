import json
from base64 import b64encode
from uuid import UUID, uuid4

from httpx import ASGITransport, AsyncClient

from machineplay.schemas import GameStatus

from app.auth import mint_token
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


def _game(
    white: Engine,
    black: Engine,
    status: GameStatus,
    white_version_id: UUID | None = None,
    black_version_id: UUID | None = None,
) -> Game:
    return Game(
        white=white,
        black=black,
        white_name=white.name,
        black_name=black.name,
        white_version="v1",
        black_version="v1",
        white_version_id=white_version_id,
        black_version_id=black_version_id,
        status=status,
    )


async def _only_version(engine: Engine) -> EngineVersion:
    version = await EngineVersion.find_one({"engine.$id": engine.id})
    assert version is not None
    return version


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


async def test_edit_engine_owner() -> None:
    alice = await User(github_id=1, login="alice").insert()
    engine = await _engine_with_version(alice, "sf")

    async with _client(as_user=alice) as client:
        resp = await client.patch(
            "/user/alice/sf",
            json={
                "name": "Stockfish",
                "description": "  the strong one  ",
                "tags": ["C++", "nnue", "c++", " ", "alpha-beta"],
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "stockfish"
    assert body["description"] == "the strong one"
    # Lowercased, de-duplicated, blanks dropped, order preserved.
    assert body["tags"] == ["c++", "nnue", "alpha-beta"]
    # The engine moved to its new URL; its versions came along.
    reloaded = await Engine.get(engine.id)
    assert reloaded is not None and reloaded.name == "stockfish"
    assert len(body["versions"]) == 1


async def test_edit_engine_leaves_omitted_fields_alone() -> None:
    alice = await User(github_id=1, login="alice").insert()
    await Engine(
        name="sf",
        description="keep me",
        tags=["python"],
        owner=alice,
        owner_login="alice",
    ).insert()

    async with _client(as_user=alice) as client:
        resp = await client.patch("/user/alice/sf", json={"name": "sf2"})

    assert resp.status_code == 200
    assert resp.json()["description"] == "keep me"
    assert resp.json()["tags"] == ["python"]


async def test_edit_engine_rejects_bad_name_tags_and_clashes() -> None:
    alice = await User(github_id=1, login="alice").insert()
    await _engine_with_version(alice, "sf")
    await _engine_with_version(alice, "lc0")

    async with _client(as_user=alice) as client:
        # Not a slug, an existing name in the same namespace, a bad tag, and
        # more tags than the cap.
        assert (
            await client.patch("/user/alice/sf", json={"name": "Not A Slug"})
        ).status_code == 409
        assert (
            await client.patch("/user/alice/sf", json={"name": "lc0"})
        ).status_code == 409
        assert (
            await client.patch("/user/alice/sf", json={"tags": ["not a tag"]})
        ).status_code == 409
        assert (
            await client.patch(
                "/user/alice/sf", json={"tags": [f"t{i}" for i in range(11)]}
            )
        ).status_code == 409

    assert (await Engine.find_one(Engine.name == "sf")) is not None


async def test_edit_engine_requires_owner_or_admin() -> None:
    alice = await User(github_id=1, login="alice").insert()
    bob = await User(github_id=2, login="bob").insert()
    admin = await User(github_id=3, login="root", is_admin=True).insert()
    await _engine_with_version(alice, "sf")

    async with _client() as client:
        assert (
            await client.patch("/user/alice/sf", json={"description": "x"})
        ).status_code == 401
    async with _client(as_user=bob) as client:
        assert (
            await client.patch("/user/alice/sf", json={"description": "x"})
        ).status_code == 403
    async with _client(as_user=admin) as client:
        assert (
            await client.patch("/user/alice/sf", json={"description": "x"})
        ).status_code == 200


async def test_edit_engine_rename_frees_the_old_name() -> None:
    """A rename doesn't squat its old name: re-uploading under it creates a
    fresh engine rather than colliding on the (owner, name) unique index."""
    alice = await User(github_id=1, login="alice").insert()
    original = await _engine_with_version(alice, "sf")

    async with _client(as_user=alice) as client:
        assert (
            await client.patch("/user/alice/sf", json={"name": "sf-old"})
        ).status_code == 200

    revived = await Engine(name="sf", owner=alice, owner_login="alice").insert()
    assert revived.id != original.id
    assert await Engine.find({"owner.$id": alice.id}).count() == 2


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


# --- versions -----------------------------------------------------------------


async def test_rename_version_owner() -> None:
    """A rename moves the label only: the image stays pinned where it was
    pushed, and a game that already played keeps the string it recorded."""
    alice = await User(github_id=1, login="alice").insert()
    engine = await _engine_with_version(alice, "sf")
    other = await _engine_with_version(alice, "lc0")
    version = await _only_version(engine)
    played = await _game(
        engine, other, GameStatus.ENDED, white_version_id=version.id
    ).insert()

    async with _client(as_user=alice) as client:
        resp = await client.patch(
            f"/user/alice/sf/version/{version.id}", json={"version": "  v1.1  "}
        )

    assert resp.status_code == 200
    assert [v["version"] for v in resp.json()["versions"]] == ["v1.1"]
    reloaded = await EngineVersion.get(version.id)
    assert reloaded is not None
    assert reloaded.version == "v1.1"
    assert (reloaded.image_repository, reloaded.image_digest) == (
        "alice/sf",
        "sha256:aaa",
    )
    history = await Game.get(played.id)
    assert history is not None and history.white_version == "v1"


async def test_rename_version_rejects_bad_labels_and_clashes() -> None:
    alice = await User(github_id=1, login="alice").insert()
    engine = await _engine_with_version(alice, "sf")
    v1 = await _only_version(engine)
    await EngineVersion(
        engine=engine,
        version="v2",
        image_repository="alice/sf",
        image_digest="sha256:bbb",
    ).insert()
    foreign = await _only_version(await _engine_with_version(alice, "lc0"))

    async with _client(as_user=alice) as client:

        async def rename(version_id: UUID, label: str) -> int:
            resp = await client.patch(
                f"/user/alice/sf/version/{version_id}", json={"version": label}
            )
            return resp.status_code

        # Not tag-shaped (leading separator, a space), and a label this engine's
        # other version already uses.
        assert await rename(v1.id, "-nope") == 409
        assert await rename(v1.id, "v 1") == 409
        assert await rename(v1.id, "v2") == 409
        assert await rename(v1.id, "x" * 65) == 422
        # A version of a different engine, and one that exists nowhere: the URL
        # names a pair that doesn't go together either way.
        assert await rename(foreign.id, "v9") == 404
        assert await rename(uuid4(), "v9") == 404

    unchanged = await EngineVersion.get(v1.id)
    assert unchanged is not None and unchanged.version == "v1"


async def test_rename_version_requires_owner_or_admin() -> None:
    alice = await User(github_id=1, login="alice").insert()
    bob = await User(github_id=2, login="bob").insert()
    admin = await User(github_id=3, login="root", is_admin=True).insert()
    version = await _only_version(await _engine_with_version(alice, "sf"))
    path = f"/user/alice/sf/version/{version.id}"

    async with _client() as client:
        assert (await client.patch(path, json={"version": "v2"})).status_code == 401
    async with _client(as_user=bob) as client:
        assert (await client.patch(path, json={"version": "v2"})).status_code == 403
    async with _client(as_user=admin) as client:
        assert (await client.patch(path, json={"version": "v2"})).status_code == 200


async def test_delete_version_owner(manifest_deletes: list[tuple[str, str]]) -> None:
    """One version goes; the engine, its other versions and finished games stay."""
    alice = await User(github_id=1, login="alice").insert()
    engine = await _engine_with_version(alice, "sf", digest="sha256:aaa")
    other = await _engine_with_version(alice, "lc0")
    old = await _only_version(engine)
    new = await EngineVersion(
        engine=engine,
        version="v2",
        image_repository="alice/sf",
        image_digest="sha256:bbb",
    ).insert()
    played = await _game(
        engine, other, GameStatus.ENDED, white_version_id=old.id
    ).insert()

    async with _client(as_user=alice) as client:
        resp = await client.delete(f"/user/alice/sf/version/{old.id}")

    assert resp.status_code == 200
    assert await EngineVersion.get(old.id) is None
    assert await EngineVersion.get(new.id) is not None
    assert await Engine.get(engine.id) is not None
    # The finished game renders from its denormalized names/version strings.
    history = await Game.get(played.id)
    assert history is not None and history.white_version == "v1"
    assert manifest_deletes == [("alice/sf", "sha256:aaa")]


async def test_delete_version_requires_owner_or_admin(
    manifest_deletes: list[tuple[str, str]],
) -> None:
    alice = await User(github_id=1, login="alice").insert()
    bob = await User(github_id=2, login="bob").insert()
    admin = await User(github_id=3, login="root", is_admin=True).insert()
    version = await _only_version(await _engine_with_version(alice, "sf"))
    path = f"/user/alice/sf/version/{version.id}"

    async with _client() as client:
        assert (await client.delete(path)).status_code == 401
    async with _client(as_user=bob) as client:
        assert (await client.delete(path)).status_code == 403
    assert manifest_deletes == []
    async with _client(as_user=admin) as client:
        assert (await client.delete(path)).status_code == 200
    assert await EngineVersion.get(version.id) is None


async def test_delete_version_blocked_while_its_own_games_are_active(
    manifest_deletes: list[tuple[str, str]],
) -> None:
    """Only games of *that* version block it — a tournament's un-played pairings
    are PENDING games, so an entered version can't vanish mid-tournament."""
    alice = await User(github_id=1, login="alice").insert()
    engine = await _engine_with_version(alice, "sf")
    other = await _engine_with_version(alice, "lc0")
    entered = await _only_version(engine)
    idle = await EngineVersion(
        engine=engine,
        version="v2",
        image_repository="alice/sf",
        image_digest="sha256:bbb",
    ).insert()
    await _game(other, engine, GameStatus.PENDING, black_version_id=entered.id).insert()

    async with _client(as_user=alice) as client:
        blocked = await client.delete(f"/user/alice/sf/version/{entered.id}")
        allowed = await client.delete(f"/user/alice/sf/version/{idle.id}")

    assert blocked.status_code == 409
    assert await EngineVersion.get(entered.id) is not None
    assert allowed.status_code == 200
    assert manifest_deletes == [("alice/sf", "sha256:bbb")]


async def test_delete_version_keeps_manifest_another_version_still_references(
    manifest_deletes: list[tuple[str, str]],
) -> None:
    alice = await User(github_id=1, login="alice").insert()
    engine = await _engine_with_version(alice, "sf", digest="sha256:shared")
    duplicate = await EngineVersion(
        engine=engine,
        version="v2",
        image_repository="alice/sf",
        image_digest="sha256:shared",
    ).insert()

    async with _client(as_user=alice) as client:
        resp = await client.delete(f"/user/alice/sf/version/{duplicate.id}")

    assert resp.status_code == 200
    assert manifest_deletes == []


async def test_delete_last_version_leaves_an_empty_engine(
    manifest_deletes: list[tuple[str, str]],
) -> None:
    """Deleting everything playable keeps the engine (and its description, tags
    and URL) around, listed with no versions."""
    alice = await User(github_id=1, login="alice").insert()
    engine = await _engine_with_version(alice, "sf")
    version = await _only_version(engine)

    async with _client(as_user=alice) as client:
        assert (
            await client.delete(f"/user/alice/sf/version/{version.id}")
        ).status_code == 200
        listed = await client.get("/engine")
        detail = await client.get("/user/alice/sf")

    assert await Engine.get(engine.id) is not None
    assert [e["version_count"] for e in listed.json()] == [0]
    assert detail.json()["versions"] == []


async def test_register_version_rejects_a_label_that_is_not_tag_shaped() -> None:
    """The version doubles as the pushed image's docker tag; the CLI checks the
    same shape before pushing, so this is the backstop."""
    alice = await User(github_id=1, login="alice").insert()
    headers = {"Authorization": f"Bearer {await mint_token(alice)}"}
    body = {
        "name": "sf",
        "version": "release 1",
        "repository": "alice/sf",
        "digest": "sha256:aaa",
    }

    async with _client() as client:
        rejected = await client.post("/engine/register", json=body, headers=headers)
        accepted = await client.post(
            "/engine/register", json={**body, "version": "release-1"}, headers=headers
        )

    assert rejected.status_code == 409
    assert accepted.status_code == 200
