from uuid import uuid4

import chess
import pytest
from machineplay.schemas import GameEndEvent, GameStatus

from app import books, engines, runners, streaming, tournaments
from app.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.models import (
    Book,
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


def test_round_robin_order_covers_every_pair_both_colors() -> None:
    order = tournaments.round_robin_order(3, 2)
    # 3 unordered pairs, each played twice.
    assert len(order) == 6
    # Each unordered pair appears once with each color assignment.
    colors = [(p.white, p.black) for p in order]
    for i, j in [(0, 1), (0, 2), (1, 2)]:
        assert (i, j) in colors
        assert (j, i) in colors
    # No engine ever plays itself.
    assert all(p.white != p.black for p in order)


def test_round_robin_single_game_per_pairing() -> None:
    order = tournaments.round_robin_order(4, 1)
    assert len(order) == 6  # 4*3/2
    assert all(p.white != p.black for p in order)


def test_gauntlet_order_head_plays_everyone() -> None:
    order = tournaments.gauntlet_order(4, 0, 2)
    # head (index 0) vs the other 3, twice each.
    assert len(order) == 6
    assert all(0 in (p.white, p.black) for p in order)
    # Colors alternate: head is white in the first round, black in the second.
    assert order[0].white == 0
    assert order[3].black == 0


def test_colour_reversed_twins_share_an_opening() -> None:
    """The point of the book: a pairing's two games start from the same line,
    one from each side, so a lopsided opening cancels instead of counting."""
    order = tournaments.round_robin_order(3, 4)
    by_pairing: dict[frozenset[int], list[tournaments.Pairing]] = {}
    for pairing in order:
        by_pairing.setdefault(frozenset((pairing.white, pairing.black)), []).append(
            pairing
        )
    for played in by_pairing.values():
        assert [p.opening for p in played] == [0, 0, 1, 1]
        # ... and the two games sharing an opening swap colours.
        assert played[0].white == played[1].black
        assert played[2].white == played[3].black


def test_gauntlet_openings_pair_by_round() -> None:
    order = tournaments.gauntlet_order(3, 0, 4)
    per_opponent = [p for p in order if 1 in (p.white, p.black)]
    assert [p.opening for p in per_opponent] == [0, 0, 1, 1]


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
            await Game.find(Game.tournament_id == tour.id)
            .sort("+tournament_seq")
            .to_list()
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


async def test_deleting_an_entered_version_keeps_tournament_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Standings and participants render from the snapshot taken at creation,
    so deleting a version a finished tournament played doesn't erase it."""

    async def fake_manifest_delete(repository: str, digest: str) -> None:
        return None

    monkeypatch.setattr("app.registry.delete_manifest", fake_manifest_delete)
    user = await User(github_id=9, login="alice").insert()
    a, b = [await _engine(user, n) for n in ("a", "b")]
    conn = await _online_runner(user, max_games=2)
    try:
        tour = await tournaments.create_tournament(
            user,
            TournamentCreateRequest(
                name="cup",
                format=TournamentFormat.ROUND_ROBIN,
                entries=_entries(a, b),
                games_per_pairing=2,
                runner_id=conn.runner_id,
            ),
        )
        for doc in await Game.find(Game.tournament_id == tour.id).to_list():
            doc.status = GameStatus.ENDED
            doc.result = "1-0"
            await doc.save()

        played = await EngineVersion.find_one({"engine.$id": a.id})
        # Re-read the engine the way a request does: an owner Link, not the
        # in-memory User the helper inserted it with.
        owned = await Engine.get(a.id)
        assert played is not None and owned is not None
        await engines.delete_version(user, owned, played.id)

        detail = await tournaments.tournament_detail(tour.id)
    finally:
        _cleanup(conn)

    assert [(p.engine_name, p.version) for p in detail.participants] == [
        ("a", "v1"),
        ("b", "v1"),
    ]
    assert {row.engine_name for row in detail.standings} == {"a", "b"}
    assert sum(row.played for row in detail.standings) == 4


def test_estimate_scales_with_base_and_increment() -> None:
    # Both sides' budget over ETA_MOVES_PER_SIDE moves, plus startup.
    assert tournaments.estimate_game_seconds("30+0.3") == 2 * (30 + 40 * 0.3) + 10
    # An increment-less control is still accepted.
    assert tournaments.estimate_game_seconds("60") == 2 * 60 + 10
    # More slots finish the same work proportionally sooner.
    one = tournaments.estimate_remaining_seconds("10+0.1", 100, 1)
    four = tournaments.estimate_remaining_seconds("10+0.1", 100, 4)
    assert one is not None and four is not None
    assert one == pytest.approx(four * 4)
    # Nothing left to play has no ETA.
    assert tournaments.estimate_remaining_seconds("10+0.1", 0, 4) is None


async def test_rejects_more_games_than_the_tournament_cap() -> None:
    user = await User(github_id=20, login="ivy").insert()
    engines_ = [await _engine(user, n) for n in ("a", "b", "c", "d")]
    conn = await _online_runner(user, max_games=1)
    try:
        # 6 pairings * 400 games = 2400, over the 2000 cap — even though no
        # single pairing plays an outrageous number.
        with pytest.raises(ConflictError) as exc:
            await tournaments.create_tournament(
                user,
                TournamentCreateRequest(
                    name="too big",
                    format=TournamentFormat.ROUND_ROBIN,
                    entries=_entries(*engines_),
                    games_per_pairing=400,
                    runner_id=conn.runner_id,
                ),
            )
        assert "2000" in str(exc.value)
        # Nothing was inserted.
        assert await Tournament.find(Tournament.creator_id == user.id).count() == 0
    finally:
        _cleanup(conn)


async def test_allows_a_long_head_to_head_match() -> None:
    """The per-pairing count is only capped by the tournament total: a two-engine
    match may play far more games than a round robin's pairings could."""
    user = await User(github_id=21, login="jo").insert()
    e1 = await _engine(user, "a")
    e2 = await _engine(user, "b")
    conn = await _online_runner(user, max_games=1)
    try:
        tour = await tournaments.create_tournament(
            user,
            TournamentCreateRequest(
                name="match",
                format=TournamentFormat.ROUND_ROBIN,
                entries=_entries(e1, e2),
                games_per_pairing=500,
                runner_id=conn.runner_id,
                tc="10+0.1",
            ),
        )
        assert await Game.find(Game.tournament_id == tour.id).count() == 500
        # The batch shares a created_at, so seq is what orders them: it must be
        # dense and gap-free or dispatch would skip pairings.
        seqs = [
            g.tournament_seq
            for g in await Game.find(Game.tournament_id == tour.id)
            .sort("+tournament_seq")
            .to_list()
        ]
        assert seqs == list(range(500))

        detail = await tournaments.tournament_detail(tour.id)
        assert detail.progress.total == 500
        assert detail.progress.playing == 1
        assert detail.progress.pending == 499
        # 500 games, one slot: the ETA is the whole match end to end.
        assert detail.eta_seconds == pytest.approx(
            500 * tournaments.estimate_game_seconds("10+0.1")
        )
    finally:
        _cleanup(conn)


async def test_detail_carries_live_boards_not_the_whole_schedule() -> None:
    user = await User(github_id=22, login="kim").insert()
    e1 = await _engine(user, "a")
    e2 = await _engine(user, "b")
    conn = await _online_runner(user, max_games=2)
    try:
        tour = await tournaments.create_tournament(
            user,
            TournamentCreateRequest(
                name="live",
                format=TournamentFormat.ROUND_ROBIN,
                entries=_entries(e1, e2),
                games_per_pairing=10,
                runner_id=conn.runner_id,
            ),
        )
        detail = await tournaments.tournament_detail(tour.id)
        # Only the games actually playing ride along, however big the schedule.
        assert len(detail.live_games) == 2
        assert detail.progress.total == 10
        assert {g.status for g in detail.live_games} == {GameStatus.PLAYING}
    finally:
        _cleanup(conn)


async def test_games_page_is_ordered_filtered_and_bounded() -> None:
    user = await User(github_id=23, login="lee").insert()
    e1 = await _engine(user, "a")
    e2 = await _engine(user, "b")
    conn = await _online_runner(user, max_games=1)
    try:
        tour = await tournaments.create_tournament(
            user,
            TournamentCreateRequest(
                name="paged",
                format=TournamentFormat.ROUND_ROBIN,
                entries=_entries(e1, e2),
                games_per_pairing=6,
                runner_id=conn.runner_id,
            ),
        )
        first = await tournaments.tournament_games(tour.id, None, limit=4, offset=0)
        assert first.total == 6
        assert len(first.games) == 4
        second = await tournaments.tournament_games(tour.id, None, limit=4, offset=4)
        assert len(second.games) == 2
        # Paging walks the schedule in order, without repeats.
        ids = [g.id for g in first.games] + [g.id for g in second.games]
        assert len(set(ids)) == 6
        ordered = (
            await Game.find(Game.tournament_id == tour.id)
            .sort("+tournament_seq")
            .to_list()
        )
        assert ids == [g.id for g in ordered]

        # Filtering by status counts only the matching games.
        playing = await tournaments.tournament_games(
            tour.id, GameStatus.PLAYING, limit=50, offset=0
        )
        assert playing.total == 1
        assert playing.games[0].status == GameStatus.PLAYING
    finally:
        _cleanup(conn)


async def test_tournament_deals_paired_openings_by_default() -> None:
    """Every game gets a book line, and a pairing's two colour-reversed games
    start from the same one."""
    user = await User(github_id=40, login="booker").insert()
    engines = [await _engine(user, n) for n in ("a", "b")]
    conn = await _online_runner(user, max_games=1)
    try:
        tour = await tournaments.create_tournament(
            user,
            TournamentCreateRequest(
                name="booked",
                format=TournamentFormat.ROUND_ROBIN,
                entries=_entries(*engines),
                games_per_pairing=4,
                runner_id=conn.runner_id,
                tc="10+0.1",
            ),
        )
        assert tour.book == books.DEFAULT_SLUG
        docs = (
            await Game.find(Game.tournament_id == tour.id)
            .sort("+tournament_seq")
            .to_list()
        )
        assert len(docs) == 4
        assert all(len(d.opening_moves) == books.PLIES for d in docs)
        assert all(d.book == books.DEFAULT_SLUG for d in docs)
        # Games 0/1 share a line and swap colours; so do 2/3, on a different one.
        assert docs[0].opening_moves == docs[1].opening_moves
        assert docs[2].opening_moves == docs[3].opening_moves
        assert docs[0].opening_moves != docs[2].opening_moves
        assert docs[0].white_name == docs[1].black_name
    finally:
        _cleanup(conn)


async def test_tournament_without_a_book_starts_from_the_initial_position() -> None:
    user = await User(github_id=41, login="nobook").insert()
    engines = [await _engine(user, n) for n in ("a", "b")]
    conn = await _online_runner(user, max_games=1)
    try:
        tour = await tournaments.create_tournament(
            user,
            TournamentCreateRequest(
                name="bare",
                format=TournamentFormat.ROUND_ROBIN,
                entries=_entries(*engines),
                games_per_pairing=2,
                runner_id=conn.runner_id,
                book=books.NO_BOOK,
            ),
        )
        assert tour.book is None
        docs = await Game.find(Game.tournament_id == tour.id).to_list()
        assert all(d.opening_moves == [] and d.book is None for d in docs)
    finally:
        _cleanup(conn)


async def test_create_rejects_unknown_book() -> None:
    user = await User(github_id=42, login="typo").insert()
    engines = [await _engine(user, n) for n in ("a", "b")]
    conn = await _online_runner(user, max_games=1)
    try:
        with pytest.raises(NotFoundError):
            await tournaments.create_tournament(
                user,
                TournamentCreateRequest(
                    name="oops",
                    format=TournamentFormat.ROUND_ROBIN,
                    entries=_entries(*engines),
                    runner_id=conn.runner_id,
                    book="perfekt-2021",
                ),
            )
    finally:
        _cleanup(conn)


async def test_openings_are_legal_and_reach_the_book_depth() -> None:
    """A line the runner can't play is a game that dies on arrival, so the
    book's own contents are checked rather than trusted."""
    book = await books.get(books.DEFAULT_SLUG)
    assert len(book.lines) > 100
    for line in book.lines:
        board = chess.Board()
        for uci in line:
            move = chess.Move.from_uci(uci)
            assert move in board.legal_moves, f"{line} illegal at {uci}"
            board.push(move)
        assert books.MIN_PLIES <= len(line) <= books.PLIES
        assert not board.is_game_over()


async def test_book_order_is_seed_stable_and_seed_dependent() -> None:
    book = await books.get(books.DEFAULT_SLUG)
    assert books.order(book, 7) == books.order(book, 7)
    assert books.order(book, 7) != books.order(book, 8)
    # A reordering, not a filter.
    assert sorted(books.order(book, 7)) == sorted(book.lines)


async def test_seed_is_idempotent() -> None:
    before = await Book.find_all().to_list()
    await books.seed()
    after = await Book.find_all().to_list()
    assert len(before) == len(after) == 1
    assert before[0].id == after[0].id
