from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from machineplay.schemas import GameEndEvent, GameStatus

from app import engines, glicko2, ratings, runners, streaming, tournaments
from app.exceptions import ForbiddenError
from app.models import (
    Engine,
    EngineVersion,
    Game,
    Rating,
    RatingCategory,
    Runner,
    TournamentFormat,
    User,
)
from app.schemas import TournamentCreateRequest, TournamentEntry

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_glickman_example() -> None:
    """Glickman's worked example from the Glicko-2 paper, as upstream tests it."""
    env = glicko2.Glicko2(tau=0.5)
    r1 = env.create_rating(1500, 200, 0.06)
    r2 = env.create_rating(1400, 30)
    r3 = env.create_rating(1550, 100)
    r4 = env.create_rating(1700, 300)
    rated = env.rate(r1, [(glicko2.WIN, r2), (glicko2.LOSS, r3), (glicko2.LOSS, r4)])
    assert rated.mu == pytest.approx(1464.051, abs=0.01)
    assert rated.phi == pytest.approx(151.515, abs=0.01)
    assert rated.sigma == pytest.approx(0.05999, abs=1e-5)


@pytest.mark.parametrize(
    ("tc", "expected"),
    [
        ("10+0.1", RatingCategory.BULLET),
        ("30+0.3", RatingCategory.BULLET),
        ("60+1", RatingCategory.BLITZ),
        ("180+2", RatingCategory.BLITZ),
        ("300+3", RatingCategory.RAPID),
        ("59", RatingCategory.BULLET),
        ("60", RatingCategory.BLITZ),
        ("300", RatingCategory.RAPID),
    ],
)
def test_category_by_estimated_duration(tc: str, expected: RatingCategory) -> None:
    assert ratings.category(tc) == expected


async def _version(owner: User, name: str, version: str = "v1") -> EngineVersion:
    engine = await Engine.find_one({"owner.$id": owner.id}, Engine.name == name)
    if engine is None:
        engine = await Engine(name=name, owner=owner, owner_login=owner.login).insert()
    doc = await EngineVersion(
        engine=engine,
        version=version,
        image_repository=f"{owner.login}/{name}",
        image_digest=f"sha256:{name}{version}",
    ).insert()
    # Reloaded so `engine` is a Link, as everywhere outside this helper.
    fresh = await EngineVersion.get(doc.id)
    assert fresh is not None
    return fresh


async def _game(
    white: EngineVersion,
    black: EngineVersion,
    result: str,
    minute: int,
    *,
    tc: str = "30+0.3",
    rated: bool = True,
    status: GameStatus = GameStatus.ENDED,
) -> Game:
    w_engine = await Engine.get(white.engine.ref.id)
    b_engine = await Engine.get(black.engine.ref.id)
    assert w_engine is not None and b_engine is not None
    return await Game(
        white=w_engine,
        black=b_engine,
        white_name=w_engine.name,
        black_name=b_engine.name,
        white_version=white.version,
        black_version=black.version,
        white_version_id=white.id,
        black_version_id=black.id,
        tc=tc,
        tournament_id=uuid4() if rated else None,
        rated=rated,
        status=status,
        result=result,
        ended_at=T0 + timedelta(minutes=minute),
    ).insert()


async def _ratings(version: EngineVersion) -> dict[RatingCategory, Rating]:
    fresh = await EngineVersion.get(version.id)
    assert fresh is not None
    return fresh.ratings


@pytest.fixture
async def alice() -> User:
    return await User(github_id=1, login="alice").insert()


async def test_win_moves_equal_players_symmetrically(alice: User) -> None:
    a = await _version(alice, "a")
    b = await _version(alice, "b")
    game = await _game(a, b, "1-0", 1)

    assert await ratings.rate_unrated() == 1

    ra = (await _ratings(a))[RatingCategory.BULLET]
    rb = (await _ratings(b))[RatingCategory.BULLET]
    assert ra.mu > 1500 > rb.mu
    assert ra.mu - 1500 == pytest.approx(1500 - rb.mu)
    assert ra.phi < glicko2.PHI and rb.phi < glicko2.PHI
    assert ra.games == rb.games == 1

    stored = await Game.get(game.id)
    assert stored is not None and stored.rated_at is not None
    assert stored.white_rating is not None and stored.black_rating is not None
    assert stored.white_rating.before == 1500
    assert stored.white_rating.after == pytest.approx(ra.mu)
    assert stored.black_rating.after == pytest.approx(rb.mu)


async def test_draw_between_equals_keeps_rating_and_shrinks_rd(alice: User) -> None:
    a = await _version(alice, "a")
    b = await _version(alice, "b")
    await _game(a, b, "1/2-1/2", 1)

    await ratings.rate_unrated()

    for v in (a, b):
        r = (await _ratings(v))[RatingCategory.BULLET]
        assert r.mu == pytest.approx(1500)
        assert r.phi < glicko2.PHI


async def test_games_apply_in_end_order_not_insert_order(alice: User) -> None:
    a = await _version(alice, "a")
    b = await _version(alice, "b")
    # Inserted loss-first, but the win ended first.
    await _game(a, b, "0-1", 2)
    await _game(a, b, "1-0", 1)

    await ratings.rate_unrated()

    fresh = Rating.initial()
    a1 = ratings.rate(fresh, 1.0, fresh)
    b1 = ratings.rate(fresh, 0.0, fresh)
    a2 = ratings.rate(a1, 0.0, b1)
    got = (await _ratings(a))[RatingCategory.BULLET]
    assert got.mu == pytest.approx(a2.mu)
    assert got.games == 2


async def test_repeated_rating_counts_each_game_once(alice: User) -> None:
    a = await _version(alice, "a")
    b = await _version(alice, "b")
    await _game(a, b, "1-0", 1)

    assert await ratings.rate_unrated() == 1
    once = await _ratings(a)
    assert await ratings.rate_unrated() == 0
    assert await _ratings(a) == once


async def test_two_versions_of_one_engine_rate_separately(alice: User) -> None:
    v1 = await _version(alice, "dinora", "v1")
    v2 = await _version(alice, "dinora", "v2")
    await _game(v2, v1, "1-0", 1)
    await _game(v1, v2, "0-1", 2)

    await ratings.rate_unrated()

    r1 = (await _ratings(v1))[RatingCategory.BULLET]
    r2 = (await _ratings(v2))[RatingCategory.BULLET]
    assert r2.mu > 1500 > r1.mu
    assert r1.games == r2.games == 2

    # The engine list shows the latest version's ratings.
    [out] = await engines.list_engines()
    assert out.ratings[RatingCategory.BULLET].mu == pytest.approx(r2.mu)


async def test_categories_are_rated_independently(alice: User) -> None:
    a = await _version(alice, "a")
    b = await _version(alice, "b")
    await _game(a, b, "1-0", 1, tc="10+0.1")
    await _game(a, b, "0-1", 2, tc="300+3")

    await ratings.rate_unrated()

    ra = await _ratings(a)
    assert set(ra) == {RatingCategory.BULLET, RatingCategory.RAPID}
    assert ra[RatingCategory.BULLET].mu > 1500 > ra[RatingCategory.RAPID].mu
    assert ra[RatingCategory.BULLET].games == ra[RatingCategory.RAPID].games == 1


async def test_ineligible_games_are_ignored(alice: User) -> None:
    a = await _version(alice, "a")
    b = await _version(alice, "b")
    await _game(a, b, "1-0", 1, rated=False)
    await _game(a, b, "*", 2, status=GameStatus.ABORTED)
    await _game(a, b, "*", 3)
    await _game(a, b, "1-0", 4, status=GameStatus.PLAYING)

    assert await ratings.rate_unrated() == 0
    assert await _ratings(a) == {}


async def test_deleted_opponent_skips_the_game(alice: User) -> None:
    a = await _version(alice, "a")
    b = await _version(alice, "b")
    game = await _game(a, b, "1-0", 1)
    await b.delete()

    assert await ratings.rate_unrated() == 1
    assert await _ratings(a) == {}
    stored = await Game.get(game.id)
    assert stored is not None
    assert stored.rated_at is not None and stored.white_rating is None


async def test_rebuild_reproduces_live_ratings(alice: User) -> None:
    a = await _version(alice, "a")
    b = await _version(alice, "b")
    c = await _version(alice, "c")
    for minute, (w, bl, result) in enumerate(
        [(a, b, "1-0"), (b, c, "1/2-1/2"), (c, a, "0-1"), (b, a, "1-0")]
    ):
        await _game(w, bl, result, minute)
        await ratings.rate_unrated()
    live = {v.id: await _ratings(v) for v in (a, b, c)}

    assert await ratings.rebuild() == 4
    assert {v.id: await _ratings(v) for v in (a, b, c)} == live


async def test_rebuild_is_admin_only(alice: User) -> None:
    with pytest.raises(ForbiddenError):
        await ratings.rebuild_as(alice)
    alice.is_admin = True
    assert await ratings.rebuild_as(alice) == 0


async def _online_runner(user: User) -> runners.RunnerConnection:
    doc = await Runner(
        id=uuid4(), owner=user, owner_login=user.login, name="box", max_games=1
    ).insert()
    return runners.mark_online(doc.id, max_games=1)


@pytest.mark.parametrize("rated", [True, False])
async def test_tournament_games_rate_on_finish(alice: User, rated: bool) -> None:
    a = await _version(alice, "a")
    b = await _version(alice, "b")
    conn = await _online_runner(alice)
    try:
        tour = await tournaments.create_tournament(
            alice,
            TournamentCreateRequest(
                name="duel",
                format=TournamentFormat.ROUND_ROBIN,
                rated=rated,
                entries=[
                    TournamentEntry(engine_id=a.engine.ref.id),
                    TournamentEntry(engine_id=b.engine.ref.id),
                ],
                games_per_pairing=1,
                runner_id=conn.runner_id,
                tc="60+1",
            ),
        )
        game = await Game.find_one(Game.tournament_id == tour.id)
        assert game is not None and game.rated == rated
        await streaming.finish_game(
            game.id, GameEndEvent(result="1-0", status=GameStatus.ENDED)
        )

        detail = await tournaments.tournament_detail(tour.id)
        assert detail.rated == rated
        assert detail.rating_category == RatingCategory.BLITZ
        winner = detail.standings[0]
        if rated:
            assert winner.rating is not None and winner.rating.mu > 1500
            gain = winner.rating.mu - 1500
            assert winner.rating_delta == pytest.approx(gain)
            assert detail.standings[1].rating_delta == pytest.approx(-gain)
        else:
            assert winner.rating is None and winner.rating_delta is None
            assert await _ratings(a) == {}
    finally:
        for game_id in conn.game_ids():
            streaming.game_registry.unregister(game_id)
        runners.mark_offline(conn)
