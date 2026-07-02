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
    TournamentOut,
    TournamentParticipantOut,
)

logger = logging.getLogger(__name__)

# N*(N-1)/2 pairings * games_per_pairing grows fast; keep tournaments bounded.
MAX_PARTICIPANTS = 8
MAX_GAMES_PER_PAIRING = 20

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
    if not 1 <= payload.games_per_pairing <= MAX_GAMES_PER_PAIRING:
        raise ConflictError(
            f"games_per_pairing must be between 1 and {MAX_GAMES_PER_PAIRING}"
        )

    ids = [entry.engine_id for entry in payload.entries]
    if len(set(ids)) != len(ids):
        raise ConflictError("participants must be distinct engines")
    if len(ids) < 2:
        raise ConflictError("a tournament needs at least 2 engines")
    if len(ids) > MAX_PARTICIPANTS:
        raise ConflictError(f"at most {MAX_PARTICIPANTS} engines per tournament")

    head_id: UUID | None = None
    if payload.format == TournamentFormat.GAUNTLET:
        head_id = payload.gauntlet_head_id
        if head_id is None or head_id not in ids:
            raise ConflictError("a gauntlet needs a head engine among the participants")

    # The runner must be online now; games dispatch onto it immediately.
    runner = runners.get_online(payload.runner_id)
    tc = payload.tc or settings.tc

    # Resolve engines and snapshot the chosen version (or each engine's latest).
    engine_by_id: dict[UUID, Engine] = {}
    version_by_id: dict[UUID, EngineVersion] = {}
    participants: list[TournamentParticipant] = []
    for entry in payload.entries:
        engine = await Engine.get(entry.engine_id)
        if engine is None:
            raise NotFoundError(f"engine {entry.engine_id} not found")
        version = await games._resolve_version(engine, entry.version_id)
        engine_by_id[entry.engine_id] = engine
        version_by_id[entry.engine_id] = version
        participants.append(
            TournamentParticipant(
                engine_id=entry.engine_id,
                engine_name=engine.name,
                version_id=version.id,
                version=version.version,
            )
        )

    if payload.format == TournamentFormat.GAUNTLET:
        assert head_id is not None
        order = gauntlet_order(len(ids), ids.index(head_id), payload.games_per_pairing)
    else:
        order = round_robin_order(len(ids), payload.games_per_pairing)

    tour = Tournament(
        name=name,
        creator_id=user.id,
        created_by=user.login,
        runner_id=runner.runner_id,
        tc=tc,
        format=payload.format,
        games_per_pairing=payload.games_per_pairing,
        gauntlet_head_id=head_id,
        participants=participants,
    )
    await tour.insert()

    for white_i, black_i in order:
        w, b = ids[white_i], ids[black_i]
        await games.create_game(
            engine_by_id[w],
            engine_by_id[b],
            version_by_id[w],
            version_by_id[b],
            tc=tc,
            runner_id=runner.runner_id,
            tournament_id=tour.id,
        )

    logger.info(
        "created tournament=%s %s %d players %d games by %s on runner=%s",
        tour.id,
        payload.format,
        len(ids),
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


async def _dispatch_pending(conn: RunnerConnection, tour: Tournament) -> None:
    """Schedule this tournament's PENDING games onto `conn` until it's full."""
    pending = (
        await Game.find(
            Game.tournament_id == tour.id, Game.status == GameStatus.PENDING
        )
        .sort("+created_at")
        .to_list()
    )
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
            # Full (raced) or the runner went offline mid-loop: stop; the next
            # finish/reconnect picks up where this left off.
            break


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
        await streaming.abort_game(doc.id, reason="tournament cancelled")
        if conn is not None:
            await conn.scheduled_commands.put(StopGame(game_id=doc.id))

    pending = await Game.find(
        Game.tournament_id == tour.id, Game.status == GameStatus.PENDING
    ).to_list()
    for doc in pending:
        await _mark_aborted(doc, "tournament cancelled")
    logger.info("tournament=%s cancelled by %s", tour.id, user.login)


# --- queries / serialization -------------------------------------------------


async def get_tournament(tournament_id: UUID) -> Tournament:
    tour = await Tournament.get(tournament_id)
    if tour is None:
        raise NotFoundError("tournament not found")
    return tour


def compute_standings(tour: Tournament, tourney_games: list[Game]) -> list[StandingRow]:
    """W=1 / D=0.5 / L=0 over ENDED games, sorted by score then wins then name."""
    wins: dict[UUID, int] = {p.engine_id: 0 for p in tour.participants}
    draws: dict[UUID, int] = {p.engine_id: 0 for p in tour.participants}
    losses: dict[UUID, int] = {p.engine_id: 0 for p in tour.participants}
    names = {p.engine_id: p.engine_name for p in tour.participants}

    for g in tourney_games:
        if g.status != GameStatus.ENDED:
            continue
        w, b = g.white_id, g.black_id
        if w not in names or b not in names:
            continue
        if g.result == "1-0":
            wins[w] += 1
            losses[b] += 1
        elif g.result == "0-1":
            wins[b] += 1
            losses[w] += 1
        elif g.result == "1/2-1/2":
            draws[w] += 1
            draws[b] += 1

    rows = [
        StandingRow(
            engine_id=eid,
            engine_name=names[eid],
            played=wins[eid] + draws[eid] + losses[eid],
            wins=wins[eid],
            draws=draws[eid],
            losses=losses[eid],
            score=wins[eid] + 0.5 * draws[eid],
        )
        for eid in names
    ]
    rows.sort(key=lambda r: (-r.score, -r.wins, r.engine_name))
    return rows


async def tournament_detail(tournament_id: UUID) -> TournamentDetailOut:
    tour = await get_tournament(tournament_id)
    tourney_games = (
        await Game.find(Game.tournament_id == tour.id).sort("+created_at").to_list()
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
        gauntlet_head_id=tour.gauntlet_head_id,
        participants=[
            TournamentParticipantOut.model_validate(p) for p in tour.participants
        ],
        standings=compute_standings(tour, tourney_games),
        games=[GameOut.model_validate(g) for g in tourney_games],
        created_at=tour.created_at,
        ended_at=tour.ended_at,
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
