from uuid import uuid4

import pytest
from machineplay.schemas import GameEndEvent, GameStatus

from app import runners, streaming, tournaments
from app.exceptions import ConflictError, ForbiddenError
from app.models import (
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


# --- pairing generators (pure) -----------------------------------------------


def test_round_robin_order_covers_every_pair_both_colors() -> None:
    order = tournaments.round_robin_order(3, 2)
    # 3 unordered pairs, each played twice.
    assert len(order) == 6
    # Each unordered pair appears once with each color assignment.
    for i, j in [(0, 1), (0, 2), (1, 2)]:
        assert (i, j) in order
        assert (j, i) in order
    # No engine ever plays itself.
    assert all(w != b for w, b in order)


def test_round_robin_single_game_per_pairing() -> None:
    order = tournaments.round_robin_order(4, 1)
    assert len(order) == 6  # 4*3/2
    assert all(w != b for w, b in order)


def test_gauntlet_order_head_plays_everyone() -> None:
    order = tournaments.gauntlet_order(4, 0, 2)
    # head (index 0) vs the other 3, twice each.
    assert len(order) == 6
    assert all(0 in pair for pair in order)
    # Colors alternate: head is white in the first round, black in the second.
    assert order[0][0] == 0
    assert order[3][1] == 0


# --- helpers -----------------------------------------------------------------


async def _engine(user: User, name: str) -> Engine:
    engine = await Engine(name=name, owner=user, owner_login=user.login).insert()
    await EngineVersion(
        engine=engine,
        version="v1",
        image_repository=f"{user.login}/{name}",
        image_digest=f"sha256:{name}",
    ).insert()
    return engine


def _entries(*engines: Engine) -> list[TournamentEntry]:
    return [TournamentEntry(engine_id=e.id) for e in engines]


async def _online_runner(user: User, max_games: int) -> runners.RunnerConnection:
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


# --- creation & dispatch -----------------------------------------------------


async def test_create_round_robin_dispatches_up_to_capacity() -> None:
    user = await User(github_id=1, login="alice").insert()
    engines = [await _engine(user, n) for n in ("a", "b", "c")]
    conn = await _online_runner(user, max_games=2)
    try:
        tour = await tournaments.create_tournament(
            user,
            TournamentCreateRequest(
                name="cup",
                format=TournamentFormat.ROUND_ROBIN,
                entries=_entries(*engines),
                games_per_pairing=2,
                runner_id=conn.runner_id,
                tc="10+0.1",
            ),
        )
        # 3 pairs * 2 games each.
        total = await Game.find(Game.tournament_id == tour.id).count()
        assert total == 6
        # Runner capacity is 2, so exactly two are dispatched; the rest wait.
        playing = await Game.find(
            Game.tournament_id == tour.id, Game.status == GameStatus.PLAYING
        ).count()
        assert playing == 2
        assert conn.active_games == 2
        # Every game pinned this tournament's runner and tc.
        one = await Game.find_one(Game.tournament_id == tour.id)
        assert one is not None
        assert one.runner_id == conn.runner_id
        assert one.tc == "10+0.1"
    finally:
        _cleanup(conn)


async def test_create_rejects_offline_runner() -> None:
    user = await User(github_id=2, login="bob").insert()
    engines = [await _engine(user, n) for n in ("a", "b")]
    with pytest.raises(Exception) as exc:
        await tournaments.create_tournament(
            user,
            TournamentCreateRequest(
                name="cup",
                format=TournamentFormat.ROUND_ROBIN,
                entries=_entries(*engines),
                runner_id=uuid4(),
            ),
        )
    assert "runner" in str(exc.value)
    # A rejected create leaves nothing behind.
    assert await Tournament.find_all().count() == 0
    assert await Game.find_all().count() == 0


async def test_gauntlet_requires_head_among_participants() -> None:
    user = await User(github_id=3, login="carol").insert()
    engines = [await _engine(user, n) for n in ("a", "b")]
    conn = await _online_runner(user, max_games=1)
    try:
        with pytest.raises(ConflictError):
            await tournaments.create_tournament(
                user,
                TournamentCreateRequest(
                    name="g",
                    format=TournamentFormat.GAUNTLET,
                    entries=_entries(*engines),
                    gauntlet_head_index=5,  # out of range
                    runner_id=conn.runner_id,
                ),
            )
    finally:
        _cleanup(conn)


async def test_gauntlet_head_stored_by_version() -> None:
    user = await User(github_id=11, login="ken").insert()
    engines = [await _engine(user, n) for n in ("a", "b", "c")]
    conn = await _online_runner(user, max_games=1)
    try:
        tour = await tournaments.create_tournament(
            user,
            TournamentCreateRequest(
                name="g",
                format=TournamentFormat.GAUNTLET,
                entries=_entries(*engines),
                gauntlet_head_index=1,  # engine "b" is the head
                games_per_pairing=1,
                runner_id=conn.runner_id,
            ),
        )
        head = tour.participants[1]
        assert tour.gauntlet_head_version_id == head.version_id
        # Gauntlet: head vs the other two, one game each.
        assert await Game.find(Game.tournament_id == tour.id).count() == 2
    finally:
        _cleanup(conn)


async def test_same_engine_two_versions_are_distinct_participants() -> None:
    user = await User(github_id=12, login="lily").insert()
    e1 = await _engine(user, "a")  # version "v1"
    v1 = await EngineVersion.find_one({"engine.$id": e1.id})
    assert v1 is not None
    v2 = await EngineVersion(
        engine=e1,
        version="v2",
        image_repository="lily/a",
        image_digest="sha256:a2",
    ).insert()
    conn = await _online_runner(user, max_games=2)
    try:
        # The same engine enters twice, at v1 and v2 — a valid 2-player event.
        tour = await tournaments.create_tournament(
            user,
            TournamentCreateRequest(
                name="selfplay",
                format=TournamentFormat.ROUND_ROBIN,
                entries=[
                    TournamentEntry(engine_id=e1.id, version_id=v1.id),
                    TournamentEntry(engine_id=e1.id, version_id=v2.id),
                ],
                games_per_pairing=2,
                runner_id=conn.runner_id,
            ),
        )
        assert len(tour.participants) == 2
        # Distinct standings rows keyed by version, same engine name.
        detail = await tournaments.tournament_detail(tour.id)
        assert len(detail.standings) == 2
        assert {s.version for s in detail.standings} == {"v1", "v2"}
        assert {s.engine_name for s in detail.standings} == {"a"}
    finally:
        _cleanup(conn)


async def test_rejects_exact_duplicate_participant() -> None:
    user = await User(github_id=13, login="mona").insert()
    e1 = await _engine(user, "a")
    v1 = await EngineVersion.find_one({"engine.$id": e1.id})
    assert v1 is not None
    conn = await _online_runner(user, max_games=2)
    try:
        with pytest.raises(ConflictError):
            await tournaments.create_tournament(
                user,
                TournamentCreateRequest(
                    name="dup",
                    format=TournamentFormat.ROUND_ROBIN,
                    entries=[
                        TournamentEntry(engine_id=e1.id, version_id=v1.id),
                        TournamentEntry(engine_id=e1.id, version_id=v1.id),
                    ],
                    runner_id=conn.runner_id,
                ),
            )
    finally:
        _cleanup(conn)


async def test_create_snapshots_chosen_version() -> None:
    user = await User(github_id=10, login="jade").insert()
    e1 = await _engine(user, "a")  # created with version "v1"
    e2 = await _engine(user, "b")
    v1 = await EngineVersion.find_one({"engine.$id": e1.id})
    assert v1 is not None
    # A newer version exists, but we deliberately enter the older v1.
    await EngineVersion(
        engine=e1,
        version="v2",
        image_repository="jade/a",
        image_digest="sha256:a2",
    ).insert()
    conn = await _online_runner(user, max_games=2)
    try:
        tour = await tournaments.create_tournament(
            user,
            TournamentCreateRequest(
                name="pinned",
                format=TournamentFormat.ROUND_ROBIN,
                entries=[
                    TournamentEntry(engine_id=e1.id, version_id=v1.id),
                    TournamentEntry(engine_id=e2.id),
                ],
                games_per_pairing=1,
                runner_id=conn.runner_id,
            ),
        )
        part = next(p for p in tour.participants if p.engine_id == e1.id)
        assert part.version_id == v1.id
        assert part.version == "v1"
        # The game pins the chosen v1, not the latest v2.
        g = await Game.find_one(Game.tournament_id == tour.id)
        assert g is not None
        vid = g.white_version_id if g.white_id == e1.id else g.black_version_id
        assert vid == v1.id
    finally:
        _cleanup(conn)


# --- advancing on finish -----------------------------------------------------


async def test_finishing_a_game_dispatches_the_next_and_completes() -> None:
    user = await User(github_id=4, login="dave").insert()
    e1 = await _engine(user, "a")
    e2 = await _engine(user, "b")
    conn = await _online_runner(user, max_games=1)
    try:
        tour = await tournaments.create_tournament(
            user,
            TournamentCreateRequest(
                name="duel",
                format=TournamentFormat.ROUND_ROBIN,
                entries=_entries(e1, e2),
                games_per_pairing=2,
                runner_id=conn.runner_id,
            ),
        )
        # Capacity 1: one game playing, one pending.
        assert conn.active_games == 1
        first = await Game.find_one(
            Game.tournament_id == tour.id, Game.status == GameStatus.PLAYING
        )
        assert first is not None

        # Finishing it should free the slot and dispatch the pending game.
        await streaming.finish_game(
            first.id, GameEndEvent(result="1-0", status=GameStatus.ENDED)
        )
        assert conn.active_games == 1
        playing = await Game.find(
            Game.tournament_id == tour.id, Game.status == GameStatus.PLAYING
        ).count()
        assert playing == 1
        pending = await Game.find(
            Game.tournament_id == tour.id, Game.status == GameStatus.PENDING
        ).count()
        assert pending == 0

        # Finishing the last game completes the tournament.
        second = await Game.find_one(
            Game.tournament_id == tour.id, Game.status == GameStatus.PLAYING
        )
        assert second is not None
        await streaming.finish_game(
            second.id, GameEndEvent(result="0-1", status=GameStatus.ENDED)
        )
        refreshed = await Tournament.get(tour.id)
        assert refreshed is not None
        assert refreshed.status == TournamentStatus.COMPLETED
    finally:
        _cleanup(conn)


async def test_disconnect_pauses_and_reconnect_resumes() -> None:
    user = await User(github_id=5, login="erin").insert()
    e1 = await _engine(user, "a")
    e2 = await _engine(user, "b")
    conn = await _online_runner(user, max_games=1)
    runner_id = conn.runner_id
    conn2: runners.RunnerConnection | None = None
    try:
        tour = await tournaments.create_tournament(
            user,
            TournamentCreateRequest(
                name="flaky",
                format=TournamentFormat.ROUND_ROBIN,
                entries=_entries(e1, e2),
                games_per_pairing=1,
                runner_id=runner_id,
            ),
        )
        playing = await Game.find_one(
            Game.tournament_id == tour.id, Game.status == GameStatus.PLAYING
        )
        assert playing is not None

        # Simulate a disconnect: the runner goes offline, then its in-flight
        # game is aborted (as the WS session does on disconnect).
        runners.mark_offline(conn)
        await streaming.finish_game(
            playing.id,
            GameEndEvent(
                result="*",
                status=GameStatus.ABORTED,
                reason="runner disconnected",
            ),
        )
        # Paused: the game is re-queued PENDING, not dispatched (runner offline),
        # and nothing is counted — the tournament is still running.
        paused = await Game.get(playing.id)
        assert paused is not None
        assert paused.status == GameStatus.PENDING
        refreshed = await Tournament.get(tour.id)
        assert refreshed is not None
        assert refreshed.status == TournamentStatus.RUNNING

        # The runner reconnects: the connect hook resumes dispatch.
        conn2 = runners.mark_online(runner_id, max_games=1)
        await runners.notify_connected(runner_id)
        resumed = await Game.get(playing.id)
        assert resumed is not None
        assert resumed.status == GameStatus.PLAYING
        assert conn2.active_games == 1
    finally:
        _cleanup(conn2 if conn2 is not None else conn)


# --- standings ---------------------------------------------------------------


async def test_standings_score_and_order() -> None:
    user = await User(github_id=6, login="fran").insert()
    e1 = await _engine(user, "winner")
    e2 = await _engine(user, "loser")
    conn = await _online_runner(user, max_games=2)
    try:
        tour = await tournaments.create_tournament(
            user,
            TournamentCreateRequest(
                name="score",
                format=TournamentFormat.ROUND_ROBIN,
                entries=_entries(e1, e2),
                games_per_pairing=2,
                runner_id=conn.runner_id,
            ),
        )
        # Both games dispatched (capacity 2). game0: e1 white; game1: e2 white.
        games_docs = (
            await Game.find(Game.tournament_id == tour.id).sort("+created_at").to_list()
        )
        assert games_docs[0].white_id == e1.id
        # e1 wins both: white win in game0, black win (e1 is black) in game1.
        await streaming.finish_game(
            games_docs[0].id, GameEndEvent(result="1-0", status=GameStatus.ENDED)
        )
        await streaming.finish_game(
            games_docs[1].id, GameEndEvent(result="0-1", status=GameStatus.ENDED)
        )

        detail = await tournaments.tournament_detail(tour.id)
        assert detail.status == TournamentStatus.COMPLETED
        top, bottom = detail.standings
        assert top.engine_id == e1.id
        assert top.score == 2.0
        assert top.wins == 2 and top.played == 2
        assert bottom.engine_id == e2.id
        assert bottom.score == 0.0
        assert bottom.losses == 2
    finally:
        _cleanup(conn)


# --- cancellation ------------------------------------------------------------


async def test_cancel_aborts_live_and_pending_games() -> None:
    user = await User(github_id=7, login="gina").insert()
    e1 = await _engine(user, "a")
    e2 = await _engine(user, "b")
    conn = await _online_runner(user, max_games=1)
    try:
        tour = await tournaments.create_tournament(
            user,
            TournamentCreateRequest(
                name="stop",
                format=TournamentFormat.ROUND_ROBIN,
                entries=_entries(e1, e2),
                games_per_pairing=2,
                runner_id=conn.runner_id,
            ),
        )
        # Drain the StartGame command the live game queued.
        conn.scheduled_commands.get_nowait()

        await tournaments.cancel_tournament(user, tour.id)

        refreshed = await Tournament.get(tour.id)
        assert refreshed is not None
        assert refreshed.status == TournamentStatus.ABORTED
        # No game is left running or pending.
        alive = await Game.find(
            Game.tournament_id == tour.id,
            {"status": {"$in": [GameStatus.PLAYING, GameStatus.PENDING]}},
        ).count()
        assert alive == 0
        assert conn.active_games == 0
        # The live game's runner was told to stop it.
        from machineplay.schemas import StopGame

        stop = conn.scheduled_commands.get_nowait()
        assert isinstance(stop, StopGame)
    finally:
        _cleanup(conn)


async def test_cancel_forbidden_for_non_creator() -> None:
    owner = await User(github_id=8, login="hank").insert()
    other = await User(github_id=9, login="iris").insert()
    e1 = await _engine(owner, "a")
    e2 = await _engine(owner, "b")
    conn = await _online_runner(owner, max_games=2)
    try:
        tour = await tournaments.create_tournament(
            owner,
            TournamentCreateRequest(
                name="mine",
                format=TournamentFormat.ROUND_ROBIN,
                entries=_entries(e1, e2),
                games_per_pairing=1,
                runner_id=conn.runner_id,
            ),
        )
        with pytest.raises(ForbiddenError):
            await tournaments.cancel_tournament(other, tour.id)
    finally:
        _cleanup(conn)
