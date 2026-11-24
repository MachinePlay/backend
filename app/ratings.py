"""Glicko-2 ratings per engine version and speed category.

Every rated game is its own rating period, applied in `ended_at` order. A
version that sits out keeps its RD: a binary doesn't get rusty, so Glicko-2's
"didn't play" step is never applied.
"""

import asyncio
import logging
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field
from pymongo import UpdateOne

from machineplay.schemas import GameEndEvent, GameStatus

from app import glicko2, streaming
from app.config import settings
from app.exceptions import ForbiddenError
from app.models import (
    EngineVersion,
    Game,
    Rating,
    RatingCategory,
    RatingChange,
    User,
    utcnow,
)

logger = logging.getLogger(__name__)

# Glickman suggests tau in 0.3..1.2; low because a version's strength is fixed.
ENV = glicko2.Glicko2(tau=0.5)

SCORES = {"1-0": glicko2.WIN, "1/2-1/2": glicko2.DRAW, "0-1": glicko2.LOSS}

# Lichess's base + 40*inc estimate, with cut-offs scaled down to engine speeds
# so each of the frontend's TC presets has a category to land in.
BULLET_UNDER_SECONDS = 60
BLITZ_UNDER_SECONDS = 300


def category(tc: str) -> RatingCategory:
    base, _, inc = tc.partition("+")
    estimate = float(base) + 40 * float(inc or 0)
    if estimate < BULLET_UNDER_SECONDS:
        return RatingCategory.BULLET
    if estimate < BLITZ_UNDER_SECONDS:
        return RatingCategory.BLITZ
    return RatingCategory.RAPID


def rate(rating: Rating, score: float, opponent: Rating) -> Rating:
    after = ENV.rate(
        glicko2.Rating(rating.mu, rating.phi, rating.sigma),
        [(score, glicko2.Rating(opponent.mu, opponent.phi, opponent.sigma))],
    )
    return Rating(mu=after.mu, phi=after.phi, sigma=after.sigma, games=rating.games + 1)


class _Unrated(BaseModel):
    """The few fields rating needs; full game docs run to ~100KB."""

    id: UUID = Field(alias="_id")
    white_version_id: UUID | None = None
    black_version_id: UUID | None = None
    result: str
    tc: str | None = None


_lock = asyncio.Lock()


async def rate_unrated() -> int:
    """Rate every eligible game not rated yet; returns how many were taken."""
    async with _lock:
        return await _rate_unrated()


async def rebuild() -> int:
    """Forget all ratings and replay every eligible game."""
    async with _lock:
        await EngineVersion.find_all().update({"$set": {"ratings": {}}})
        await Game.find({"rated_at": {"$ne": None}}).update(
            {"$set": {"rated_at": None, "white_rating": None, "black_rating": None}}
        )
        count = await _rate_unrated()
    logger.info("rebuilt ratings from %d game(s)", count)
    return count


async def rebuild_as(user: User) -> int:
    if not user.is_admin:
        raise ForbiddenError("only admins can rebuild ratings")
    return await rebuild()


async def _rate_unrated() -> int:
    rows = (
        await Game.find(
            {
                "rated": True,
                "rated_at": None,
                "status": GameStatus.ENDED,
                "result": {"$in": list(SCORES)},
            }
        )
        .sort("+ended_at", "+_id")
        .project(_Unrated)
        .to_list()
    )
    if not rows:
        return 0

    version_ids = {
        vid
        for row in rows
        for vid in (row.white_version_id, row.black_version_id)
        if vid is not None
    }
    versions = {
        v.id: v
        for v in await EngineVersion.find({"_id": {"$in": list(version_ids)}}).to_list()
    }
    touched: dict[UUID, EngineVersion] = {}
    now = utcnow()
    game_ops: list[UpdateOne] = []
    for row in rows:
        update: dict[str, Any] = {"rated_at": now}
        white = versions.get(row.white_version_id) if row.white_version_id else None
        black = versions.get(row.black_version_id) if row.black_version_id else None
        # A deleted version leaves the game marked rated with no change.
        if white is not None and black is not None and white.id != black.id:
            cat = category(row.tc or settings.tc)
            w_before = white.ratings.get(cat, Rating.initial())
            b_before = black.ratings.get(cat, Rating.initial())
            score = SCORES[row.result]
            white.ratings[cat] = rate(w_before, score, b_before)
            black.ratings[cat] = rate(b_before, 1 - score, w_before)
            update["white_rating"] = RatingChange(
                before=w_before.mu, after=white.ratings[cat].mu
            ).model_dump()
            update["black_rating"] = RatingChange(
                before=b_before.mu, after=black.ratings[cat].mu
            ).model_dump()
            touched[white.id] = white
            touched[black.id] = black
        game_ops.append(UpdateOne({"_id": row.id, "rated_at": None}, {"$set": update}))

    await Game.get_pymongo_collection().bulk_write(game_ops, ordered=False)
    for version in touched.values():
        await EngineVersion.find_one(EngineVersion.id == version.id).update(
            {
                "$set": {
                    "ratings": {
                        cat.value: r.model_dump() for cat, r in version.ratings.items()
                    }
                }
            }
        )
    return len(rows)


async def _on_game_finished(game_id: UUID, event: GameEndEvent) -> None:
    if event.status == GameStatus.ENDED:
        await rate_unrated()


streaming.on_game_finished(_on_game_finished)
