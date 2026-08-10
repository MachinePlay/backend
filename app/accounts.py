"""Account deletion: tearing one user's account down in a defined order.

Deleting an account is not a cascade over foreign keys — it is a short,
ordered script, because half of what the account owns is *live*: tokens that
authenticate, tournaments that keep dispatching, games with containers running
on a runner. The order below is the whole design:

1. revoke access, so nothing can act as the account mid-teardown;
2. stop live work (tournaments before games — a running tournament answers a
   finished game by dispatching the next pairing);
3. delete what the account owns: engines, uploaded versions, registry images,
   runners;
4. neutralize the handle wherever public history recorded it;
5. delete the user.

Public history survives step 3 because it never depended on these documents:
games and tournament participants carry denormalized names and version strings
(``Game.white_name``, ``TournamentParticipant.version``), so a finished game
still renders with every engine it was played by gone.

This module is deliberately a leaf: it imports the domain modules it tears
down, and only ``app.routes`` imports it.
"""

import logging
from uuid import UUID

from machineplay.schemas import GameStatus, StopGame

from app import engines, runners, streaming, tournaments
from app.models import (
    ApiToken,
    Engine,
    Game,
    Runner as RunnerDoc,
    Tournament,
    TournamentStatus,
    User,
    utcnow,
)
from app.schemas import RunnerLiveEvent

logger = logging.getLogger(__name__)

# What a deleted account's handle becomes wherever public history recorded it
# (a tournament's ``created_by``). Deliberately not a legal handle — HANDLE_RE
# allows only a-z, 0-9 and hyphens — so it can't collide with a real user, and
# so somebody registering the freed handle later doesn't inherit the credit for
# the old account's tournaments.
DELETED_LOGIN = "[deleted]"

# Stamped on every game and tournament stopped by the deletion, so history says
# why they ended rather than just showing an unexplained abort.
REASON = "owner account deleted"


async def delete_account(user: User) -> None:
    """Delete `user` and everything they own, ending their live work first.

    There is no confirmation argument: the website makes the user type their
    handle before calling this, but that is a "slow the human down" gate, and
    a caller who can authenticate as the account can always satisfy it. The
    request itself names its own target, so there is nothing an echoed handle
    would disambiguate.

    Active work is ended rather than refused — the account is going away, so
    there is nothing to come back and finish it. That deliberately reaches one
    step past the account itself: a tournament somebody else pinned to *this*
    account's runner is stopped too, because the runner leaves with the account
    and its token is revoked here, so it can never reconnect to dispatch the
    rest.
    """
    logger.info("deleting account %s id=%s", user.login, user.id)

    # 1. Revoke access first: from here on no CLI token, registry push or
    #    runner connect can act as this account while the teardown runs.
    await ApiToken.find({"user.$id": user.id}).delete()

    # What the account owns, collected before anything is deleted — the game
    # and tournament sweeps below are keyed off these ids.
    engine_ids = [e.id for e in await Engine.find({"owner.$id": user.id}).to_list()]
    runner_ids = [r.id for r in await RunnerDoc.find({"owner.$id": user.id}).to_list()]

    # 2. Stop live work.
    await _abort_running_tournaments(user.id, runner_ids)
    await _abort_active_games(engine_ids, runner_ids)

    # 3. Delete what the account owns. Engines take their uploaded versions and
    #    registry images with them; runners lose their durable record, and their
    #    live connection is dropped so nothing can be scheduled onto a runner
    #    that no longer exists (the socket itself dies when its process notices,
    #    and can't authenticate again).
    await engines.delete_owned_engines(user)
    await RunnerDoc.find({"owner.$id": user.id}).delete()
    for runner_id in runner_ids:
        _drop_runner_connection(runner_id)

    # 4. Public history keeps its snapshots, but not the handle: the handle is
    #    free to register again the moment the user document is gone.
    await Tournament.find(Tournament.creator_id == user.id).update(
        {"$set": {"created_by": DELETED_LOGIN}}
    )

    await user.delete()
    logger.info(
        "deleted account %s (%d engine(s), %d runner(s))",
        user.login,
        len(engine_ids),
        len(runner_ids),
    )


async def _abort_running_tournaments(user_id: UUID, runner_ids: list[UUID]) -> None:
    """Stop every running tournament the account is responsible for: the ones it
    created, plus any pinned to a runner that is disappearing with it."""
    running = await Tournament.find(
        Tournament.status == TournamentStatus.RUNNING,
        {"$or": [{"creator_id": user_id}, {"runner_id": {"$in": runner_ids}}]},
    ).to_list()
    for tour in running:
        await tournaments.abort_tournament(tour, REASON)


async def _abort_active_games(engine_ids: list[UUID], runner_ids: list[UUID]) -> None:
    """End every game still pending or playing that dies with this account: the
    ones its engines are in, plus whatever its runners are mid-game on.

    Both id lists are empty-safe: ``$in: []`` matches nothing, so an account
    with no engines and no runners sweeps no games.
    """
    active = await Game.find(
        {
            "$or": [
                {"white.$id": {"$in": engine_ids}},
                {"black.$id": {"$in": engine_ids}},
                {"runner_id": {"$in": runner_ids}},
            ],
            "status": {"$in": [GameStatus.PENDING, GameStatus.PLAYING]},
        }
    ).to_list()
    for doc in active:
        await _end_game(doc.id)


async def _end_game(game_id: UUID) -> None:
    """Abort one game now, re-reading its status first.

    The re-read matters: aborting a game runs the game-finished hooks, and
    somebody *else's* running tournament on the same runner answers those by
    dispatching its next pairing — so a game this sweep listed as pending can
    already be playing by the time the loop reaches it. Marking that one
    finished from a stale document would leave its containers running.
    """
    doc = await Game.get(game_id)
    if doc is None or doc.status not in (GameStatus.PENDING, GameStatus.PLAYING):
        return
    if doc.status == GameStatus.PLAYING:
        # Same two steps as `games.cancel_game`: end it server-side, then tell
        # whichever runner holds it to kill the engine containers.
        conn = runners.find_by_game(doc.id)
        await streaming.abort_game(doc.id, reason=REASON)
        if conn is not None:
            await conn.scheduled_commands.put(StopGame(game_id=doc.id))
        return
    # Never dispatched, so there is nothing to stop — just record the ending.
    doc.status = GameStatus.ABORTED
    doc.result = "*"
    doc.reason = REASON
    doc.ended_at = utcnow()
    await doc.save()


def _drop_runner_connection(runner_id: UUID) -> None:
    """Take a deleted runner offline immediately.

    Its WebSocket may stay up until the runner process notices, but the backend
    must stop offering it: ``get_online`` reads the in-memory map, so a runner
    left there would still accept `POST /game` after its record was deleted.
    """
    conn = runners.find_online(runner_id)
    if conn is None or not runners.mark_offline(conn):
        return
    streaming.runner_stream.broadcast(
        RunnerLiveEvent(
            runner_id=runner_id, online=False, active_games=0, telemetry=None
        )
    )
