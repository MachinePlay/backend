"""Public user profiles and case-insensitive login lookup."""

import re

from app import engines, games
from app.exceptions import NotFoundError
from app.models import Engine, User
from app.schemas import GameOut, UserProfileOut


async def find_by_login(login: str) -> User | None:
    """Case-insensitive lookup so e.g. 'saegl' can't shadow 'Saegl'."""
    return await User.find_one(
        {"login": {"$regex": f"^{re.escape(login)}$", "$options": "i"}}
    )


async def by_login(login: str) -> User:
    user = await find_by_login(login)
    if user is None:
        raise NotFoundError("user not found")
    return user


async def profile(login: str) -> UserProfileOut:
    """Public profile: the user, their engines, and those engines' games."""
    user = await by_login(login)
    owned = await Engine.find(Engine.owner_id == user.id).to_list()
    recent = await games.recent_games([e.id for e in owned])
    return UserProfileOut(
        login=user.login,
        name=user.name,
        avatar_url=user.avatar_url,
        created_at=user.created_at,
        engines=await engines.to_out(owned),
        games=[GameOut.model_validate(g) for g in recent],
    )
