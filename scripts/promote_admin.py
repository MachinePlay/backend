"""Flip a user's `is_admin` flag by GitHub login.

Usage (from the `backend/` directory):
    uv run python scripts/promote_admin.py <github_login>
"""

import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from beanie import init_beanie
from pymongo import AsyncMongoClient

from app.config import settings
from app.models import User


async def main(login: str) -> None:
    # `dict[str, Any]` is pymongo's _DocumentType — shape of raw BSON results.
    # Irrelevant here since all reads/writes go through beanie's ODM.
    client: AsyncMongoClient[dict[str, Any]] = AsyncMongoClient(settings.mongo_url)
    try:
        await init_beanie(database=client[settings.mongo_db], document_models=[User])
        user = await User.find_one(User.login == login)
        if user is None:
            print(f"no user with login {login!r} (have they logged in yet?)")
            raise SystemExit(1)
        user.is_admin = True
        await user.save()
        print(f"{login} is now an admin (id={user.id})")
    finally:
        await client.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: uv run python scripts/promote_admin.py <github_login>")
        raise SystemExit(2)
    asyncio.run(main(sys.argv[1]))
