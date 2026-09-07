"""Tournaments: pairing schedules run as ordinary single games on one runner.

A tournament pins every game to a single runner (same hardware => fair) and
snapshots each participant's engine version at creation. Pairings are generated
here (round robin or gauntlet) and inserted as PENDING ``Game`` docs up front;
they are then dispatched onto the runner up to its ``max_games`` capacity and
advanced as slots free up, reusing the single-game pipeline
(``create_game``/``schedule_game``) rather than fastchess's own tournament mode.

Two hooks drive it:
- ``_on_game_finished`` (streaming's game-finished hook): a finished game frees a
  slot, so dispatch the next pending pairing on that runner; a tournament game
  aborted for an infrastructure reason is re-queued to retry.
- ``_on_runner_connected`` (runners' connect hook): a returning runner re-queues
  its tournaments' retryable-aborted games and resumes dispatch.
"""

import asyncio
import logging
from collections import Counter
from typing import NamedTuple
from uuid import UUID

from machineplay.schemas import GameEndEvent, GameStatus, StopGame

from app import games, runners, streaming
from app.config import settings
from app.exceptions import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    RunnerBusyError,
)
from app.models import (
    Engine,
    EngineVersion,
    Game,
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

# The cap that matters is the tournament's *total* games, not its per-pairing
# count: a round robin has N*(N-1)/2 pairings, so the same "games per pairing"
# means 500 games for a two-engine match and 14000 for eight players. 2000 is
# where a match stops being cheap — a finished game doc runs to ~100KB — and is
# also about where the elo error bars stop shrinking usefully (~±13 elo).
MAX_GAMES_PER_TOURNAMENT = 2000

# Rough per-game wallclock for the ETA. Engines at a fixed time control spend
# most of their budget, so a game costs about both sides' allowance over an
# assumed 40 moves each, plus docker/fastchess startup. Deliberately crude: the
# point is to tell "20 minutes" from "two days" before the tournament starts.
ETA_MOVES_PER_SIDE = 40
ETA_STARTUP_SECONDS = 10.0

# Aborts a tournament game can recover from by re-queueing it: the game never
# got a fair result because the infrastructure went away, not the engine. Engine
# faults (crash, wallclock timeout, failed pull) are left ABORTED and simply
# don't count toward standings — retrying them could loop forever.
RETRYABLE_REASONS = {"runner disconnected", "backend restarted"}


# --- pairing generation (pure; unit-tested) ----------------------------------


def round_robin_order(n: int, games_per_pairing: int) -> list[tuple[int, int]]:
    """(white, black) participant-index pairs for a round robin over `n` players.

    Interleaved by round so every pairing plays its k-th game before any plays
    its (k+1)-th, and colors swap each round — standings stay meaningful as the
    tournament progresses.
    """
    order: list[tuple[int, int]] = []
    for k in range(games_per_pairing):
        for i in range(n):
            for j in range(i + 1, n):
                order.append((i, j) if k % 2 == 0 else (j, i))
    return order


def gauntlet_order(n: int, head: int, games_per_pairing: int) -> list[tuple[int, int]]:
    """(white, black) index pairs for `head` playing every other participant."""
    order: list[tuple[int, int]] = []
    for k in range(games_per_pairing):
        for j in range(n):
            if j == head:
                continue
            order.append((head, j) if k % 2 == 0 else (j, head))
    return order


# --- creation ----------------------------------------------------------------


async def create_tournament(user: User, payload: TournamentCreateRequest) -> Tournament:
    """Validate the request, snapshot versions, insert the tournament plus all
    its pairings as PENDING games, and dispatch onto the (online) runner."""
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

    # The runner must be online now; games dispatch onto it immediately.
    runner = runners.get_online(payload.runner_id)
    tc = payload.tc or settings.tc

    # Resolve each entry's engine + chosen version (or its latest). A participant
    # is a distinct (engine, version) pair, so the same engine may enter at two
    # different versions — but the exact same pair twice is a duplicate. Keep the
    # resolved engines/versions positional (aligned with participants) so the
    # pairing indices address them even when an engine repeats.
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
        games_per_pairing=payload.games_per_pairing,
        # The head participant's version id is the stable per-participant key
        # used to highlight it in standings.
        gauntlet_head_version_id=(
            participants[head].version_id if head is not None else None
        ),
        participants=participants,
    )
    await tour.insert()

    # One insert for the whole schedule: 2000 individual round trips would keep
    # the POST open for seconds. `tournament_seq` carries the order, since the
    # batch shares a `created_at`.
    await Game.insert_many(
        [
            games.build_game(
                engines_ordered[white_i],
                engines_ordered[black_i],
                versions_ordered[white_i],
                versions_ordered[black_i],
                tc=tc,
                runner_id=runner.runner_id,
                tournament_id=tour.id,
                tournament_seq=seq,
            )
            for seq, (white_i, black_i) in enumerate(order)
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


# --- dispatch / advance / resume ---------------------------------------------

# Serializes all tournament dispatch so a request-time dispatch and a hook-time
# dispatch can't both claim the same PENDING game (double-schedule it). Cheap at
# this scale; scheduling is a handful of awaits.
_dispatch_lock = asyncio.Lock()


async def _mark_aborted(doc: Game, reason: str) -> None:
    doc.status = GameStatus.ABORTED
    doc.result = "*"
    doc.reason = reason
    doc.ended_at = utcnow()
    await doc.save()


async def _requeue(doc: Game) -> None:
    """Reset a game back to PENDING so it gets dispatched (again)."""
    doc.status = GameStatus.PENDING
    doc.result = None
    doc.reason = None
    doc.ended_at = None
    await doc.save()


async def _next_pending(tour: Tournament, limit: int) -> list[Game]:
    """The next `limit` pairings due to play, in schedule order.

    Ordered by `tournament_seq` so the round-interleaved pairing order is what
    actually gets played; `created_at` breaks ties for games inserted before
    that field existed. Limited because a tournament can have thousands pending
    and this runs on every game finish.
    """
    return (
        await Game.find(
            Game.tournament_id == tour.id, Game.status == GameStatus.PENDING
        )
        .sort("+tournament_seq", "+created_at")
        .limit(limit)
        .to_list()
    )


async def _dispatch_pending(conn: RunnerConnection, tour: Tournament) -> None:
    """Schedule this tournament's PENDING games onto `conn` until it's full."""
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
                # Full (raced) or the runner went offline mid-loop: stop; the
                # next finish/reconnect picks up where this left off.
                return
        # Loop rather than return: a page that was all unplayable (missing
        # image) aborted its games without filling a slot, so the next page is
        # still owed one. Terminates — each pass either schedules or aborts.


async def _dispatch_runner(runner_id: UUID) -> None:
    """Fill a runner's free slots with pending games from its running
    tournaments (oldest first), and complete any that have nothing left."""
    async with _dispatch_lock:
        conn = runners.find_online(runner_id)
        if conn is None:
            return  # offline; resumes when it reconnects
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
    """Mark a tournament COMPLETED once no game is pending or playing."""
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
    """Game-finished hook: retry infra-aborted tournament games, then fill the
    slot the finished game freed on its runner."""
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
    """Runner-connect hook: re-queue retryable-aborted games (e.g. orphaned by a
    backend restart or a disconnect) for this runner's tournaments, then dispatch."""
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


# --- cancellation ------------------------------------------------------------


async def cancel_tournament(user: User, tournament_id: UUID) -> None:
    """Stop a running tournament: abort its live game(s), drop the rest."""
    tour = await get_tournament(tournament_id)
    if tour.status != TournamentStatus.RUNNING:
        raise ConflictError("tournament is not running")
    if tour.creator_id != user.id and not user.is_admin:
        raise ForbiddenError("only the creator can cancel this tournament")

    await abort_tournament(tour, "tournament cancelled")
    logger.info("tournament=%s cancelled by %s", tour.id, user.login)


async def abort_tournament(tour: Tournament, reason: str) -> None:
    """End a RUNNING tournament now, without asking who is doing it.

    `cancel_tournament` is the permission-checked entry point; account deletion
    calls this directly for work it is tearing down on the owner's behalf.
    `reason` lands on every game it stops, so history says why.
    """
    # Flip to ABORTED first so no game-finished hook re-dispatches it as its
    # games are torn down.
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


# --- queries / serialization -------------------------------------------------


async def get_tournament(tournament_id: UUID) -> Tournament:
    tour = await Tournament.get(tournament_id)
    if tour is None:
        raise NotFoundError("tournament not found")
    return tour


class OutcomeCount(NamedTuple):
    """How many of a tournament's games share one (status, pairing, result).

    The unit the standings/progress aggregate returns: at most a few per
    pairing, so a 2000-game tournament summarises into a handful of rows
    instead of 2000 documents on the wire.
    """

    status: str
    white_version_id: UUID | None
    black_version_id: UUID | None
    result: str | None
    # Named `game_count`, not `count`: NamedTuple inherits `tuple.count`.
    game_count: int


async def _outcome_counts(tournament_id: UUID) -> list[OutcomeCount]:
    """Group the tournament's games by status, pairing and result, in Mongo."""
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
        )
        for row in rows
    ]


def compute_progress(counts: list[OutcomeCount]) -> TournamentProgress:
    """Per-status game counts for the tournament, from the grouped rows."""
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
    tour: Tournament, counts: list[OutcomeCount]
) -> list[StandingRow]:
    """W=1 / D=0.5 / L=0 over ENDED games, sorted by score then wins then name.

    Keyed by version id: a participant is an (engine, version) pair, so two
    versions of the same engine keep separate rows.
    """
    parts = {p.version_id: p for p in tour.participants}
    wins: dict[UUID, int] = {vid: 0 for vid in parts}
    draws: dict[UUID, int] = {vid: 0 for vid in parts}
    losses: dict[UUID, int] = {vid: 0 for vid in parts}

    for row in counts:
        if row.status != GameStatus.ENDED:
            continue
        w, b = row.white_version_id, row.black_version_id
        if w not in parts or b not in parts:
            continue
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
        )
        for vid, p in parts.items()
    ]
    rows.sort(key=lambda r: (-r.score, -r.wins, r.engine_name, r.version))
    return rows


# --- ETA ---------------------------------------------------------------------


def estimate_game_seconds(tc: str) -> float:
    """Roughly how long one game at time control `tc` takes, wallclock.

    Engines at a fixed time control spend most of their allowance, so a game
    costs about both sides' budget over `ETA_MOVES_PER_SIDE` moves, plus image
    pull/startup. Mirrored in the frontend (`src/tc.ts`) for the create form,
    which needs the estimate before the tournament exists.
    """
    base, _, inc = tc.partition("+")
    seconds = 2 * (float(base) + ETA_MOVES_PER_SIDE * float(inc or 0))
    return seconds + ETA_STARTUP_SECONDS


def estimate_remaining_seconds(tc: str, unplayed: int, max_games: int) -> float | None:
    """Wallclock left for `unplayed` games sharing `max_games` runner slots."""
    if unplayed <= 0 or max_games <= 0:
        return None
    return unplayed * estimate_game_seconds(tc) / max_games


async def _runner_slots(runner_id: UUID) -> int:
    """The runner's game capacity: live if it is connected, else last reported."""
    conn = runners.find_online(runner_id)
    if conn is not None:
        return conn.max_games
    doc = await Runner.get(runner_id)
    return doc.max_games if doc is not None else 0


# --- serialization -----------------------------------------------------------


async def tournament_detail(tournament_id: UUID) -> TournamentDetailOut:
    """Everything the tournament page shows except the pairings list.

    Standings and progress come from one grouped aggregate and the live boards
    from the handful of PLAYING games, so the response size is set by the
    participant and runner-slot counts rather than by how many games the
    tournament runs. The pairings themselves are paged in via
    `tournament_games`.
    """
    tour = await get_tournament(tournament_id)
    counts = await _outcome_counts(tour.id)
    progress = compute_progress(counts)
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
        runner_id=tour.runner_id,
        created_by=tour.created_by,
        tc=tour.tc,
        games_per_pairing=tour.games_per_pairing,
        gauntlet_head_version_id=tour.gauntlet_head_version_id,
        participants=[
            TournamentParticipantOut.model_validate(p) for p in tour.participants
        ],
        standings=compute_standings(tour, counts),
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
    """One page of the pairings list, in schedule order, optionally by status."""
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
    """(total, completed) game counts per tournament, in one aggregate query."""
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


# Register the schedulers at import time so they're live wherever the app is
# imported (prod and tests alike). Both are no-ops for non-tournament work.
streaming.on_game_finished(_on_game_finished)
runners.on_runner_connected(_on_runner_connected)
