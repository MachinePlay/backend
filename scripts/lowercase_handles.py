"""One-off migration: lowercase user handles and their denormalized copies.

Handles created before the pick-a-handle registration flow kept GitHub's
casing (e.g. "Saegl"); lookups now expect stored-lowercase values. Run from
the `backend/` directory: `uv run python scripts/lowercase_handles.py`.
Idempotent — already-lowercase docs are left untouched.
"""

import asyncio

from app import db
from app.models import Engine, Runner, User


async def main() -> None:
    client = await db.connect()
    try:
        changed = 0
        async for user in User.find_all():
            if user.login != user.login.lower():
                print(f"user {user.login!r} -> {user.login.lower()!r}")
                user.login = user.login.lower()
                await user.save()
                changed += 1
        for model in (Engine, Runner):
            async for doc in model.find_all():
                if doc.owner_login != doc.owner_login.lower():
                    print(
                        f"{model.__name__} {doc.id}: "
                        f"{doc.owner_login!r} -> {doc.owner_login.lower()!r}"
                    )
                    doc.owner_login = doc.owner_login.lower()
                    await doc.save()
                    changed += 1
        print(f"done, {changed} document(s) updated")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
