import json
from base64 import b64encode
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from itsdangerous import TimestampSigner
from machineplay.schemas import GameEndEvent, GameStatus, StopGame

from app import runners, streaming, tournaments
from app.accounts import DELETED_LOGIN, REASON
from app.auth import mint_token
from app.config import settings
from app.main import app
from app.models import (
    ApiToken,
    Engine,
    EngineVersion,
    Game,
    Runner,
    Tournament,
    TournamentFormat,
    TournamentStatus,
    User,
)
from app.schemas import TournamentCreateRequest, TournamentEntry


def _session_cookie(user: User) -> str:
    """Forge a logged-in session cookie the way SessionMiddleware signs it."""
    payload = b64encode(json.dumps({"user_id": str(user.id)}).encode("utf-8"))
    return TimestampSigner(settings.secret_key).sign(payload).decode("utf-8")


def _client(as_user: User | None = None) -> AsyncClient:
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    if as_user is not None:
        client.cookies.set("session", _session_cookie(as_user))
    return client


async def _delete_account(user: User | None) -> int:
    """DELETE /me as `user`; returns the status code."""
    async with _client(as_user=user) as client:
        resp = await client.delete("/me")
    return resp.status_code


async def _engine(user: User, name: str, digest: str | None = None) -> Engine:
    engine = await Engine(name=name, owner=user, owner_login=user.login).insert()
    await EngineVersion(
        engine=engine,
        version="v1",
        image_repository=f"{user.login}/{name}",
        image_digest=digest or f"sha256:{name}",
    ).insert()
    return engine


async def _online_runner(user: User, max_games: int = 2) -> runners.RunnerConnection:
    doc = await Runner(
        id=uuid4(),
        owner=user,
        owner_login=user.login,
        name="box",
        max_games=max_games,
    ).insert()
    return runners.mark_online(doc.id, max_games=max_games)


def _cleanup(conn: runners.RunnerConnection) -> None:
    for game_id in conn.game_ids():
        streaming.game_registry.unregister(game_id)
    runners.mark_offline(conn)


async def _tournament(
    creator: User, conn: runners.RunnerConnection, *entrants: Engine
) -> Tournament:
    return await tournaments.create_tournament(
        creator,
        TournamentCreateRequest(
            name="cup",
            format=TournamentFormat.ROUND_ROBIN,
            entries=[TournamentEntry(engine_id=e.id) for e in entrants],
            games_per_pairing=2,
            runner_id=conn.runner_id,
            tc="10+0.1",
        ),
    )


# --- who may delete ----------------------------------------------------------


async def test_delete_account_requires_login() -> None:
    assert await _delete_account(None) == 401


async def test_delete_account_only_deletes_the_caller(
    manifest_deletes: list[tuple[str, str]],
) -> None:
    """There is no way to name a victim: /me deletes whoever authenticated."""
    alice = await User(github_id=1, login="alice").insert()
    bob = await User(github_id=2, login="bob").insert()
    await _engine(alice, "sf")
    await _engine(bob, "bot")

    assert await _delete_account(bob) == 200

    assert await User.get(bob.id) is None
    assert await User.get(alice.id) is not None
    assert await Engine.find({"owner.$id": alice.id}).count() == 1


# --- what the account takes with it ------------------------------------------


async def test_delete_account_removes_engines_versions_tokens_and_runners(
    manifest_deletes: list[tuple[str, str]],
) -> None:
    alice = await User(github_id=1, login="alice").insert()
    await _engine(alice, "sf", digest="sha256:aaa")
    await _engine(alice, "lc0", digest="sha256:bbb")
    token = await mint_token(alice)
    runner = await Runner(
        id=uuid4(), owner=alice, owner_login="alice", name="box"
    ).insert()

    assert await _delete_account(alice) == 200

    assert await User.get(alice.id) is None
    assert await Engine.find_all().count() == 0
    assert await EngineVersion.find_all().count() == 0
    assert await ApiToken.find_all().count() == 0
    assert await Runner.get(runner.id) is None
    # Both engines' images are dropped from the registry.
    assert sorted(manifest_deletes) == [
        ("alice/lc0", "sha256:bbb"),
        ("alice/sf", "sha256:aaa"),
    ]
    # The revoked token no longer authenticates.
    async with _client() as client:
        me = await client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 401
    # The handle is free again.
    assert await User.find_one(User.login == "alice") is None


async def test_delete_account_keeps_played_history(
    manifest_deletes: list[tuple[str, str]],
) -> None:
    alice = await User(github_id=1, login="alice").insert()
    sf = await _engine(alice, "sf")
    lc0 = await _engine(alice, "lc0")
    conn = await _online_runner(alice)
    try:
        tour = await _tournament(alice, conn, sf, lc0)
        # One of the dispatched games finished before the account was deleted.
        played = await Game.find_one(
            Game.tournament_id == tour.id, Game.status == GameStatus.PLAYING
        )
        assert played is not None
        await streaming.finish_game(
            played.id,
            GameEndEvent(
                result="1-0", pgn="1. e4 1-0", status=GameStatus.ENDED, reason="normal"
            ),
        )

        assert await _delete_account(alice) == 200
    finally:
        _cleanup(conn)

    # The finished game survives with everything it needs to render.
    kept = await Game.get(played.id)
    assert kept is not None
    assert kept.status == GameStatus.ENDED
    assert (kept.result, kept.pgn) == ("1-0", "1. e4 1-0")
    assert {kept.white_name, kept.black_name} == {"sf", "lc0"}
    assert kept.white_version == "v1"

    # So does the tournament, participant snapshots included — but the handle
    # is replaced, so a future "alice" doesn't inherit the credit.
    stored = await Tournament.get(tour.id)
    assert stored is not None
    assert stored.created_by == DELETED_LOGIN
    assert {p.engine_name for p in stored.participants} == {"sf", "lc0"}
    assert {p.version for p in stored.participants} == {"v1"}


# --- active work is ended, not refused ---------------------------------------


async def test_delete_account_ends_its_running_tournament_and_games(
    manifest_deletes: list[tuple[str, str]],
) -> None:
    alice = await User(github_id=1, login="alice").insert()
    sf = await _engine(alice, "sf")
    lc0 = await _engine(alice, "lc0")
    conn = await _online_runner(alice, max_games=1)
    try:
        tour = await _tournament(alice, conn, sf, lc0)
        # One pairing is playing, the rest are still pending.
        assert conn.active_games == 1

        assert await _delete_account(alice) == 200

        stored = await Tournament.get(tour.id)
        assert stored is not None
        assert stored.status == TournamentStatus.ABORTED
        assert stored.ended_at is not None

        played = await Game.find(Game.tournament_id == tour.id).to_list()
        assert len(played) == 2
        assert all(g.status == GameStatus.ABORTED for g in played)
        assert all(g.reason == REASON for g in played)
        assert all(g.ended_at is not None for g in played)

        # The runner was told to kill the containers of the live one.
        stops = [
            conn.scheduled_commands.get_nowait()
            for _ in range(conn.scheduled_commands.qsize())
        ]
        assert any(isinstance(c, StopGame) for c in stops)
    finally:
        _cleanup(conn)


async def test_delete_account_ends_a_standalone_game_of_its_engines(
    manifest_deletes: list[tuple[str, str]],
) -> None:
    alice = await User(github_id=1, login="alice").insert()
    bob = await User(github_id=2, login="bob").insert()
    sf = await _engine(alice, "sf")
    bobs = await _engine(bob, "bot")
    game = await Game(
        white=sf,
        black=bobs,
        white_name="sf",
        black_name="bot",
        white_version="v1",
        black_version="v1",
        status=GameStatus.PENDING,
    ).insert()

    assert await _delete_account(alice) == 200

    stored = await Game.get(game.id)
    assert stored is not None
    assert stored.status == GameStatus.ABORTED
    assert stored.reason == REASON


async def test_delete_account_stops_someone_elses_tournament_on_its_runner(
    manifest_deletes: list[tuple[str, str]],
) -> None:
    # Alice owns the runner; Bob's tournament runs on it. The runner leaves with
    # Alice's account and can never reconnect, so Bob's tournament is stopped
    # rather than left running forever.
    alice = await User(github_id=1, login="alice").insert()
    bob = await User(github_id=2, login="bob").insert()
    a = await _engine(bob, "a")
    b = await _engine(bob, "b")
    conn = await _online_runner(alice, max_games=1)
    try:
        tour = await _tournament(bob, conn, a, b)

        assert await _delete_account(alice) == 200

        stored = await Tournament.get(tour.id)
        assert stored is not None
        assert stored.status == TournamentStatus.ABORTED
        # Bob's own tournament keeps his name — only Alice's are relabeled.
        assert stored.created_by == "bob"
        assert all(
            g.status == GameStatus.ABORTED
            for g in await Game.find(Game.tournament_id == tour.id).to_list()
        )
    finally:
        _cleanup(conn)

    # The runner is offline and unschedulable the moment its record is gone.
    assert runners.find_online(conn.runner_id) is None
    # Bob keeps everything of his own.
    assert await User.get(bob.id) is not None
    assert await Engine.find({"owner.$id": bob.id}).count() == 2


async def test_delete_account_leaves_other_accounts_untouched(
    manifest_deletes: list[tuple[str, str]],
) -> None:
    alice = await User(github_id=1, login="alice").insert()
    bob = await User(github_id=2, login="bob").insert()
    await _engine(alice, "sf", digest="sha256:aaa")
    await _engine(bob, "sf", digest="sha256:bbb")
    await mint_token(alice)
    bob_token = await mint_token(bob)
    bob_runner = await Runner(
        id=uuid4(), owner=bob, owner_login="bob", name="bobbox"
    ).insert()

    assert await _delete_account(alice) == 200

    assert await User.get(bob.id) is not None
    assert await Engine.find({"owner.$id": bob.id}).count() == 1
    assert await EngineVersion.find_all().count() == 1
    assert await Runner.get(bob_runner.id) is not None
    assert manifest_deletes == [("alice/sf", "sha256:aaa")]
    async with _client() as client:
        me = await client.get("/me", headers={"Authorization": f"Bearer {bob_token}"})
    assert me.status_code == 200
