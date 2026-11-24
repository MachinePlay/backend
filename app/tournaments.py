"""Tournaments: pairing schedules played as ordinary single games on one runner.

All pairings are inserted as PENDING games up front and dispatched as runner
slots free up, driven by two hooks: a finished game fills its slot (re-queueing
infrastructure aborts), and a reconnecting runner resumes its tournaments.
"""

import asyncio
import logging
import random
from collections import Counter
from typing import NamedTuple
from uuid import UUID

from machineplay.schemas import GameEndEvent, GameStatus, StopGame

from app import books, games, ratings, runners, streaming
from app.config import settings
from app.exceptions import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    RunnerBusyError,
)
from app.models import (
    Book,
    Engine,
    EngineVersion,
    Game,
    Rating,
    Runner,
    Tournament,
    TournamentFormat,
    TournamentParticipant,
    TournamentStatus,
    User,
    utcnow,
)
from app.runners import RunnerConnection
from app.schemas import (
    GameOut,
    StandingRow,
    TournamentCreateRequest,
    TournamentDetailOut,
    TournamentGameRow,
    TournamentGamesOut,
    TournamentOut,
    TournamentParticipantOut,
    TournamentProgress,
)

logger = logging.getLogger(__name__)

MAX_PARTICIPANTS = 8

# Capped on total games, since round-robin pairings grow quadratically. A
# finished game doc runs to ~100KB, and past ~2000 games elo error bars
# (~±13) stop shrinking usefully.
MAX_GAMES_PER_TOURNAMENT = 2000

ETA_MOVES_PER_SIDE = 40
ETA_STARTUP_SECONDS = 10.0

# Infrastructure aborts are retried. Engine faults stay ABORTED and don't count
# toward standings; retrying them could loop forever.
RETRYABLE_REASONS = {"runner disconnected", "backend restarted"}


class Pairing(NamedTuple):
    """One scheduled game. `opening` indexes the tournament's shuffled book.

    Each line is played once from each side, which cancels the line's bias
    (Perfect 2021 favours White by design).
    """

    white: int
    black: int
    opening: int


def round_robin_order(n: int, games_per_pairing: int) -> list[Pairing]:
    """Interleaved by round, so standings stay meaningful mid-tournament."""
    order: list[Pairing] = []
    for k in range(games_per_pairing):
        for i in range(n):
            for j in range(i + 1, n):
                white, black = (i, j) if k % 2 == 0 else (j, i)
                order.append(Pairing(white, black, k // 2))
    return order


def gauntlet_order(n: int, head: int, games_per_pairing: int) -> list[Pairing]:
    order: list[Pairing] = []
    for k in range(games_per_pairing):
        for j in range(n):
            if j == head:
                continue
            white, black = (head, j) if k % 2 == 0 else (j, head)
            order.append(Pairing(white, black, k // 2))
    return order


async def create_tournament(user: User, payload: TournamentCreateRequest) -> Tournament:
    name = payload.name.strip()
    if not name:
        raise ConflictError("tournament name is required")
    if not games.TC_RE.fullmatch(payload.tc or settings.tc):
        raise ConflictError("invalid time control (expected 'base+inc')")
    if payload.games_per_pairing < 1:
        raise ConflictError("games_per_pairing must be at least 1")

    n = len(payload.entries)
    if n < 2:
        raise ConflictError("a tournament needs at least 2 participants")
    if n > MAX_PARTICIPANTS:
        raise ConflictError(f"at most {MAX_PARTICIPANTS} participants per tournament")

    head: int | None = None
    if payload.format == TournamentFormat.GAUNTLET:
        head = payload.gauntlet_head_index
        if head is None or not 0 <= head < n:
            raise ConflictError("a gauntlet needs a head chosen among the participants")

    runner = runners.get_online(payload.runner_id)
    tc = payload.tc or settings.tc
    book = await books.resolve(payload.book)
    book_seed = random.randrange(2**31)
    lines = books.order(book, book_seed) if book is not None else []

    # Positional, so pairing indices address them even when an engine repeats.
    engines_ordered: list[Engine] = []
    versions_ordered: list[EngineVersion] = []
    participants: list[TournamentParticipant] = []
    seen: set[tuple[UUID, UUID]] = set()
    for entry in payload.entries:
        engine = await Engine.get(entry.engine_id)
        if engine is None:
            raise NotFoundError(f"engine {entry.engine_id} not found")
        version = await games._resolve_version(engine, entry.version_id)
        if (engine.id, version.id) in seen:
            raise ConflictError(
                f"{engine.name!r} {version.version!r} is entered more than once"
            )
        seen.add((engine.id, version.id))
        engines_ordered.append(engine)
        versions_ordered.append(version)
        participants.append(
            TournamentParticipant(
                engine_id=engine.id,
                engine_name=engine.name,
                version_id=version.id,
                version=version.version,
            )
        )

    if payload.format == TournamentFormat.GAUNTLET:
        assert head is not None
        order = gauntlet_order(n, head, payload.games_per_pairing)
    else:
        order = round_robin_order(n, payload.games_per_pairing)

    if len(order) > MAX_GAMES_PER_TOURNAMENT:
        raise ConflictError(
            f"{len(order)} games is over the {MAX_GAMES_PER_TOURNAMENT} per "
            f"tournament limit ({len(order) // payload.games_per_pairing} "
            f"pairings x {payload.games_per_pairing} games)"
        )

    tour = Tournament(
        name=name,
        creator_id=user.id,
        created_by=user.login,
        runner_id=runner.runner_id,
        tc=tc,
        format=payload.format,
        rated=payload.rated,
        games_per_pairing=payload.games_per_pairing,
        gauntlet_head_version_id=(
            participants[head].version_id if head is not None else None
        ),
        book=book.slug if book is not None else None,
        book_seed=book_seed,
        participants=participants,
    )
    await tour.insert()

    # One insert: 2000 round trips would keep the POST open for seconds.
    await Game.insert_many(
        [
            games.build_game(
                engines_ordered[pairing.white],
                engines_ordered[pairing.black],
                versions_ordered[pairing.white],
                versions_ordered[pairing.black],
                tc=tc,
                runner_id=runner.runner_id,
                tournament_id=tour.id,
                tournament_seq=seq,
                rated=tour.rated,
                book=tour.book,
                # Wraps when there are more rounds than lines.
                opening_moves=(lines[pairing.opening % len(lines)] if lines else []),
            )
            for seq, pairing in enumerate(order)
        ]
    )

    logger.info(
        "created tournament=%s %s %d players %d games by %s on runner=%s",
        tour.id,
        payload.format,
        n,
        len(order),
        user.login,
        runner.runner_id,
    )
    await _dispatch_runner(runner.runner_id)
    return tour


# So a request-time and a hook-time dispatch can't both claim one PENDING game.
_dispatch_lock = asyncio.Lock()


async def _mark_aborted(doc: Game, reason: str) -> None:
    doc.status = GameStatus.ABORTED
    doc.result = "*"
    doc.reason = reason
    doc.ended_at = utcnow()
    await doc.save()


async def _requeue(doc: Game) -> None:
    doc.status = GameStatus.PENDING
    doc.result = None
    doc.reason = None
    doc.ended_at = None
    await doc.save()


async def _next_pending(tour: Tournament, limit: int) -> list[Game]:
    """Limited: thousands may be pending, and this runs on every game finish."""
    return (
        await Game.find(
            Game.tournament_id == tour.id, Game.status == GameStatus.PENDING
        )
        .sort("+tournament_seq", "+created_at")
        .limit(limit)
        .to_list()
    )


async def _dispatch_pending(conn: RunnerConnection, tour: Tournament) -> None:
    while not conn.is_full():
        pending = await _next_pending(tour, conn.max_games - conn.active_games)
        if not pending:
            return
        for doc in pending:
            if conn.is_full():
                break
            wv = await EngineVersion.get(doc.white_version_id)
            bv = await EngineVersion.get(doc.black_version_id)
            if wv is None or bv is None:
                await _mark_aborted(doc, "engine version missing")
                continue
            try:
                await games.schedule_game(doc, conn, wv, bv)
            except (RunnerBusyError, NotFoundError):
                # The next finish or reconnect picks up from here.
                return
        # A page of unplayable games filled no slot; each pass schedules or
        # aborts, so this terminates.


async def _dispatch_runner(runner_id: UUID) -> None:
    """Fill a runner's free slots from its running tournaments, oldest first,
    and complete any that have nothing left."""
    async with _dispatch_lock:
        conn = runners.find_online(runner_id)
        if conn is None:
            return  # resumes on reconnect
        tours = (
            await Tournament.find(
                Tournament.runner_id == runner_id,
                Tournament.status == TournamentStatus.RUNNING,
            )
            .sort("+created_at")
            .to_list()
        )
        for tour in tours:
            if not conn.is_full():
                await _dispatch_pending(conn, tour)
            await _maybe_complete(tour)


async def _maybe_complete(tour: Tournament) -> None:
    remaining = await Game.find(
        Game.tournament_id == tour.id,
        {"status": {"$in": [GameStatus.PENDING, GameStatus.PLAYING]}},
    ).count()
    if remaining == 0:
        tour.status = TournamentStatus.COMPLETED
        tour.ended_at = utcnow()
        await tour.save()
        logger.info("tournament=%s completed", tour.id)


async def _on_game_finished(game_id: UUID, event: GameEndEvent) -> None:
    doc = await Game.get(game_id)
    if doc is None:
        return
    if doc.tournament_id is not None:
        tour = await Tournament.get(doc.tournament_id)
        if (
            tour is not None
            and tour.status == TournamentStatus.RUNNING
            and event.status == GameStatus.ABORTED
            and event.reason in RETRYABLE_REASONS
        ):
            await _requeue(doc)
    if doc.runner_id is not None:
        await _dispatch_runner(doc.runner_id)


async def _on_runner_connected(runner_id: UUID) -> None:
    tours = await Tournament.find(
        Tournament.runner_id == runner_id,
        Tournament.status == TournamentStatus.RUNNING,
    ).to_list()
    for tour in tours:
        aborted = await Game.find(
            Game.tournament_id == tour.id,
            Game.status == GameStatus.ABORTED,
            {"reason": {"$in": list(RETRYABLE_REASONS)}},
        ).to_list()
        for doc in aborted:
            await _requeue(doc)
    await _dispatch_runner(runner_id)


async def cancel_tournament(user: User, tournament_id: UUID) -> None:
    tour = await get_tournament(tournament_id)
    if tour.status != TournamentStatus.RUNNING:
        raise ConflictError("tournament is not running")
    if tour.creator_id != user.id and not user.is_admin:
        raise ForbiddenError("only the creator can cancel this tournament")

    await abort_tournament(tour, "tournament cancelled")
    logger.info("tournament=%s cancelled by %s", tour.id, user.login)


async def abort_tournament(tour: Tournament, reason: str) -> None:
    """`cancel_tournament` without the permission check."""
    # First, so game-finished hooks don't re-dispatch it.
    tour.status = TournamentStatus.ABORTED
    tour.ended_at = utcnow()
    await tour.save()

    playing = await Game.find(
        Game.tournament_id == tour.id, Game.status == GameStatus.PLAYING
    ).to_list()
    for doc in playing:
        conn = runners.find_by_game(doc.id)
        await streaming.abort_game(doc.id, reason=reason)
        if conn is not None:
            await conn.scheduled_commands.put(StopGame(game_id=doc.id))

    pending = await Game.find(
        Game.tournament_id == tour.id, Game.status == GameStatus.PENDING
    ).to_list()
    for doc in pending:
        await _mark_aborted(doc, reason)


async def get_tournament(tournament_id: UUID) -> Tournament:
    tour = await Tournament.get(tournament_id)
    if tour is None:
        raise NotFoundError("tournament not found")
    return tour


class OutcomeCount(NamedTuple):
    """How many of a tournament's games share one (status, pairing, result)."""

    status: str
    white_version_id: UUID | None
    black_version_id: UUID | None
    result: str | None
    # Not `count`, which NamedTuple inherits from tuple.
    game_count: int
    white_delta: float = 0.0
    black_delta: float = 0.0


async def _outcome_counts(tournament_id: UUID) -> list[OutcomeCount]:
    rows = await Game.aggregate(
        [
            {"$match": {"tournament_id": tournament_id}},
            {
                "$group": {
                    "_id": {
                        "status": "$status",
                        "white": "$white_version_id",
                        "black": "$black_version_id",
                        "result": "$result",
                    },
                    "count": {"$sum": 1},
                    # $subtract of a missing field is null, which $sum skips.
                    "white_delta": {
                        "$sum": {
                            "$subtract": ["$white_rating.after", "$white_rating.before"]
                        }
                    },
                    "black_delta": {
                        "$sum": {
                            "$subtract": ["$black_rating.after", "$black_rating.before"]
                        }
                    },
                }
            },
        ]
    ).to_list()
    return [
        OutcomeCount(
            status=row["_id"]["status"],
            white_version_id=row["_id"].get("white"),
            black_version_id=row["_id"].get("black"),
            result=row["_id"].get("result"),
            game_count=row["count"],
            white_delta=row["white_delta"],
            black_delta=row["black_delta"],
        )
        for row in rows
    ]


def compute_progress(counts: list[OutcomeCount]) -> TournamentProgress:
    by_status: Counter[str] = Counter()
    for row in counts:
        by_status[row.status] += row.game_count
    return TournamentProgress(
        total=sum(by_status.values()),
        pending=by_status[GameStatus.PENDING],
        playing=by_status[GameStatus.PLAYING],
        ended=by_status[GameStatus.ENDED],
        aborted=by_status[GameStatus.ABORTED],
    )


def compute_standings(
    tour: Tournament,
    counts: list[OutcomeCount],
    current: dict[UUID, Rating] | None = None,
) -> list[StandingRow]:
    """W=1 / D=0.5 / L=0 over ENDED games, keyed by version id. `current` is
    each version's rating in the tournament's category."""
    parts = {p.version_id: p for p in tour.participants}
    wins: dict[UUID, int] = {vid: 0 for vid in parts}
    draws: dict[UUID, int] = {vid: 0 for vid in parts}
    losses: dict[UUID, int] = {vid: 0 for vid in parts}
    deltas: dict[UUID, float] = {vid: 0.0 for vid in parts}

    for row in counts:
        if row.status != GameStatus.ENDED:
            continue
        w, b = row.white_version_id, row.black_version_id
        if w not in parts or b not in parts:
            continue
        deltas[w] += row.white_delta
        deltas[b] += row.black_delta
        if row.result == "1-0":
            wins[w] += row.game_count
            losses[b] += row.game_count
        elif row.result == "0-1":
            wins[b] += row.game_count
            losses[w] += row.game_count
        elif row.result == "1/2-1/2":
            draws[w] += row.game_count
            draws[b] += row.game_count

    rows = [
        StandingRow(
            engine_id=p.engine_id,
            engine_name=p.engine_name,
            version_id=vid,
            version=p.version,
            played=wins[vid] + draws[vid] + losses[vid],
            wins=wins[vid],
            draws=draws[vid],
            losses=losses[vid],
            score=wins[vid] + 0.5 * draws[vid],
            rating=(current or {}).get(vid) if tour.rated else None,
            rating_delta=deltas[vid] if tour.rated else None,
        )
        for vid, p in parts.items()
    ]
    rows.sort(key=lambda r: (-r.score, -r.wins, r.engine_name, r.version))
    return rows


def estimate_game_seconds(tc: str) -> float:
    """Crude wallclock per game: both sides spend most of their budget.
    Mirrored in the frontend's `src/tc.ts`."""
    base, _, inc = tc.partition("+")
    seconds = 2 * (float(base) + ETA_MOVES_PER_SIDE * float(inc or 0))
    return seconds + ETA_STARTUP_SECONDS


def estimate_remaining_seconds(tc: str, unplayed: int, max_games: int) -> float | None:
    """Wallclock left for `unplayed` games sharing `max_games` runner slots."""
    if unplayed <= 0 or max_games <= 0:
        return None
    return unplayed * estimate_game_seconds(tc) / max_games


async def _runner_slots(runner_id: UUID) -> int:
    conn = runners.find_online(runner_id)
    if conn is not None:
        return conn.max_games
    doc = await Runner.get(runner_id)
    return doc.max_games if doc is not None else 0


async def tournament_detail(tournament_id: UUID) -> TournamentDetailOut:
    """Everything the tournament page shows except the paged pairings list."""
    tour = await get_tournament(tournament_id)
    counts = await _outcome_counts(tour.id)
    progress = compute_progress(counts)
    category = ratings.category(tour.tc)
    versions = await EngineVersion.find(
        {"_id": {"$in": [p.version_id for p in tour.participants]}}
    ).to_list()
    current = {v.id: v.ratings[category] for v in versions if category in v.ratings}
    book = await Book.find_one(Book.slug == tour.book) if tour.book else None
    live = (
        await Game.find(
            Game.tournament_id == tour.id, Game.status == GameStatus.PLAYING
        )
        .sort("+tournament_seq", "+created_at")
        .to_list()
    )
    eta = (
        estimate_remaining_seconds(
            tour.tc,
            progress.pending + progress.playing,
            await _runner_slots(tour.runner_id),
        )
        if tour.status == TournamentStatus.RUNNING
        else None
    )
    return TournamentDetailOut(
        id=tour.id,
        name=tour.name,
        format=tour.format,
        status=tour.status,
        rated=tour.rated,
        rating_category=category,
        runner_id=tour.runner_id,
        created_by=tour.created_by,
        tc=tour.tc,
        games_per_pairing=tour.games_per_pairing,
        gauntlet_head_version_id=tour.gauntlet_head_version_id,
        book=tour.book,
        book_name=book.name if book is not None else None,
        participants=[
            TournamentParticipantOut.model_validate(p) for p in tour.participants
        ],
        standings=compute_standings(tour, counts, current),
        progress=progress,
        live_games=[GameOut.model_validate(g) for g in live],
        eta_seconds=eta,
        created_at=tour.created_at,
        ended_at=tour.ended_at,
    )


async def tournament_games(
    tournament_id: UUID,
    status: GameStatus | None,
    limit: int,
    offset: int,
) -> TournamentGamesOut:
    await get_tournament(tournament_id)
    query: dict[str, object] = {"tournament_id": tournament_id}
    if status is not None:
        query["status"] = status
    total = await Game.find(query).count()
    docs = (
        await Game.find(query)
        .sort("+tournament_seq", "+created_at")
        .skip(offset)
        .limit(limit)
        .to_list()
    )
    return TournamentGamesOut(
        games=[TournamentGameRow.model_validate(g) for g in docs],
        total=total,
    )


async def list_tournaments(limit: int) -> list[TournamentOut]:
    docs = await Tournament.find_all().sort("-created_at").limit(limit).to_list()
    counts = await _game_counts([t.id for t in docs])
    return [
        TournamentOut(
            id=t.id,
            name=t.name,
            format=t.format,
            status=t.status,
            rated=t.rated,
            runner_id=t.runner_id,
            created_by=t.created_by,
            participant_count=len(t.participants),
            games_total=counts.get(t.id, (0, 0))[0],
            games_completed=counts.get(t.id, (0, 0))[1],
            created_at=t.created_at,
            ended_at=t.ended_at,
        )
        for t in docs
    ]


async def _game_counts(tournament_ids: list[UUID]) -> dict[UUID, tuple[int, int]]:
    if not tournament_ids:
        return {}
    rows = await Game.aggregate(
        [
            {"$match": {"tournament_id": {"$in": tournament_ids}}},
            {
                "$group": {
                    "_id": "$tournament_id",
                    "total": {"$sum": 1},
                    "completed": {
                        "$sum": {"$cond": [{"$eq": ["$status", "ended"]}, 1, 0]}
                    },
                }
            },
        ]
    ).to_list()
    return {row["_id"]: (row["total"], row["completed"]) for row in rows}


streaming.on_game_finished(_on_game_finished)
runners.on_runner_connected(_on_runner_connected)
