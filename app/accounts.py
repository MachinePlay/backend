"""Account deletion, as an ordered script rather than a cascade, because some of
what an account owns is live: tokens, running tournaments, playing games.

Order: revoke access; stop tournaments, then games (a running tournament answers
a finished game by dispatching the next one); delete engines, images and
runners; scrub the handle from history; delete the user. Finished games survive
because they carry denormalized names and versions.
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

# Not a legal handle, so whoever registers the freed handle doesn't inherit
# credit for the old account's tournaments.
DELETED_LOGIN = "[deleted]"

REASON = "owner account deleted"


async def delete_account(user: User) -> None:
    """Delete `user` and everything they own, ending their live work first.

    That includes other users' tournaments pinned to this account's runners,
    which can never reconnect to finish them.
    """
    logger.info("deleting account %s id=%s", user.login, user.id)

    await ApiToken.find({"user.$id": user.id}).delete()

    engine_ids = [e.id for e in await Engine.find({"owner.$id": user.id}).to_list()]
    runner_ids = [r.id for r in await RunnerDoc.find({"owner.$id": user.id}).to_list()]

    await _abort_running_tournaments(user.id, runner_ids)
    await _abort_active_games(engine_ids, runner_ids)

    await engines.delete_owned_engines(user)
    await RunnerDoc.find({"owner.$id": user.id}).delete()
    for runner_id in runner_ids:
        _drop_runner_connection(runner_id)

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
    """Tournaments the account created or that run on its runners."""
    running = await Tournament.find(
        Tournament.status == TournamentStatus.RUNNING,
        {"$or": [{"creator_id": user_id}, {"runner_id": {"$in": runner_ids}}]},
    ).to_list()
    for tour in running:
        await tournaments.abort_tournament(tour, REASON)


async def _abort_active_games(engine_ids: list[UUID], runner_ids: list[UUID]) -> None:
    """Pending or playing games of the account's engines or on its runners."""
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
    """Abort one game, re-reading its status first: another tournament's
    dispatch may have started a game the sweep listed as pending."""
    doc = await Game.get(game_id)
    if doc is None or doc.status not in (GameStatus.PENDING, GameStatus.PLAYING):
        return
    if doc.status == GameStatus.PLAYING:
        # As in `games.cancel_game`.
        conn = runners.find_by_game(doc.id)
        await streaming.abort_game(doc.id, reason=REASON)
        if conn is not None:
            await conn.scheduled_commands.put(StopGame(game_id=doc.id))
        return
    doc.status = GameStatus.ABORTED
    doc.result = "*"
    doc.reason = REASON
    doc.ended_at = utcnow()
    await doc.save()


def _drop_runner_connection(runner_id: UUID) -> None:
    """Take a deleted runner offline now: its socket may linger, and
    ``get_online`` would keep scheduling onto it."""
    conn = runners.find_online(runner_id)
    if conn is None or not runners.mark_offline(conn):
        return
    streaming.runner_stream.broadcast(
        RunnerLiveEvent(
            runner_id=runner_id, online=False, active_games=0, telemetry=None
        )
    )
