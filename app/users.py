"""Public user profiles and case-insensitive login lookup."""

from app import engines, games
from app.exceptions import NotFoundError
from app.models import Engine, User
from app.schemas import GameOut, UserProfileOut


async def find_by_login(login: str) -> User | None:
    """Case-insensitive lookup: handles are stored lowercase (validated at
    registration), so lowercasing the input gives an exact, indexable match."""
    return await User.find_one(User.login == login.lower())


async def by_login(login: str) -> User:
    user = await find_by_login(login)
    if user is None:
        raise NotFoundError("user not found")
    return user


async def profile(login: str) -> UserProfileOut:
    """Public profile: the user, their engines, and those engines' games."""
    user = await by_login(login)
    owned = await Engine.find({"owner.$id": user.id}).to_list()
    recent = await games.recent_games([e.id for e in owned])
    return UserProfileOut(
        login=user.login,
        name=user.name,
        avatar_url=user.avatar_url,
        created_at=user.created_at,
        engines=await engines.to_out(owned),
        games=[GameOut.model_validate(g) for g in recent],
    )
