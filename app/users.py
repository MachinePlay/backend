"""Public user profiles and case-insensitive login lookup."""

from app import engines, games, runners
from app.exceptions import NotFoundError
from app.models import Engine, Runner, Tournament, User
from app.schemas import UserProfileOut


async def find_by_login(login: str) -> User | None:
    # Handles are stored lowercase.
    return await User.find_one(User.login == login.lower())


async def by_login(login: str) -> User:
    user = await find_by_login(login)
    if user is None:
        raise NotFoundError("user not found")
    return user


async def profile(login: str) -> UserProfileOut:
    user = await by_login(login)
    owned = await Engine.find({"owner.$id": user.id}).to_list()
    recent = await games.recent_games([e.id for e in owned])
    return UserProfileOut(
        login=user.login,
        name=user.name,
        avatar_url=user.avatar_url,
        created_at=user.created_at,
        engines=await engines.to_out(owned),
        games=await games.to_out(recent),
        runners=await runners.to_out(
            await Runner.find({"owner.$id": user.id}).sort("+created_at").to_list()
        ),
        tournament_count=await Tournament.find(
            Tournament.creator_id == user.id
        ).count(),
    )
